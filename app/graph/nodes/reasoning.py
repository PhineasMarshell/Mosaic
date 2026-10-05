"""Reasoning 节点 -- 将工具结果 + Evidence -> 结构化市场情报。

包装 ReasoningEngine.reason() 为 LangGraph 节点。
Critic 打回时把 unsupported_claims 注入 prompt 重写报告。
P2：包含 revise 循环支持（Critic 打回 <= max_revisions 轮时重新生成）。

对应现有代码：app/research/reasoning.py 的 ReasoningEngine。
"""

import logging

from app.config import Settings
from app.errors import LLMOutputError
from app.models.market import ToolResult
from app.models.response import MarketIntelligence
from app.research.reasoning import ReasoningEngine, _get_evidence_key

logger = logging.getLogger(__name__)


class ReasoningNode:
    """LLM 推理节点：证据 -> 结构化市场情报报告。"""

    def __init__(self, settings: Settings, *, critic_max_revisions: int = 2):
        self.settings = settings
        self.critic_max_revisions = critic_max_revisions
        self._engine = ReasoningEngine(settings)

    async def __call__(self, state):
        """运行推理引擎，返回 MarketIntelligence report。

        如果 state.report 已存在（来自上一轮），说明是 Critic 打回的修订请求，
        将 unsupported_claims 注入 prompt 上下文。
        """
        try:
            # Normalize state to dict (handle both ResearchState and dict)
            if hasattr(state, "model_dump"):
                state = state.model_dump(exclude_none=False)

            # 准备 history context（Critic 打回时的缺口反馈）
            critique = state.get("critique")
            unsupported_claims = []
            if critique:
                if isinstance(critique, dict):
                    raw_claims = critique.get("unsupported_claims", [])
                else:
                    raw_claims = getattr(critique, "unsupported_claims", [])
                unsupported_claims = list(raw_claims or [])

            question = state.get("question", "") if isinstance(state, dict) else getattr(state, "question", "")

            # ── 证据截断 — 防止超大证据集超出 LLM 上下文窗口 ──
            # Evidence 条目过多时，按工具分组，每组只保留最新 MAX_PER_TOOL 条
            MAX_EVIDENCE_PER_TOOL = 80
            MAX_TOTAL_EVIDENCE = 500
            evidence_items = state.get("evidence", [])
            if len(evidence_items) > MAX_TOTAL_EVIDENCE:
                tool_groups: dict[str, list] = {}
                for i, item in enumerate(evidence_items):
                    key = _get_evidence_key(item)
                    entry = {"idx": i, "item": item}
                    tool_groups.setdefault(key, []).append(entry)
                truncated = []
                for entries in tool_groups.values():
                    # 按 idx 倒序取最新条目
                    entries.sort(key=lambda e: e["idx"], reverse=True)
                    truncated.extend(e["item"] for e in entries[:MAX_EVIDENCE_PER_TOOL])
                # 截到最大总数
                evidence_items = truncated[:MAX_TOTAL_EVIDENCE]

            results = state.get("results", [])
            # LangGraph reducer merge 将 ToolResult 序列化为 dict，需要转回
            results: list[ToolResult] = [ToolResult(**r) if isinstance(r, dict) else r for r in results]
            # 使用截断后的 evidence_items，并将 reducer 序列化产生的 dict 转回 Evidence
            from app.models.evidence import Evidence

            evidence = [Evidence(**e) if isinstance(e, dict) else e for e in evidence_items]

            # 调用推理引擎
            report: MarketIntelligence = await self._engine.reason(
                question=question,
                results=results,
                evidence=evidence,
                history_context=self._build_revision_context(unsupported_claims),
                findings=state.get("findings", []),
            )

            # T15/D2：消费 Evidence Gate 的结论 —— has_evidence=False 时
            # **降级但不短路**：报告照常产出，强制 confidence=low + data_caveats
            # + errors。旧实现 has_evidence 全仓无消费方，零证据也能产出
            # 正常置信度的报告。LLM/下游不能绕过此检查结果。
            gate = state.get("gate")
            gate_has_evidence = None
            if gate is not None:
                if isinstance(gate, dict):
                    gate_has_evidence = gate.get("has_evidence")
                else:
                    gate_has_evidence = getattr(gate, "has_evidence", None)

            gate_errors: list[str] = []
            if gate_has_evidence is False:
                report.confidence = "low"
                report.data_caveats.append("证据链为空，结论不可作为依据")
                gate_errors.append("Evidence gate: no evidence collected; report is unverified")
                logger.warning("Evidence gate: has_evidence=False, report degraded to confidence=low")

            logger.info(
                "Reasoning completed: confidence=%s revisions=%d",
                report.confidence,
                state.get("revision_count", 0),
            )

            # revision_count 只在"修订"（已存在上一轮 report）时递增，
            # 使 critic_max_revisions 精确表示允许的修订次数，而非总执行次数。
            is_revision = state.get("report") is not None
            out = {
                "report": report,
                "revision_count": (state.get("revision_count") or 0) + (1 if is_revision else 0),
            }
            if gate_errors:
                out["errors"] = gate_errors
            return out
        except LLMOutputError as exc:
            logger.warning("Reasoning LLM output error: %s", exc)
            return {
                "report": None,
                "errors": [f"Reasoning engine failed: {exc}"],
            }
        except Exception as exc:
            logger.exception("Reasoning node unexpected error")
            return {
                "report": None,
                "errors": [f"Reasoning node failed: {exc}"],
            }

    def _build_revision_context(self, unsupported_claims: list[str]) -> str:
        """构建修订上下文（Critic 指出的不支持表述列表）。

        供 ReasoningEngine 在重写报告时参考。不在 reason() 签名中暴露，
        因为旧版本 ReasoningEngine.history_context 只接受字符串。
        """
        if not unsupported_claims:
            return ""
        lines = [
            "CRITIC REVISE REQUEST:",
            "The following claims in your previous report are NOT supported by evidence:",
        ]
        for claim in unsupported_claims:
            lines.append(f"  - {claim}")
        lines.append("")
        lines.append("Please rewrite those sections using ONLY verified evidence.")
        return "\n".join(lines)
