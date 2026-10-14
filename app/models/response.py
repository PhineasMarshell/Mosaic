import time
from collections.abc import Sequence
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
    result_partial: bool = False
    note: str | None = None
    #: 阶段 4：这条证据属于哪个标的（工具调用时的 ``symbol`` 参数）。
    #: 让"报告里的公司名"可以对着证据校验，而不是凭模型记忆。
    instrument: str | None = None


class MarketIntelligence(BaseModel):
    title: str = "今日市场情报"
    requested_date: str | None = None
    as_of_date: str | None = None
    market_closed: bool | None = None
    #: 受控 degraded 的数据完整性与来源覆盖元数据。
    partial: bool = False
    source: Any = None
    coverage: Any = None
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
    entity_code_matches: dict[str, str] = Field(default_factory=dict)
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
    partial: bool = False
    source: Any = None
    coverage: Any = None
    #: 本次运行是否发生了受控数据源切换（例如 THS → MCP）。该字段由
    #: ToolResult 逐条汇总，避免调用方只能深入 tool_results 才能发现路由变化。
    source_switch: bool = False
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
    reason_codes: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    #: 阶段 5：最终审计状态与交付状态。缺省 None = "本次运行没有经过审计判定"，
    #: 调用方必须显式处理（不得推断 ``errors == []`` 即可信）。
    final_audit_status: FinalAuditStatus | None = None
    delivery_status: DeliveryStatus | None = None
    #: 阶段 5：非 verified 时的可读原因（前端 / CLI 直接展示）
    delivery_reason: str = ""
    blocked_reason: str = ""
    #: 持久化门控的实际结果。``None`` 表示调用方尚未执行持久化；
    #: ``False`` 既包括门控拒绝，也包括写入失败或部分写入失败。
    persisted: bool | None = None
    #: 本次图运行的 run_id。sync / SSE / 日志共同保留该关联字段。
    run_id: str | None = None
    #: 终态诊断字段。正常 verified 响应可以为空；timeout/failed 必须保留，
    #: 便于 sync、SSE 和脱敏运行日志对齐同一失败原因。
    stage: str | None = None
    exception_type: str | None = None
    elapsed_ms: float | None = None
    remaining_budget: float | None = None
    #: P13：evidence-only fallback 的结构化准入诊断。``None`` 表示本次运行根本
    #: 没有进入该路径；只要进入（无论最终 degraded 还是 blocked）就必须带上
    #: 候选数、接受数、拒绝原因和候选 evidence id，供审计复盘与脱敏 JSONL 记录。
    fallback_diagnostics: dict[str, Any] | None = None
    #: 上述诊断的平铺字段，便于 JSONL / 前端直接读取而不必解嵌套。
    candidate_count: int | None = None
    accepted_count: int | None = None
    rejected_count: int | None = None
    rejection_reasons: dict[str, int] = Field(default_factory=dict)


def _resolve_terminal_state(state: dict[str, Any], report, errors: list[str]) -> tuple[str, str, str, Any, list[str]]:
    """由终态 state 推导审计状态、交付状态、理由、报告与严格校验原因。

    优先用 ``finalize_audit`` 节点写入的字段；重放或兼容调用缺少终态字段时按
    critique 兜底派生，并复用同一严格 claim 校验，保证两条路径的对外契约一致。
    """
    from app.graph.nodes.finalize import _strict_verified_issues, infer_blocked_reason, resolve_delivery

    critique = state.get("critique")
    blocked_reason = infer_blocked_reason(state, critique)
    delivery_status = state.get("delivery_status")

    if delivery_status in ("verified", "degraded", "blocked", "failed") and state.get("final_audit_status") in (
        "pass",
        "revise_exhausted",
        "research_exhausted",
        "error",
    ):
        audit_status = state.get("final_audit_status")
        if state.get("delivery_reason"):
            reason = str(state["delivery_reason"])
        else:
            _, _, reason = resolve_delivery(
                critique,
                errors,
                blocked_reason=blocked_reason,
                gap_key_decisions=state.get("gap_key_decisions"),
            )
        if delivery_status == "verified":
            verdict = critique.get("verdict") if isinstance(critique, dict) else getattr(critique, "verdict", None)
            if verdict != "pass":
                audit_status, delivery_status, reason = resolve_delivery(
                    critique,
                    errors,
                    blocked_reason=blocked_reason,
                    gap_key_decisions=state.get("gap_key_decisions"),
                )
    else:
        audit_status, derived_delivery, reason = resolve_delivery(
            critique,
            errors,
            blocked_reason=blocked_reason,
            gap_key_decisions=state.get("gap_key_decisions"),
        )
        delivery_status = derived_delivery

    strict_issues: list[str] = []
    if delivery_status == "verified" and report is not None:
        strict_issues = _strict_verified_issues(state, report)
        if strict_issues:
            strict_issues.append("verified_provenance_failed")
            audit_status = "revise_exhausted"
            delivery_status = "blocked"
            reason = "verified_provenance_failed: " + ", ".join(strict_issues)

    report_out = report
    if report_out is None and delivery_status == "verified":
        # A passing Critic cannot make an absent reasoning report deliverable.
        # Treat this as a failed run so API, SSE, and CLI do not report a false
        # verified result with an empty body.
        audit_status = "error"
        delivery_status = "failed"
        reason = "reasoning produced no report"
        strict_issues.append("no_report")
    if delivery_status in ("blocked", "failed") and report_out is not None:
        # 未通过审计的正文不得作为结论返回（阶段 5）。
        report_out = None

    return str(audit_status), str(delivery_status), reason, report_out, strict_issues


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
    response_started = time.monotonic()
    raw_results = state.get("results") or []
    tool_results = [r if isinstance(r, dict) else r.model_dump() for r in raw_results]

    critique = state.get("critique")
    if critique is not None and not isinstance(critique, dict) and hasattr(critique, "model_dump"):
        critique = critique.model_dump()

    report = state.get("report")
    errors = list(state.get("errors") or [])

    final_audit_status, delivery_status, delivery_reason, report, strict_issues = _resolve_terminal_state(state, report, errors)
    from app.graph.nodes.finalize import infer_blocked_reason
    blocked_reason = infer_blocked_reason(state, critique) if delivery_status not in ("verified", "degraded") else ""
    if "verified_provenance_failed" in strict_issues:
        blocked_reason = "verified_provenance_failed"

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

    elapsed = state.get("elapsed_ms")
    if elapsed is None and state.get("started_at") is not None:
        try:
            elapsed = round(max(0.0, time.monotonic() - float(state["started_at"])) * 1000, 1)
        except (TypeError, ValueError):
            elapsed = None
    remaining = state.get("remaining_budget")
    if remaining is None and state.get("budget_deadline") is not None:
        try:
            remaining = round(float(state["budget_deadline"]) - time.monotonic(), 3)
        except (TypeError, ValueError):
            remaining = None

    if delivery_status == "verified" and not state.get("final_audit_status"):
        from app.graph import run_log

        run_log.log_finalize({**state, "elapsed_ms": elapsed}, final_audit_status="pass", delivery_status="verified",
                             reason="audit_passed", remaining_budget=remaining, reason_codes=[])
        run_log.emit("stage", run_id=state.get("run_id"), stage="finalize", status="verified",
                     elapsed_ms=round((time.monotonic()-response_started)*1000, 1), remaining_budget=remaining)

    tool_reason_codes: list[str] = []
    source_switch = False
    for item in raw_results:
        if isinstance(item, dict):
            tool_reason_codes.extend(str(code) for code in (item.get("reason_codes") or []) if code)
            source_switch = source_switch or bool(item.get("source_switch", False))
        else:
            tool_reason_codes.extend(str(code) for code in (getattr(item, "reason_codes", []) or []) if code)
            source_switch = source_switch or bool(getattr(item, "source_switch", False))
    response_reason_codes = list(dict.fromkeys([
        *(state.get("reason_codes") or state.get("degraded_reason_codes") or []),
        *tool_reason_codes,
        *strict_issues,
    ]))

    # P13：fallback 诊断从 graph state 原样透出（sync / SSE 共用本函数，因此
    # 两条传输路径不可能给出不同的候选/拒绝统计）。
    fallback_diagnostics = state.get("fallback_diagnostics") or None
    if isinstance(fallback_diagnostics, dict):
        candidate_total = int(fallback_diagnostics.get("candidate_count", 0) or 0)
        accepted_total = int(fallback_diagnostics.get("accepted_count", 0) or 0)
        rejected_total = int(fallback_diagnostics.get("rejected_count", 0) or 0)
        rejection_reasons = {
            str(key): int(value)
            for key, value in (fallback_diagnostics.get("rejection_reasons") or {}).items()
        }
    else:
        fallback_diagnostics = None
        candidate_total = accepted_total = rejected_total = None
        rejection_reasons = {}

    return ResearchResponse(
        question=question,
        requested_date=state.get("requested_date") or (report.requested_date if hasattr(report, "requested_date") else report.get("requested_date") if isinstance(report, dict) else None),
        as_of_date=state.get("as_of_date") or (report.as_of_date if hasattr(report, "as_of_date") else report.get("as_of_date") if isinstance(report, dict) else None),
        market_closed=state.get("market_closed") if state.get("market_closed") is not None else (report.market_closed if hasattr(report, "market_closed") else report.get("market_closed") if isinstance(report, dict) else None),
        partial=bool((report.partial if hasattr(report, "partial") else report.get("partial", False)) if report is not None else False),
        source=(report.source if hasattr(report, "source") else report.get("source") if isinstance(report, dict) else None)
        if report is not None else None,
        coverage=(report.coverage if hasattr(report, "coverage") else report.get("coverage") if isinstance(report, dict) else None)
        if report is not None else None,
        source_switch=source_switch,
        report=report,
        tool_results=tool_results,
        cache_stats=dict(state.get("cache_stats") or {}),
        conversation_id=conversation_id,
        critique=critique,
        unresolved_issues=list(state.get("unresolved_issues") or _unresolved_issues(critique)),
        reason_codes=response_reason_codes,
        errors=errors,
        final_audit_status=final_audit_status,
        delivery_status=delivery_status,
        delivery_reason=delivery_reason,
        blocked_reason=blocked_reason,
        persisted=False if delivery_status != "verified" else state.get("persisted"),
        run_id=state.get("run_id"),
        stage=state.get("stage") or "finalize",
        exception_type=state.get("exception_type"),
        remaining_budget=remaining,
        elapsed_ms=elapsed,
        fallback_diagnostics=fallback_diagnostics,
        candidate_count=candidate_total,
        accepted_count=accepted_total,
        rejected_count=rejected_total,
        rejection_reasons=rejection_reasons,
    )


def _unresolved_issues(critique: Any) -> list[str]:
    from app.graph.nodes.finalize import unresolved_issue_summary

    return unresolved_issue_summary(critique)


def terminal_code_for(delivery_status: str | None) -> str:
    """交付状态的规范终态 ``code``（sync 与 SSE 共用同一张映射表）。

    这样"同一份 state ⇒ 同一个 code"不是靠两端各写一遍字面量，而是靠同一次查表。
    """
    return {
        "verified": "ok",
        "degraded": "degraded",
        "blocked": "blocked",
        "failed": "failed",
    }.get(str(delivery_status or ""), "failed")


def build_terminal_payload(
    result: ResearchResponse | None = None,
    *,
    code: str,
    message: str | None = None,
    retryable: bool | None = None,
    delivery_status: str | None = None,
    final_audit_status: str | None = None,
    delivery_reason: str | None = None,
    exception_type: str | None = None,
    reason_codes: Sequence[str] | None = None,
    run_id: str | None = None,
    question: str | None = None,
    elapsed_ms: float | None = None,
    remaining_budget: float | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """P13 §二：sync 与 SSE **唯一**的终态载荷构造器。

    两个端点此前各自手工拼装终态字典，于是同一份 graph state 会得到不同形状的
    交付结果（同步 blocked 有 ``retryable``、SSE 只有 ``error``；成功路径同步没有
    ``code``）。本函数把"终态字段集"收敛到一处：

    * ``status`` / ``delivery_status`` / ``final_audit_status`` / ``code`` /
      ``reason_codes`` / ``report`` / ``persisted`` 全部由此处推导；
    * ``terminal`` 显式标记这是终态事件，SSE 的 progress / heartbeat 一律不带它，
      因此"心跳"永远不会被误读成终态（§二.3）；
    * ``persisted`` 只在 ``verified`` 时才可能为真，degraded/blocked/failed 一律
      为假（§一.7）。

    传入 ``result`` 时以它的 ``model_dump()`` 为基底；不传时（超时 / 上游异常等
    没有研究报告对象的路径）用一个显式的最小骨架，避免异常路径再造一套字段。
    """
    if result is not None:
        payload: dict[str, Any] = result.model_dump()
    else:
        payload = {
            "report": None,
            "tool_results": [],
            "cache_stats": {},
            "critique": None,
            "unresolved_issues": [],
            "reason_codes": [],
            "errors": [],
            "partial": False,
            "source_switch": False,
            "persisted": False,
        }

    resolved_delivery = str(delivery_status or payload.get("delivery_status") or "failed")
    payload["delivery_status"] = resolved_delivery
    payload["final_audit_status"] = str(final_audit_status or payload.get("final_audit_status") or "error")
    if delivery_reason is not None:
        payload["delivery_reason"] = delivery_reason
    payload.setdefault("delivery_reason", "")
    if exception_type is not None:
        payload["exception_type"] = exception_type
    if reason_codes is not None:
        payload["reason_codes"] = list(dict.fromkeys([
            *(payload.get("reason_codes") or []),
            *(str(item) for item in reason_codes if item),
        ]))
    if run_id is not None:
        payload["run_id"] = run_id
    if question is not None:
        payload["question"] = question
    if elapsed_ms is not None:
        payload["elapsed_ms"] = elapsed_ms
    if remaining_budget is not None:
        payload["remaining_budget"] = remaining_budget

    payload["code"] = str(code)
    payload["status"] = resolved_delivery
    payload["terminal"] = True
    payload["persisted"] = False if resolved_delivery != "verified" else bool(payload.get("persisted"))
    if payload.get("report") is None:
        # blocked / failed 绝不把未经审计的正文发出去。
        payload["report"] = None
    if message is not None:
        payload["error"] = message
        payload["detail"] = message
    if retryable is not None:
        payload["retryable"] = bool(retryable)
    if extra:
        payload.update(extra)
    return payload
