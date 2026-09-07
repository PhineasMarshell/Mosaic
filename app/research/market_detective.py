"""Market Detective — MVP 主入口。

职责：
- 规划研究路径
- 执行工具调用（支持多轮循环）
- 聚合证据、生成情报报告

核心工作流（第一版 MVP）::

    用户问题
      → Planner (生成初始计划)
      → Tool 执行
      → Evidence Gate (代码级检查)
      → Evaluator (判断证据是否足够)
         ├─ sufficient=True   → Reasoning Engine → 输出报告
         ├─ insufficient+next_steps → 继续调 Tool → 回到 Evidence Gate
         └─ stop              → Reasoning Engine → 输出报告（注明数据缺口）
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.agent.evidence_gate import EvidenceGateResult, run_evidence_gate
from app.agent.evaluator import EvidenceEvaluator
from app.agent.planner import Planner
from app.cache import Cache, _make_cache_key, _resolve_ttl, market_cache
from app.config import Settings
from app.detector.anomaly import detect_anomalies
from app.gateway.http_client import MarketGatewayHttpClient
from app.gateway.mcp_client import MarketGatewayClient
from app.gateway.tool_registry import ALL_TOOLS, BY_NAME, BY_KEY, resolve_tool_by_name
from app.models.market import ToolResult
from app.models.research import MarketDomain, ToolCallPlan
from app.models.response import ResearchResponse
from app.research.evidence import build_evidence
from app.research.reasoning import ReasoningEngine
from app.memory.storage import MarketMemory

logger = logging.getLogger(__name__)


def _default_date_range(days_back: int = 90) -> tuple[str, str]:
    """返回 (start_str, end_str) 格式为 YYYY-MM-DD。"""
    end_dt = datetime.now(UTC)
    start_dt = end_dt - timedelta(days=days_back)
    return start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")


def _normalize_symbol(symbol: str, domain: str) -> str:
    """规范化标的符号，确保后端校验通过。

    - A 股：纯数字指数代码加 sh/sz 前缀（如 "000300" → "sh000300"）
    - HK 股：裸码补上 HK 前缀（如 "00700" → "hk00700"）
    - Crypto：ccxt 统一写法（现货 BTC/USDT，永续 BTC/USDT:USDT）
    - Commodities：OKX 永续合约 XAU/USDT:USDT（见 commodities 兜底逻辑）
    """
    if not symbol:
        return symbol

    # A 股：纯数字 → 推断交易所加前缀
    if domain == "a_share" and symbol.isdigit():
        from app.gateway.stock_codes import _infer_index_exchange
        return f"{_infer_index_exchange(symbol)}{symbol}"

    # A 股：已带前缀的不动（如 SH600519, SZ000858）
    if domain == "a_share" and any(symbol.lower().startswith(pfx) for pfx in ("sh", "sz", "bj")):
        return symbol

    # HK 股：裸码 → 加 hk 前缀
    if domain == "hk_stock" and len(symbol) <= 5 and symbol.isdigit():
        return f"hk{symbol}"

    return symbol


def _merge_args_with_defaults(
    existing: dict,
    defaults: dict,
) -> dict:
    """将已有参数与默认值智能合并。

    优先级规则：
      - 已有参数的值非空 → 保留（LLM 明确指定的意图）
      - 已有参数的值为空字符串 / None → 使用默认值兜底
      - 已有参数缺少的 key → 从默认值补齐

    这样处理了两种边缘情况：
      1. arguments == {}       → 全部取默认值
      2. arguments == {"symbol": ""} → 空值换为默认值，其余 key 仍按此逻辑补全
    """
    if not existing:
        return dict(defaults)
    merged: dict = {}
    for key, default_value in defaults.items():
        if key in existing and existing[key]:
            merged[key] = existing[key]
        else:
            merged[key] = default_value
    # 保留原有但不在默认集中的额外参数（例如 LLM 额外加了 interval）
    for key, value in existing.items():
        if key not in defaults and value:
            merged[key] = value
    return merged


@dataclass(frozen=True)
class _ToolCallRequest:
    """待执行的工具调用请求。"""

    tool_name: str
    arguments: dict
    purpose: str


def _resolve_tool_ref(ref: str) -> tuple[ToolMeta, str] | None:
    """从 Planner 输出的任意引用（key 或 operationId）解析出 ToolMeta。

    返回 (ToolMeta, resolved_business_key)，或 None。
    """
    # 直接尝试作为 business key 解析
    if ref in BY_KEY:
        return BY_KEY[ref], ref

    # 尝试作为 operationId 解析
    if ref in BY_NAME:
        return BY_NAME[ref], BY_NAME[ref].key

    return None


class MarketDetective:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.planner = Planner(settings)
        self.evaluator = EvidenceEvaluator(settings)
        self.reasoning = ReasoningEngine(settings)

    def _gateway_class(self):
        mode = self.settings.market_gateway_mode.lower().strip()
        if mode == "mcp":
            return MarketGatewayClient
        if mode == "http":
            return MarketGatewayHttpClient
        raise ValueError(
            f"Unsupported MARKET_GATEWAY_MODE={self.settings.market_gateway_mode!r}; "
            "expected 'mcp' or 'http'"
        )

    def _http_allowed_tools(self):
        from app.gateway.tool_registry import ALL_TOOLS
        return ALL_TOOLS

    async def investigate(
        self, question: str, domain: MarketDomain | None = None,
        conversation_id: str | None = None,
    ) -> ResearchResponse:
        # ---------------------------------------------------------
        # Step 1: 规划初始研究计划（如指定了 domain，传递给 planner）
        # ---------------------------------------------------------

        # 加载对话历史（供 Planner 感知上下文）
        conv_history = ""
        if conversation_id:
            memory = MarketMemory()
            conv_history = memory.get_conversation_history(
                conversation_id, self.settings.max_conversation_turns
            )

        plan = await self.planner.plan(question, conversation_history=conv_history)
        # 如果显式指定了域且 planner 自动判断不一致，以显式指定为准
        if domain is not None:
            plan.intent.domain = domain

        # ---------------------------------------------------------
        # Step 1.5: 兜底逻辑 —— 对所有域，确保通用工具带正确参数
        # ---------------------------------------------------------
        target_domain = plan.intent.domain

        # 解析已有步骤的实际 business key
        existing_business_keys: set[str] = set()
        existing_steps_raw: dict[str, ToolCallPlan] = {}
        for step in plan.steps:
            resolved = _resolve_tool_ref(step.tool_key)
            if resolved:
                _, bkey = resolved
                existing_business_keys.add(bkey)
                existing_steps_raw[bkey] = step

        # 跨域通用工具的默认参数（按 OpenAPI spec 校验）
        # ⚠️ 所有值必须是非空的——后端 422 校验会拒绝空字符串
        # ⚠️ /market/snapshot 对 Commodities 使用 OKX（XAU/USDT:USDT 永续合约）
        #   → 其他非 Crypto 域（A 股/HK）仍不适用 snapshot
        # 注意各端口的实际参数名：
        #   quote             : GET query, symbol=single_string (非复数、非数组)
        #   search (雪球)     : GET query, q=string (非 keyword)
        #   abnormal_reasons  : GET query, symbol+可选 start/end
        #   eastmoney F10     : GET query, symbol=stock_code_6位数字(不含 SH/SZ 前缀)
        #   klines            : POST body, 需 symbol+interval+start+end(exchange 可选)
        _start, _end = _default_date_range()

        if target_domain == "commodities":
            # Commodity → OKX 黄金永续合约: XAU/USDT:USDT
            # 注：Binance 等主流所无实物黄金交易对；OKX 提供 XAU-USDT-SWAP
            #     ccxt 统一写法为 XAU/USDT:USDT (okx/bybit/aster)
            _cross_tool_defaults: dict[str, dict[str, Any]] = {
                "klines":           {"symbol": "XAU/USDT:USDT", "exchange": "okx", "interval": "1d", "start": _start, "end": _end},
                "snapshot":         {"symbol": "XAU/USDT:USDT", "exchange": "okx"},
            }
        elif target_domain == "hk_stock":
            # HK klines → tencent 数据源，symbol 带 hk 前缀
            _cross_tool_defaults = {
                "klines":           {"symbol": "hk00700", "exchange": "tencent", "interval": "1d", "start": _start, "end": _end},
                "quote":            {"symbol": "hk00700"},
                "search":           {"q": "腾讯控股"},
                "abnormal_reasons": {"symbol": "hk00700"},
            }
        elif target_domain == "a_share":
            # A 股指数的 symbol 需带交易所前缀（如 sh000300），且 klines 必须指定 exchange=tencent
            _cross_tool_defaults = {
                "klines":           {"symbol": "sh000300", "exchange": "tencent", "interval": "1d", "start": _start, "end": _end},
                "quote":            {"symbol": "sh000300"},
                "search":           {"q": "沪深300"},
                "abnormal_reasons": {"symbol": "sh000300"},
            }
        else:  # crypto / default
            # Crypto: Binance 原生合约名用 "BTCUSDT" 而非 "BTC/USDT"
            # derivatives_history 需要 symbol+start+end（Binance 原生格式）
            _cross_tool_defaults = {
                "klines":         {"symbol": "BTC/USDT", "interval": "1d", "start": _start, "end": _end},
                "snapshot":       {"symbol": "BTC/USDT"},
                "search":         {"q": "Bitcoin"},
                "abnormal_reasons": {"symbol": "BTCUSDT"},
                "derivatives_history": {"symbol": "BTCUSDT", "exchange": "binance", "start": _start, "end": _end},
                # quote_tencent_quote_get 是腾讯 API，不适用于 Crypto，不添加默认参数
            }

        # 清理：删除 Planner 生成的空参数步骤（让兜底的正确版本来执行）
        plan.steps = [
            step for step in plan.steps
            if not (
                _resolve_tool_ref(step.tool_key) and
                not step.arguments
            )
        ]

        # 对每个跨域通用工具：
        # 1. 如果没出现，补上
        # 2. 如果出现但参数有缺失 / 值为空，用默认值智能补齐
        for tool_key, default_args in _cross_tool_defaults.items():
            if tool_key not in existing_business_keys:
                plan.steps.insert(0, ToolCallPlan(
                    tool_key=tool_key,
                    arguments=default_args,
                    purpose=f"获取{target_domain}行情数据",
                    priority="high",
                ))
            elif tool_key in existing_steps_raw:
                step = existing_steps_raw[tool_key]
                step.arguments = _merge_args_with_defaults(
                    step.arguments or {},
                    default_args,
                )
                # 按域规范化 symbol 参数（裸码 → 前缀格式）
                if "symbol" in step.arguments:
                    step.arguments["symbol"] = _normalize_symbol(
                        step.arguments["symbol"], target_domain
                    )

        # 记录最终计划（用于调试）
        logger.info(
            "Final plan: %d steps, domain=%s, keys=%s",
            len(plan.steps), target_domain,
            [s.tool_key for s in plan.steps],
        )
        # 打印每个步骤的参数，便于排查 422 报错
        for step in plan.steps:
            logger.debug("Plan step %s → args=%s", step.tool_key, step.arguments)

        logger.info(
            "Planner produced %d steps for task=%s (domain=%s)",
            len(plan.steps), plan.intent.task, plan.intent.domain,
        )

        # ---------------------------------------------------------
        # Step 2: 将 Plan 转换为待执行队列
        # ---------------------------------------------------------

        todo_queue: list[_ToolCallRequest] = []
        for step in plan.steps:
            resolved = _resolve_tool_ref(step.tool_key)
            if resolved is None:
                logger.warning("Planner referenced unknown tool: %s", step.tool_key)
                continue
            meta, _business_key = resolved
            todo_queue.append(_ToolCallRequest(
                tool_name=meta.tool_name,
                arguments=step.arguments or {},
                purpose=step.purpose,
            ))

        # ---------------------------------------------------------
        # Step 3: 执行循环（支持 Evaluator 动态追加）
        # ---------------------------------------------------------

        results: list[ToolResult] = []
        called_signatures: set[str] = set()
        total_calls = 0

        while todo_queue:
            # 安全限制
            if total_calls >= self.settings.max_tool_calls:
                logger.warning(
                    "Hit max_tool_calls=%d, stopping early",
                    self.settings.max_tool_calls,
                )
                break

            req = todo_queue.pop(0)
            signature = f"{req.tool_name}:{sorted(req.arguments.items())}"

            # 去重 + 缓存命中
            cached_result = self._check_cache(req, called_signatures)
            if cached_result is not None:
                results.append(cached_result)
                logger.info("Cache hit for %s (%s)", req.tool_name, req.purpose)
                continue

            # ── Debug: 记录实际发送的参数（方便排查 422） ──
            logger.debug("Tool call %s → args=%s", req.tool_name, req.arguments)

            # 执行单工具调用
            result = await self._execute_one(req, called_signatures)
            results.append(result)
            called_signatures.add(signature)
            total_calls += 1

            logger.info(
                "Called %s (%s) — status=%s partial=%s",
                result.tool,
                req.purpose,
                result.status,
                result.partial,
            )

            # --------------------------------------------------
            # 每两轮工具调用后做一次评估，避免盲目堆砌
            # （如果刚达到上限就跳过评估）
            # --------------------------------------------------
            if (total_calls % 2 == 0
                    and total_calls < self.settings.max_tool_calls):
                gate = run_evidence_gate(results)
                evidence = build_evidence(results)

                decision = await self.evaluator.evaluate(
                    question=question,
                    results=results,
                    evidence=evidence,
                    gate=gate,
                    called_tools=[r.tool for r in results],
                    domain=plan.intent.domain,
                )

                logger.info(
                    "Evaluator: sufficient=%s quality=%s action=%s",
                    decision.sufficient,
                    decision.evidence_quality,
                    decision.recommended_next_action,
                )

                if decision.sufficient:
                    # 证据已经充分，不再追加
                    pass
                elif decision.recommended_next_action == "stop":
                    # 没有更多合理工具可调用
                    break
                else:
                    # 将 Evaluator 推荐的下一步加入队列
                    for step in decision.next_steps:
                        resolved = _resolve_tool_ref(step.tool_key)
                        if resolved is None:
                            logger.warning(
                                "Evaluator recommended unknown tool: %s",
                                step.tool_key,
                            )
                            continue
                        meta, _business_key = resolved
                        todo_queue.append(_ToolCallRequest(
                            tool_name=meta.tool_name,
                            arguments=step.arguments or {},
                            purpose=step.purpose,
                        ))

        # ---------------------------------------------------------
        # Step 4: 获取历史上下文 + 最终证据门控 + 推理生成
        # ---------------------------------------------------------

        final_gate = run_evidence_gate(results)
        evidence = build_evidence(results)

        logger.info(
            "Final gate: has_evidence=%s success=%d partial=%d error=%d",
            final_gate.has_evidence,
            len(final_gate.successful_tools),
            len(final_gate.partial_tools),
            len(final_gate.error_tools),
        )

        # 注入历史上下文到 Reasoning Engine（对话历史 + 市场状态历史）
        memory = MarketMemory()
        market_state_history = memory.get_context_for_question(question, days_back=7)

        if conv_history:
            history_context = f"{conv_history}\n\n{market_state_history}" if market_state_history else conv_history
        else:
            history_context = market_state_history

        report = await self.reasoning.reason(
            question, results, evidence, history_context=history_context
        )

        # 服务端兜底：模型不能伪造"没调用的工具"。
        report.used_tools = [r.tool for r in results]

        # ── 异常检测（PRD §29-30: Anomaly Radar） ──
        try:
            anomalies = detect_anomalies(results, target_domain)
            if anomalies:
                report.anomalies = [a.to_dict() for a in anomalies]
                logger.info("Anomaly detector found %d anomalies", len(anomalies))
        except Exception as exc:
            logger.debug("Anomaly detection failed (non-fatal): %s", exc)

        return ResearchResponse(
            question=question,
            report=report,
            tool_results=[
                {
                    "tool": r.tool,
                    "arguments": r.arguments,
                    "status": r.status,
                    "partial": r.partial,
                    "error": r.error,
                }
                for r in results
            ],
            # 缓存统计（方便调试）
            cache_stats=market_cache.stats,
        )

    async def _execute_one(
        self,
        req: _ToolCallRequest,
        called_signatures: set[str],
    ) -> ToolResult:
        """执行单次工具调用，自动处理重复和连接。"""

        gateway_cls = self._gateway_class()

        async with gateway_cls(self.settings) as gateway:
            available = (
                {tool.name for tool in gateway.tools}
                if self.settings.market_gateway_mode.lower() == "mcp"
                else {meta.tool_name for meta in self._http_allowed_tools()}
            )

            if req.tool_name not in available:
                return ToolResult(
                    tool=req.tool_name,
                    arguments=req.arguments,
                    status="error",
                    normalized=[],
                    error=f"Tool not available: {req.tool_name}",
                )

            sig = f"{req.tool_name}:{sorted(req.arguments.items())}"
            if sig in called_signatures:
                return ToolResult(
                    tool=req.tool_name,
                    arguments=req.arguments,
                    status="error",
                    normalized=[],
                    error="Duplicate call blocked",
                )

            result = await gateway.call(req.tool_name, req.arguments)

            # 缓存成功结果（用于后续去重）
            cache_key = _make_cache_key(result.tool, result.arguments)
            ttl = _resolve_ttl(result.tool)
            market_cache.set(cache_key, result, ttl=ttl)

            return result

    def _check_cache(
        self,
        req: _ToolCallRequest,
        called_signatures: set[str],
    ) -> ToolResult | None:
        """检查缓存是否命中。"""
        signature = f"{req.tool_name}:{sorted(req.arguments.items())}"
        if signature in called_signatures:
            return None

        cache_key = _make_cache_key(req.tool_name, req.arguments)
        cached = market_cache.get(cache_key)
        if cached is not None:
            called_signatures.add(signature)
            return cached
        return None
