from typing import Any, Literal

from pydantic import BaseModel, Field

from app.detector.anomaly import detect_anomalies

#: 最终审计状态：pass / revise_exhausted / research_exhausted / error
FinalAuditStatus = Literal["pass", "revise_exhausted", "research_exhausted", "error"]
#: 交付状态 —— 调用方判断"能否当作可信结论展示"的**唯一**依据。
#: ``errors == []`` 不再代表可信（errors 只表达运行失败）。
DeliveryStatus = Literal["verified", "degraded", "blocked", "failed"]


#: claim—evidence 映射里论断的类型（方案 §4 阶段 4③）。
#: 区分"事实 / 推断 / 单源消息 / 比较 / 因果"是报告可追溯性的最小前提：
#: 单源消息不得写成事实，比较与因果必须引用对应 evidence。
ClaimType = Literal["fact", "inference", "single_source", "comparison", "causation", "structure", "other"]


class ClaimEvidence(BaseModel):
    """报告里的一条关键论断及其 evidence id 引用（claim—evidence 映射）。

    ``evidence_ids`` **只能来自真实证据账本**：Reasoning 输出后由代码逐条校验
    （``app/research/reasoning.py`` 的 ``_validate_claims``），不存在的 id 会被
    剥离并记入 ``MarketIntelligence.evidence_violations``。这样 LLM 无法通过
    伪造一个 id 来让某个论断看起来"有证据"。
    """

    claim: str
    evidence_ids: list[str] = Field(default_factory=list)
    claim_type: ClaimType = "other"


class EvidenceItem(BaseModel):
    """单条证据。"""

    id: str
    source_tool: str = ""
    tool_key: str | None = None
    operation_id: str | None = None
    domain: str = "unknown"
    metric: str = ""
    value: Any = None
    timestamp: str | None = None
    as_of_date: str | None = None
    retrieved_at: str | None = None
    source: str | None = None
    authority: str | None = None
    completeness: str = "unknown"
    coverage: str = "unknown"
    instrument_name: str | None = None
    status: str = "success"
    partial: bool = False
    note: str | None = None
    #: 阶段 4：这条证据属于哪个标的（工具调用时的 ``symbol`` 参数）。
    #: 让"报告里的公司名"可以对着证据校验，而不是凭模型记忆。
    instrument: str | None = None


class MarketIntelligence(BaseModel):
    title: str = "今日市场情报"
    requested_date: str | None = None
    as_of_date: str | None = None
    market_closed: bool | None = None
    #: T20：允许为空（旧实现必填）——模型少写一个字段就把整份报告（连同此前
    #: 所有已付费的工具调用）变成 ValidationError → report=None。缺失时由
    #: reasoning 归一化写入 data_caveats 说明降级，不允许静默变成正常报告。
    market_state: str = ""
    state_label: str = "Unknown"
    what_happened: str = ""
    why: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    #: 阶段 4③：关键论断 → evidence id 映射。Critic 据此逐条核对引用，
    #: 不再只能看着一段自由文本猜"这句话有没有数据支撑"。
    claims: list[ClaimEvidence] = Field(default_factory=list)
    #: 阶段 4④：**代码级**校验发现的问题（报告引用了不存在的 evidence id 等）。
    #: 非空即表示报告里出现过无法核实的引用；Critic 节点会据此在代码层强制
    #: 至少产出一条 ``unsupported_claim`` issue，因此这种情况**不可能 pass**。
    evidence_violations: list[str] = Field(default_factory=list)
    #: 报告提到但缺少成功返回数据或全 A 股名录确认的实体名。
    #: 常见股票静态表不构成验证；未验证不代表不存在。
    unverified_entities: list[str] = Field(default_factory=list)
    #: Code-generated provenance only; Reasoning overwrites model-supplied values.
    entity_registry_source: str | None = None
    entity_registry_as_of: str | None = None
    entity_conflicts: list[dict[str, str]] = Field(default_factory=list)
    entities_without_market_evidence: list[str] = Field(default_factory=list)
    strong_areas: list[str] = Field(default_factory=list)  # What's Moving — PRD §26
    what_changed: list[str] = Field(default_factory=list)  # 与之前相比的变化 — PRD §10
    what_matters: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)  # 反证与风险 — PRD §23
    data_caveats: list[str] = Field(default_factory=list)
    anomalies: list[dict[str, Any]] = Field(default_factory=list)  # Anomaly Radar — PRD §29-30
    confidence: Literal["high", "medium", "low"] = "low"
    used_tools: list[str] = Field(default_factory=list)


class ResearchResponse(BaseModel):
    question: str
    requested_date: str | None = None
    as_of_date: str | None = None
    market_closed: bool | None = None
    #: reasoning 环节失败时为空。此前该字段是必填的，导致"没产出报告"这种最需要
    #: 被解释的失败反而在组装响应时就抛 ValidationError，调用方只能拿到 500 和
    #: 一段 pydantic 校验文本，state.errors 里真正的原因永远传不出去。
    #: 现在允许为空，调用方必须检查它（见 errors）并给出明确的失败提示，
    #: 不要把空报告当成正常结果渲染。
    report: MarketIntelligence | None = None
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    cache_stats: dict[str, int] = Field(default_factory=dict)
    conversation_id: str | None = None
    critique: dict[str, Any] | None = None
    #: report 被清空后仍保留的确定性 issue 摘要，供前端和诊断使用。
    unresolved_issues: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    #: 阶段 5：最终审计状态与交付状态。缺省 None = "本次运行没有经过审计判定"，
    #: 调用方必须显式处理（不得推断 ``errors == []`` 即可信）。
    final_audit_status: FinalAuditStatus | None = None
    delivery_status: DeliveryStatus | None = None
    #: 阶段 5：非 verified 时的可读原因（前端 / CLI 直接展示）
    delivery_reason: str = ""
    #: 持久化门控的实际结果。``None`` 表示调用方尚未执行持久化；
    #: ``False`` 既包括门控拒绝，也包括写入失败或部分写入失败。
    persisted: bool | None = None
    #: 本次图运行的 run_id。**内部字段**：用于把 run_start / plan / executions /
    #: critic / route / finalize / delivery 七条结构化日志串起来；对外 JSON 响应里
    #: 会被 exclude 掉（main.py 传 model_dump(exclude={"run_id"})），落库记录里保留。
    run_id: str | None = None


def _resolve_terminal_state(state: dict[str, Any], report, errors: list[str]) -> tuple[str, str, str, Any]:
    """由终态 state 推导 (final_audit_status, delivery_status, delivery_reason, report)。

    优先用 ``finalize_audit`` 节点写入的字段；``verdict=pass`` 的快乐路径不经过该
    节点，这里按 critique 兜底派生，保证两条路径的对外契约一致。
    """
    from app.graph.nodes.finalize import resolve_delivery

    critique = state.get("critique")
    delivery_status = state.get("delivery_status")

    if (
        delivery_status in ("verified", "degraded", "blocked", "failed")
        and state.get("final_audit_status") in ("pass", "revise_exhausted", "research_exhausted", "error")
    ):
        audit_status = state.get("final_audit_status")
        if state.get("delivery_reason"):
            reason = str(state["delivery_reason"])
        else:
            _, _, reason = resolve_delivery(
                critique,
                errors,
                blocked_reason=state.get("blocked_reason"),
                gap_key_decisions=state.get("gap_key_decisions"),
            )
        if delivery_status == "verified":
            verdict = critique.get("verdict") if isinstance(critique, dict) else getattr(critique, "verdict", None)
            if verdict != "pass":
                audit_status, delivery_status, reason = resolve_delivery(
                    critique,
                    errors,
                    blocked_reason=state.get("blocked_reason"),
                    gap_key_decisions=state.get("gap_key_decisions"),
                )
    else:
        audit_status, derived_delivery, reason = resolve_delivery(
            critique,
            errors,
            blocked_reason=state.get("blocked_reason"),
            gap_key_decisions=state.get("gap_key_decisions"),
        )
        delivery_status = derived_delivery

    report_out = report
    if report_out is None and delivery_status == "verified":
        # A passing Critic cannot make an absent reasoning report deliverable.
        # Treat this as a failed run so API, SSE, and CLI do not report a false
        # verified result with an empty body.
        audit_status = "error"
        delivery_status = "failed"
        reason = "reasoning produced no report"
    if delivery_status in ("blocked", "failed") and report_out is not None:
        # 未通过审计的正文不得作为结论返回（阶段 5）。
        report_out = None

    return str(audit_status), str(delivery_status), reason, report_out


def _critique_reason(critique: Any) -> str:
    if critique is None:
        return ""
    if isinstance(critique, dict):
        return str(critique.get("reason", "") or "")
    return str(getattr(critique, "reason", "") or "")


def build_response_from_state(
    state: dict[str, Any],
    question: str,
    conversation_id: str | None = None,
) -> ResearchResponse:
    """从 LangGraph 终态组装 ResearchResponse。

    同步路径（Orchestrator.run）与 SSE 路径（/api/ask/stream）共用，
    避免 tool_results / critique 的归一化逻辑在两边漂移。

    注意：values 流模式与 ainvoke 的终态里，results 仍是 ToolResult 实例，
    而 ResearchResponse.tool_results 是 list[dict]，这里统一 dump。
    """
    raw_results = state.get("results") or []
    tool_results = [r if isinstance(r, dict) else r.model_dump() for r in raw_results]

    critique = state.get("critique")
    if critique is not None and not isinstance(critique, dict) and hasattr(critique, "model_dump"):
        critique = critique.model_dump()

    report = state.get("report")
    errors = list(state.get("errors") or [])

    final_audit_status, delivery_status, delivery_reason, report = _resolve_terminal_state(state, report, errors)

    if report is None and not errors and delivery_status not in ("blocked",):
        # 保证 report 为空时调用方手里一定有一条可展示的原因，而不是只有空报告。
        # blocked 是"审计没通过"而不是"运行失败"：它的原因在 critique / delivery_reason 里，
        # 往 errors 里塞一句反而会污染 errors 的语义（errors 只表达运行失败）。
        errors.append("reasoning produced no report")

    # D3：anomalies 由代码用 detect_anomalies 填（同步 / SSE 双路径共用此组装点），
    # 不依赖模型输出——模型即使填了也在这里被覆盖，避免双写。
    if report is not None:
        try:
            anomalies = detect_anomalies(
                state.get("results") or [],
                domain=state.get("domain") or "unknown",
            )
            if isinstance(report, dict):
                report["anomalies"] = [a.to_dict() for a in anomalies]
            else:
                report.anomalies = [a.to_dict() for a in anomalies]
        except Exception as exc:
            # 检测失败不能静默：报告照常产出，但错误必须可见（规则 6）
            errors.append(f"Anomaly detection failed: {exc}")

    return ResearchResponse(
        question=question,
        requested_date=state.get("requested_date"),
        as_of_date=state.get("as_of_date"),
        market_closed=state.get("market_closed"),
        report=report,
        tool_results=tool_results,
        cache_stats=dict(state.get("cache_stats") or {}),
        conversation_id=conversation_id,
        critique=critique,
        unresolved_issues=list(state.get("unresolved_issues") or _unresolved_issues(critique)),
        errors=errors,
        final_audit_status=final_audit_status,
        delivery_status=delivery_status,
        delivery_reason=delivery_reason,
        persisted=state.get("persisted"),
        run_id=state.get("run_id"),
    )


def _unresolved_issues(critique: Any) -> list[str]:
    from app.graph.nodes.finalize import unresolved_issue_summary

    return unresolved_issue_summary(critique)
