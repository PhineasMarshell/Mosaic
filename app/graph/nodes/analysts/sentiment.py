"""舆情情绪分析员 — 执行 Supervisor 分配的 sentiment 类工具。

A0 版：仅接入雪球 list_stock_discussions（xq_discussions），
对评论区帖子做 LLM 情感极性打分 + 关键主题提取，
产出 Evidence 条目 + finding digest。

流程：
1. 从 route 获取分配给 category="sentiment" 的 tool_calls，调用父类骨架执行
   （MCP list_stock_discussions → 原始评论帖子列表）
2. 截断至 SENTIMENT_MAX_COMMENTS 条，每条文本再截断至 200 字
3. 构造 LLM prompt → 单次调用 LLM 产出情感分布 + 主题标签
4. 拆为 Evidence 条目（sentiment_bullish / sentiment_bearish / sentiment_neutral
   + sentiment_score 共 4 条）
5. 生成 ≤200 字 finding digest

降级策略（见 docs/sentiment-analyst-plan.md §6）：
- MCP 返回空（无评论）→ status=success，digest "sentiment: 无评论数据"
- MCP 调用失败 → 基类已记 error evidence，digest 标记失败，不阻塞推理
- LLM 超时/解析失败 → 回退规则统计（帖子数 + 时间分布），digest 注明降级
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from openai import AsyncOpenAI

from app.config import Settings
from app.graph.nodes.analysts.base import MarketAnalystNode
from app.llm_json import parse_json_object
from app.models.market import ToolResult

logger = logging.getLogger(__name__)

#: 单条评论文本进 prompt 的截断长度（token 预算防线，计划 §6）
_POST_TEXT_CLIP = 200


class SentimentAnalystNode(MarketAnalystNode):
    """舆情情绪分析员节点。"""

    category = "sentiment"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        # 情感分析 LLM client：与 reasoning/critic 共用同一配置
        # （settings.openai_api_key / settings.openai_base_url）
        self.client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=settings.llm_timeout_seconds,
        )

    async def __call__(self, state):
        """LangGraph 节点入口：父类执行工具后追加 LLM 情感分析步骤。

        返回结构与基类一致（results / findings / evidence / cache_stats / errors），
        只是 evidence 里多了本节点产出的 sentiment_* 条目、digest 换成情感摘要。
        """
        if hasattr(state, "model_dump"):
            state = state.model_dump(exclude_none=False)

        tools_used: list[str] = []
        results: list[ToolResult] = []
        cache_stats: dict[str, int] = {}
        called_signatures: set[str] = set()

        try:
            # ── ① 工具执行（复用基类 _execute_tools，不改通用骨架）──
            async with self._runtime.gateway_session():
                for result in await self._execute_tools(state, called_signatures):
                    results.append(result)
                    tools_used.append(result.tool)
                    stats = getattr(result, "_cache_info", None)
                    if stats:
                        cache_stats.update(stats)

            for r in results:
                self._runtime.truncate(r)

            from app.graph import run_log

            run_log.log_executions(state, results, category=self.category)

            # ── ② 提取评论帖子（按 SENTIMENT_MAX_COMMENTS 硬截断）──
            posts = self._extract_posts(results)

            # ── ③ LLM 情感分析（失败降级为规则统计）──
            analysis: dict[str, Any] | None = None
            degraded = False
            if posts:
                try:
                    analysis = await self._analyze_sentiment(posts)
                except Exception as exc:  # noqa: BLE001 — LLM 失败不炸图
                    logger.warning("sentiment: LLM 分析异常，降级为规则统计: %s", exc)
                if analysis is None:
                    degraded = True

            # ── ④ 基类统一构建 Evidence（原始 datum 照常入账），再追加 sentiment 条目 ──
            from app.research.evidence import build_evidence

            evidence_items = build_evidence(results, id_prefix=self.category)
            if analysis is not None:
                evidence_items.extend(self._build_sentiment_evidence(analysis, results, len(posts)))

            return {
                "results": results,
                "findings": [
                    {
                        "analyst": self.category,
                        "digest": self._make_digest(tools_used, results, posts, analysis, degraded),
                        "tools_used": tools_used,
                        "failed": False,
                    }
                ],
                "evidence": evidence_items,
                "cache_stats": cache_stats,
                "errors": [],
            }

        except Exception as exc:
            logger.warning("Analyst %s failed: %s", self.category, exc)
            return {
                "errors": [f"{self.category} analysis failed: {exc}"],
                "findings": [
                    {
                        "analyst": self.category,
                        "digest": f"分析失败: {exc}",
                        "tools_used": tools_used,
                        "failed": True,
                    }
                ],
            }

    # ------------------------------------------------------------------ #
    # 评论提取                                                            #
    # ------------------------------------------------------------------ #

    def _extract_posts(self, results: list[ToolResult]) -> list[dict[str, Any]]:
        """从工具结果的 raw 中提取评论帖子 [{text, created_at}]。

        实测（2026-07-21，iiix 0.8.4 + market-gateway）返回结构：
          {"category":..., "count":..., "items":[{...},...], "max_page":..., ...}
          每个 item: {author, id, like_count, published_at(ms), text, title, url, ...}
        这里对常见容器键与文本/时间字段名做宽容匹配（兼容未来结构变化）。
        """
        max_comments = int(getattr(self.settings, "sentiment_max_comments", 500) or 500)
        posts: list[dict[str, Any]] = []
        for result in results:
            if result.status == "error":
                continue
            payload = result.raw
            items: list[Any] = []
            if isinstance(payload, list):
                items = payload
            elif isinstance(payload, dict):
                for key in ("discussions", "items", "data", "posts", "list"):
                    candidate = payload.get(key)
                    if isinstance(candidate, list):
                        items = candidate
                        break
            for item in items:
                if not isinstance(item, dict):
                    continue
                text = None
                for field in ("text", "description", "title", "content"):
                    value = item.get(field)
                    if isinstance(value, str) and value.strip():
                        text = value.strip()
                        break
                if not text:
                    continue
                # 实测返回 published_at（Unix 毫秒时间戳）；兼容常见别名
                created_at = item.get("created_at") or item.get("published_at") or item.get("time") or item.get("created_time")
                posts.append({"text": text[:_POST_TEXT_CLIP], "created_at": str(created_at) if created_at is not None else None})
                if len(posts) >= max_comments:
                    return posts
        return posts

    # ------------------------------------------------------------------ #
    # LLM 情感分析                                                        #
    # ------------------------------------------------------------------ #

    async def _analyze_sentiment(self, posts: list[dict[str, Any]]) -> dict[str, Any] | None:
        """单次 LLM 调用：帖子列表 → 情感分布 JSON。

        Returns:
            解析后的 dict（含 sentiment_score/bullish_ratio/bearish_ratio/neutral_ratio），
            或 None（LLM 不可用 / 输出无法解析——由调用方降级处理）。
        """
        lines = "\n".join(
            f"[{p['created_at'] or 'N/A'}] {p['text']}" if p["created_at"] else p["text"] for p in posts
        )
        prompt = (
            f"你是一个金融市场情绪分析助手。以下是从雪球股票评论区采集的 {len(posts)} 条用户帖子。\n\n"
            "请分析这些帖子的整体情绪状态，返回严格的 JSON：\n\n"
            "{\n"
            '  "sentiment_score": 0.0-1.0（0=极度悲观, 0.5=中性, 1=极度乐观）,\n'
            '  "bullish_ratio": 0.0-1.0（看涨帖子占比）,\n'
            '  "bearish_ratio": 0.0-1.0（看跌帖子占比）,\n'
            '  "neutral_ratio": 0.0-1.0（中性帖子占比）,\n'
            '  "key_themes": ["主题1", "主题2", ...]（5 个以内的关键讨论主题）,\n'
            '  "summary": "一句话概述评论区整体情绪（≤50 字）"\n'
            "}\n\n"
            "帖子列表：\n---\n"
            f"{lines}\n"
            "---"
        )
        response = await self.client.chat.completions.create(
            model=self.settings.openai_model,
            messages=[
                {
                    "role": "system",
                    "content": "你是金融舆情分析助手。必须只返回合法 JSON object，不要包含任何 Markdown 代码块标记、不要加解释文字。",
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        content = response.choices[0].message.content or "{}"
        data = parse_json_object(content, source="SentimentAnalyst")
        # 数值字段清洗：非法值按 0 处理，clamp 到 [0, 1]
        for key in ("sentiment_score", "bullish_ratio", "bearish_ratio", "neutral_ratio"):
            try:
                data[key] = min(max(float(data.get(key, 0.5)), 0.0), 1.0)
            except (TypeError, ValueError):
                data[key] = 0.5
        themes = data.get("key_themes")
        data["key_themes"] = [str(t)[:30] for t in themes][:5] if isinstance(themes, list) else []
        return data

    # ------------------------------------------------------------------ #
    # Evidence 构造                                                       #
    # ------------------------------------------------------------------ #

    def _build_sentiment_evidence(
        self,
        analysis: dict[str, Any],
        results: list[ToolResult],
        n_posts: int,
    ) -> list:
        """情感指标 → 4 条 Evidence（bullish/bearish/neutral/composite score）。

        id 沿用基类账本（build_evidence(id_prefix="sentiment")）的编号续排，
        避免与原始 datum 证据冲突。timestamp 取数据时效（帖子最新时间），
        没有则用当前时刻。
        """
        from app.models.evidence import Evidence

        source_tool = next((r.tool_key or r.tool for r in results if r.status != "error"), "xq_discussions")
        instrument = next((str(r.arguments.get("symbol")) for r in results if r.arguments.get("symbol")), None)
        domain = next((d.domain for r in results for d in r.normalized if d.domain), "a_share")
        timestamp = next(
            (p["created_at"] for p in reversed(self._extract_posts(results)) if p.get("created_at")),
            datetime.now(UTC).isoformat(),
        )

        bullish = float(analysis.get("bullish_ratio", 0))
        bearish = float(analysis.get("bearish_ratio", 0))
        neutral = float(analysis.get("neutral_ratio", 0))
        score = float(analysis.get("sentiment_score", 0.5))
        themes_str = ", ".join(analysis.get("key_themes") or [])
        summary = str(analysis.get("summary") or "")[:80]

        counter_start = self._next_evidence_counter(results)

        def mk(offset: int, metric: str, value: float, note: str):
            return Evidence(
                id=f"sentiment-{counter_start + offset:03d}",
                source_tool=source_tool,
                domain=domain,
                instrument=instrument,
                metric=metric,
                value=value,
                timestamp=timestamp,
                status="success",
                note=note,
            )
        return [
            mk(0, "sentiment_bullish", bullish, f"看涨帖子占比: {bullish:.0%}（样本 {n_posts} 条）"),
            mk(1, "sentiment_bearish", bearish, f"看跌帖子占比: {bearish:.0%}（样本 {n_posts} 条）"),
            mk(2, "sentiment_neutral", neutral, f"中性帖子占比: {neutral:.0%}（样本 {n_posts} 条）"),
            mk(
                3,
                "sentiment_score",
                score,
                f"关键主题: {themes_str or '无'} | {summary}".strip(" |"),
            ),
        ]

    @staticmethod
    def _next_evidence_counter(results: list[ToolResult]) -> int:
        """基类 build_evidence 已用的最大编号 + 1（sentiment 条目续排）。"""
        from app.research.evidence import build_evidence

        existing = build_evidence(results, id_prefix="sentiment")
        used = 0
        for e in existing:
            try:
                used = max(used, int(e.id.rsplit("-", 1)[-1]))
            except (ValueError, IndexError):
                continue
        return used + 1

    # ------------------------------------------------------------------ #
    # Digest                                                              #
    # ------------------------------------------------------------------ #

    def _make_digest(  # type: ignore[override] — 子类扩展签名，调用点在本类内
        self,
        tools_used: list[str],
        results: list[ToolResult],
        posts: list[dict[str, Any]] | None = None,
        analysis: dict[str, Any] | None = None,
        degraded: bool = False,
    ) -> str:
        """情感分析摘要（≤200 字）。

        形态：``sentiment: 雪球评论100条，情绪偏乐观(sentiment_score=0.62,
        看涨45%/看跌30%/中性25%)；热门主题: 财报预期、政策利好、行业竞争``
        """
        if posts is None:
            posts = []
        if not any(r.status != "error" for r in results):
            return super()._make_digest(tools_used, results)
        if not posts:
            return "sentiment: 无评论数据"
        if analysis is None:
            # 降级：规则统计（帖子数 + 时间窗），不发 LLM 结论
            times = [p["created_at"] for p in posts if p.get("created_at")]
            window = f"，时间窗{times[-1]}" if times else ""
            return f"sentiment: 雪球评论{len(posts)}条，LLM 不可用，降级为规则统计{window}"[:200]

        score = float(analysis.get("sentiment_score", 0.5))
        bullish = float(analysis.get("bullish_ratio", 0))
        bearish = float(analysis.get("bearish_ratio", 0))
        neutral = float(analysis.get("neutral_ratio", 0))
        direction = "偏乐观" if score > 0.6 else ("偏悲观" if score < 0.4 else "中性")
        themes = "、".join(analysis.get("key_themes") or []) or "无明显主题"
        text = (
            f"sentiment: 雪球评论{len(posts)}条，情绪{direction}"
            f"(sentiment_score={score:.2f}, 看涨{bullish:.0%}/看跌{bearish:.0%}/中性{neutral:.0%})；"
            f"热门主题: {themes}"
        )
        if degraded:
            text += "[降级]"
        return text[:200]
