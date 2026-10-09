"""Reasoning 节点 -- 将工具结果 + Evidence -> 结构化市场情报。

包装 ReasoningEngine.reason() 为 LangGraph 节点。
Critic 打回时把 unsupported_claims 注入 prompt 重写报告。
P2：包含 revise 循环支持（Critic 打回 <= max_revisions 轮时重新生成）。

对应现有代码：app/research/reasoning.py 的 ReasoningEngine。
"""

import json
import logging

from app.config import Settings
from app.errors import LLMOutputError
from app.models.market import ToolResult
from app.models.response import MarketIntelligence
from app.research.reasoning import ReasoningEngine, _get_evidence_key

logger = logging.getLogger(__name__)


def _field(item, key, default=None):
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _select_evidence(items: list, previous_report, *, total_limit: int = 500, per_tool: int = 80) -> tuple[list, dict]:
    if len(items) <= total_limit:
        return items, {"kept": len(items), "omitted": 0, "reason": "within_limit"}
    referenced = {
        str(eid)
        for claim in (_field(previous_report, "claims", []) or [])
        for eid in (_field(claim, "evidence_ids", []) or [])
    }
    referenced.update(str(_field(e, "id")) for e in (_field(previous_report, "evidence", []) or []))
    selected: set[int] = set()
    per_source: dict[str, int] = {}
    reasons = {"report_reference": 0, "market_minimum": 0, "tool_quota": 0}

    def take(index: int, reason: str, *, force: bool = False) -> None:
        if index in selected or (len(selected) >= total_limit and not force):
            return
        tool = _get_evidence_key(items[index])
        if not force and per_source.get(tool, 0) >= per_tool:
            return
        selected.add(index)
        per_source[tool] = per_source.get(tool, 0) + 1
        reasons[reason] += 1

    for i, item in enumerate(items):
        if str(_field(item, "id")) in referenced:
            take(i, "report_reference", force=True)
    for i, item in enumerate(items):
        metric = str(_field(item, "metric", "") or "").lower()
        tool = _get_evidence_key(item).lower()
        if any(x in metric for x in ("up_count", "down_count", "涨跌", "涨停", "timestamp", "date")) or any(
            x in tool for x in ("quote", "sentiment", "limit_up", "telegraph")
        ) or _field(item, "partial") or _field(item, "note"):
            take(i, "market_minimum")
    for i in range(len(items) - 1, -1, -1):
        take(i, "tool_quota")
    selected_items = [item for i, item in enumerate(items) if i in selected]
    return selected_items, {"kept": len(selected_items), "omitted": len(items) - len(selected_items),
                            "reason": "total_and_per_tool_limit", "kept_reasons": reasons}


class ReasoningNode:
    """LLM 推理节点：证据 -> 结构化市场情报报告。"""

    def __init__(self, settings: Settings, *, max_rewrites: int | None = None):
        self.settings = settings
        #: 阶段 6：本节点只负责 `revise → reasoning` 这一条路径的计数上限
        #: （research_more 的额度由 SupervisorNode 自己管）。
        self.max_rewrites = (
            settings.effective_max_rewrites if max_rewrites is None else max(0, int(max_rewrites))
        )
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
            structured_issues: list = []
            critique_verdict = ""
            if critique:
                if isinstance(critique, dict):
                    raw_claims = critique.get("unsupported_claims", [])
                    raw_issues = critique.get("issues", [])
                    critique_verdict = critique.get("verdict", "")
                else:
                    raw_claims = getattr(critique, "unsupported_claims", [])
                    raw_issues = getattr(critique, "issues", [])
                    critique_verdict = getattr(critique, "verdict", "")
                unsupported_claims = list(raw_claims or [])
                structured_issues = list(raw_issues or [])

            # 阶段 6：这次进本节点是"Critic 判 revise 的打回"还是"补完证据后的重新成文"？
            # 只有前者算 `rewrite_count`；后者（research_more 回环）由 SupervisorNode
            # 记 `research_round_count`，两条路径各自设限、互不挤占额度。
            is_rewrite = (
                state.get("report") is not None
                and str(critique_verdict or "").strip().lower() == "revise"
            )
            is_followup = state.get("report") is not None and not is_rewrite

            question = state.get("question", "") if isinstance(state, dict) else getattr(state, "question", "")

            evidence_items, truncation = _select_evidence(state.get("evidence", []), state.get("report"))
            if truncation["omitted"]:
                from app.graph import run_log
                run_log.emit("reasoning_context", run_id=state.get("run_id"), context_truncation=truncation)

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
                history_context=self._build_revision_context(unsupported_claims, structured_issues),
                findings=state.get("findings", []),
            )
            # 阶段 6：把这次推理的 LLM 用量/耗时写进运行日志（指标的数据源）。
            from app.graph import run_log

            run_log.log_llm_call(
                state,
                node="reasoning",
                phase="rewrite" if is_rewrite else ("research_followup" if is_followup else "initial"),
                model=self.settings.openai_model,
                usage=getattr(self._engine, "last_llm_usage", None),
                duration_ms=getattr(self._engine, "last_llm_duration_ms", None),
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

            # 阶段 6：只增 `rewrite_count`（本节点就是 revise → reasoning 的落点）。
            # 判据见上面 is_rewrite：带着上一版报告进本节点**不足以**算改写——
            # research_more 回环也会带报告回来，但那是补证据后的重新成文。
            rewrite_count = int(state.get("rewrite_count", 0) or 0) + (1 if is_rewrite else 0)
            research_round_count = int(state.get("research_round_count", 0) or 0)

            logger.info(
                "Reasoning completed: confidence=%s rewrites=%d/%d research_rounds=%d/%d",
                report.confidence,
                rewrite_count,
                self.max_rewrites,
                research_round_count,
                self.settings.effective_max_research_rounds,
            )

            out = {
                "report": report,
                "rewrite_count": rewrite_count,
                "revision_count": rewrite_count + research_round_count,
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

    #: 阶段 4①：把审计的 ``action`` 翻译成重写时必须执行的动作。
    #: 只把 claim 文本贴回去是不够的 —— 模型会把"补研究"重新表述成"措辞更谨慎"，
    #: 于是同一句话换层皮再被审计打回一次（方案 §2 根因：三轮 LLM 改写伪装解决）。
    _ACTION_INSTRUCTIONS = {
        "remove_or_qualify": (
            "删除该论断，或改写到证据真正支持的范围（明确写出缺口，不得保留原有强度）"
        ),
        "research_more": (
            "该论断缺少数据支撑：本轮补充的研究结果已在上方数据/evidence 中，"
            "只能按新证据能支持的程度重写；新证据仍不足则删除该论断"
        ),
        "repair_format": (
            "该论断的口径/格式不合规：按证据的原始口径重写（单位、时间范围、标的必须与证据一致）"
        ),
    }

    def _build_revision_context(self, unsupported_claims: list[str], issues: list | None = None) -> str:
        """构建修订上下文：优先给**完整结构化 issue**，而不是只有一句 claim 文本。

        阶段 4① 之前只传 ``unsupported_claims`` 字符串，模型拿不到 kind / severity /
        action / required_coverage，只能靠猜该做什么，于是反复改写措辞而结论不变。

        旧格式响应（没有结构化 issues）仍然按原来的字符串列表渲染，保持兼容。
        """
        lines: list[str] = []
        if issues:
            lines.extend(
                [
                    "CRITIC REVISE REQUEST（结构化审计意见，逐条处理，不要只做措辞润色）:",
                    "上一版报告在以下论断上被审计判定为证据不足/不合规：",
                ]
            )
            for index, issue in enumerate(issues, start=1):
                lines.extend(_format_issue_for_revision(index, issue, self._ACTION_INSTRUCTIONS))
            lines.append("")
            lines.append(
                "审计未点名的部分不要改变结论方向；已被点名的论断必须按上面的「必须」执行，"
                "缺少证据时直接删除而不是换个说法。"
            )
            return "\n".join(lines)

        if not unsupported_claims:
            return ""
        lines.extend(
            [
                "CRITIC REVISE REQUEST:",
                "The following claims in your previous report are NOT supported by evidence:",
            ]
        )
        for claim in unsupported_claims:
            lines.append(f"  - {claim}")
        lines.append("")
        lines.append("Please rewrite those sections using ONLY verified evidence.")
        return "\n".join(lines)


def _issue_field(issue, key, default=None):
    """从 dict 或 ``AuditIssue`` 读取字段 —— state 里两种形式都可能出现。"""
    if isinstance(issue, dict):
        return issue.get(key, default)
    return getattr(issue, key, default)


def _format_issue_for_revision(index: int, issue, action_instructions: dict[str, str]) -> list[str]:
    """把一条 ``AuditIssue`` 渲染成修订指令（含 action 对应的必须动作）。"""
    kind = _issue_field(issue, "kind", "?")
    severity = _issue_field(issue, "severity", "?")
    action = _issue_field(issue, "action", "")
    claim = str(_issue_field(issue, "claim", "") or "")
    rationale = str(_issue_field(issue, "rationale", "") or "")
    required_tool_keys = list(_issue_field(issue, "required_tool_keys", []) or [])
    required_coverage = _issue_field(issue, "required_coverage", {}) or {}

    lines = [f"{index}. [{severity}/{kind}] {claim}"]
    if rationale:
        lines.append(f"   审计理由: {rationale}")
    instruction = action_instructions.get(str(action))
    if instruction:
        lines.append(f"   必须: {instruction}")
    if required_tool_keys:
        lines.append(f"   审计点名需要的数据: {', '.join(str(k) for k in required_tool_keys)}")
    if required_coverage:
        lines.append(f"   审计对数据量的要求: {json.dumps(required_coverage, ensure_ascii=False, default=str)}")
    return lines
