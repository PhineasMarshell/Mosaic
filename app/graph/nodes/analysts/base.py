"""Analyst 节点通用骨架 — 预算守卫、异常捕获、降级。

P3 起 analyst（technical / fundamental / moneyflow + 可选 news/sentiment）共用此基类，
提供：
- 工具发现（按 category 查询 registry）
- 批量执行（ToolRuntime + called_signatures 去重）
- 域默认参数填充
- 异常全部捕获 → errors 追加，不炸图
- finding digest（≤200 字中间结论）
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import Settings
from app.graph.state import AnalystName
from app.graph.tool_runtime import ToolRuntime
from app.models.market import STATUS_ERROR, STATUS_PARTIAL, STATUS_SUCCESS, ToolResult

logger = logging.getLogger(__name__)


def _date_range(days_back: int = 90) -> tuple[str, str]:
    """返回 (start_str, end_str) 格式为 YYYY-MM-DD。"""
    end_dt = datetime.now(UTC)
    start_dt = end_dt - timedelta(days=days_back)
    return start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")


class MarketAnalystNode:
    """分析师节点的通用骨架。

    子类只需指定 ``category`` 和可选的 ``domain_defaults``，
    其余逻辑（工具发现、批量执行、预算守卫、异常处理）统一处理。
    """

    #: 该分析员负责的工具类别
    category: AnalystName | str = "technical"

    #: 无需 symbol 即可安全执行的 tool_name 白名单（GET / 聚合类端点）。
    #: 任何不在列表中的工具调用时必须提供 symbol，否则会因参数校验失败报错。
    WHITELIST_NO_SYMBOL: set[str] = frozenset(
        [
            # —— technical (情绪/涨跌池/板块) ——
            "public_sentiment_ashare_master_sentiment_get",
            "public_limit_up_count_ashare_master_limit_up_count_get",
            "public_limit_up_sectors_ashare_master_limit_up_sectors_get",
            "public_limit_up_pool_ashare_master_limit_up_pool_get",
            # —— crypto 无币种要求 ——
            "hyperliquid_symbols_coinglass_hyperliquid_symbols_get",
            "hyperliquid_user_count_coinglass_hyperliquid_user_count_get",
            "hyperliquid_vaults_coinglass_hyperliquid_vaults_get",
            "exchanges_market_exchanges_get",
            "health_health_get",
            "health_market_health_get",
            # —— T8：搜索 / 新闻 / 龙虎榜 / 港股内部聚合，本就不依赖 A 股 symbol ——
            "news_search",
            "search_xueqiu_search_get",
            "longhu_xueqiu_longhu_get",
            "internal_hk_northbound",
            "internal_hk_index",
        ]
    )

    def __init__(self, settings: Settings):
        """初始化分析员节点。"""
        self.settings = settings
        self.client = None  # 子类根据需要初始化 OpenAI client
        self._runtime = ToolRuntime(settings)

    async def __call__(self, state):
        """LangGraph 节点入口。

        LangGraph v1.x 可能传入 ResearchState（Pydantic）或 dict，统一处理。
        所有异常被捕获后写入 errors，不炸图。
        """
        try:
            # Normalize state to dict (handle both ResearchState and dict)
            if hasattr(state, "model_dump"):
                state = state.model_dump(exclude_none=False)
            tools_used: list[str] = []
            results: list[ToolResult] = []
            cache_stats: dict[str, int] = {}
            called_signatures: set[str] = set()

            # T18：整个 analyst 运行共用一个 Gateway 客户端（MCP 模式旧实现
            # 每个工具 spawn 一次子进程 + 握手）。异常时 async with 保证关闭。
            async with self._runtime.gateway_session():
                for result in await self._execute_tools(state, called_signatures):
                    results.append(result)
                    tools_used.append(result.tool)
                    stats = getattr(result, "_cache_info", None)
                    if stats:
                        cache_stats.update(stats)

            # Truncate oversized results
            for r in results:
                self._runtime.truncate(r)

            # 统一构建 Evidence 列表（§1 约定：source_tool / timestamp 语义）
            from app.research.evidence import build_evidence

            evidence_items = build_evidence(results, id_prefix=self.category)

            return {
                "results": results,
                "findings": [
                    {
                        "analyst": self.category,
                        "digest": self._make_digest(tools_used, results),
                        "tools_used": tools_used,
                        "failed": False,
                    }
                ],
                "evidence": evidence_items,
                "cache_stats": cache_stats,
                "errors": [],  # always present — reducer appends on merge
            }

        except Exception as exc:
            logger.warning("Analyst %s failed: %s", self.category, exc)
            return {
                "errors": [f"{self.category} analysis failed: {exc}"],
                "findings": [
                    {
                        "analyst": self.category,
                        "digest": f"分析失败: {exc}",
                        "tools_used": [],
                        "failed": True,
                    }
                ],
            }

    # ------------------------------------------------------------------ #
    # Stock-ID helpers — extract from question or plan                      #
    # ------------------------------------------------------------------ #

    def _extract_stocks(self, state: dict) -> list[str]:
        """从问题文本 / 研究计划中抽取 A 股 6 位数字代码。"""
        question = state.get("question", "")
        if not isinstance(question, str):
            question = ""

        # ① 正则匹配 6 位纯数字（"贵州茅台600519"/"股票代码000001"）
        # 优先精确匹配，避免把长串编号误认成代码
        candidates: list[str] = []
        for match in re.finditer(r"(?<!\d)(\d{6})(?!\d)", question):
            code = match.group(1)
            if code not in candidates:
                candidates.append(code)

        # ② 查找 supervisor route 中各 assignment 的 tool_calls.arguments.symbol
        route = state.get("route")
        if isinstance(route, list):
            for assignment in route:
                if isinstance(assignment, dict) and "tool_calls" in assignment:
                    for tc in assignment["tool_calls"]:
                        sym = (tc.get("arguments") or {}).get("symbol")
                        if sym and isinstance(sym, str) and len(sym) >= 6 and sym[-6:].isdigit():
                            if sym[-6:] not in candidates:
                                candidates.append(sym[-6:])

        return candidates

    async def _execute_tools(
        self,
        state: dict,
        called_signatures: set[str],
    ) -> list[ToolResult]:
        """只执行 supervisor route 中分配给本 analyst 的 tool_calls。

        子类可覆盖以注入特定的领域默认参数。
        """
        from app.gateway.tool_registry import resolve_tool

        route = state.get("route") or []
        mine = next((a for a in route if a.get("analyst") == self.category), None)
        if mine is None or not mine.get("tool_calls"):
            logger.info("%s: no assignment from supervisor, skip", self.category)
            return []

        stocks = self._extract_stocks(state)
        results: list[ToolResult] = []
        budget = int(mine.get("budget") or len(mine["tool_calls"]))
        # T27：route 里重复的 (tool, arguments) 在这里真正跳过，
        # 不再依赖下游 execute 的签名集合（那只挡得住"已成功执行过"的）。
        seen: set[tuple[str, tuple]] = set()
        # T23：整次调查的预算按"剩余预算 ÷ 剩余工具数"均分给每个工具，
        # 下发到 gateway 客户端做重试预算感知（超预算返回 error ToolResult，不炸链）。
        budget_seconds = float(getattr(self.settings, "research_budget_seconds", 0) or 0)
        run_started = time.monotonic()
        total_calls = len(mine["tool_calls"])

        for idx, tc in enumerate(mine["tool_calls"]):
            if budget <= 0:
                break
            budget -= 1
            tool_key = tc.get("tool_key", "")
            try:
                meta = resolve_tool(tool_key)
            except Exception:
                logger.warning("%s: unknown tool_key %s, skip", self.category, tool_key)
                continue

            arguments = dict(tc.get("arguments") or {})
            # symbol 守卫：非白名单工具且 planner 没给 symbol → 用问题里抽到的代码补，仍无则跳过
            if meta.tool_name not in self.WHITELIST_NO_SYMBOL and not arguments.get("symbol"):
                if stocks:
                    arguments["symbol"] = ";".join(stocks)
                else:
                    # T8：跳过必须可见（旧实现是 debug，功能没跑却看不出）。
                    logger.warning("%s 跳过 %s（缺少 symbol 且问题中无 6 位代码）", self.category, tool_key)
                    continue

            dedup_key = (meta.tool_name, tuple(sorted(arguments.items())))
            if dedup_key in seen:
                logger.warning("%s 跳过重复 tool_call %s（同签名已在本次 route 中出现）", self.category, tool_key)
                continue
            seen.add(dedup_key)

            deadline = None
            if budget_seconds > 0:
                remaining_budget = budget_seconds - (time.monotonic() - run_started)
                remaining_tools = max(total_calls - idx, 1)
                deadline = time.monotonic() + max(remaining_budget / remaining_tools, 0.0)

            results.append(
                await self._runtime.execute(meta.tool_name, arguments, called_signatures, deadline=deadline)
            )

        return results

    def _build_arguments(
        self,
        meta,
        domain: str | None,
        stocks: list[str] | None = None,
    ) -> dict:
        """根据工具和当前域构建调用参数。"""
        tool_name = meta.tool_name
        arguments: dict[str, Any] = {}

        # 对于需要 symbol 的工具（不在白名单），若已知 stock codes → 全部传入。
        if tool_name not in self.WHITELIST_NO_SYMBOL and stocks:
            arguments["symbol"] = ";".join(stocks)

        # 特定工具的额外参数：
        if tool_name == "longhu_xueqiu_longhu_get":
            # 龙虎榜可以传 page，但 symbol 可选（全市场榜单）；有股票时只查该股
            pass  # symbol 已在上面设置
        elif tool_name == "finance_eastmoney_f10_finance_get":
            arguments["periods"] = getattr(meta, "periods_hint", 8)

        return arguments

    def _make_digest(self, tools_used: list[str], results: list[ToolResult]) -> str:
        """生成 ≤200 字的执行摘要，供 Reasoning 引用。"""
        successful = sum(1 for r in results if r.status == STATUS_SUCCESS)
        partial = sum(1 for r in results if r.status == STATUS_PARTIAL)
        failed = sum(1 for r in results if r.status == STATUS_ERROR)
        parts = [f"{self.category}: 执行了 {len(tools_used)} 个工具"]
        if successful:
            parts.append(f"{successful} 成功")
        if partial:
            parts.append(f"{partial} 部分数据")
        if failed:
            parts.append(f"{failed} 失败")
        return ", ".join(parts)[:200]
