"""finalize_audit 节点 — 决定"这次运行到底交付什么"（阶段 5 保守版）。

阶段 6 的受控 degraded 路径只在非 pass 审计耗尽、且代码能够逐条证明至少一个
claim 的 evidence、标的、日期、来源与完整性时启用。正文由代码从保留 claim 重建，
不会再次调用模型生成摘要；无法安全隔离的运行仍然 blocked。

四种终态（``delivery_status``）：

=================  ====================================  ===============
值                 条件                                   持久化
=================  ====================================  ===============
``verified``       Critic=pass                             是
``degraded``       耗尽但可产出仅含已证实事实的报告         否
``blocked``        耗尽且 verdict 非 pass，无法安全作答   否
``failed``         节点 / 上游 / 审计器自身错误            否
=================  ====================================  ===============

**`errors == []` 不再等价于可信**：errors 只表达运行失败。一个 ``verdict=revise``
且 errors 为空的运行，最终状态是 ``blocked`` 而不是正常完成。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Literal

from app.agent.evidence_gate import match_claims_to_evidence
from app.graph import run_log
from app.graph.budget import elapsed_ms

logger = logging.getLogger(__name__)

#: 最终审计状态（写进 ResearchState / ResearchResponse）
FinalAuditStatus = Literal["pass", "revise_exhausted", "research_exhausted", "error"]
#: 交付状态（调用方判断"能否当作可信结论展示"的唯一依据）
DeliveryStatus = Literal["verified", "degraded", "blocked", "failed"]

#: verdict → 耗尽后的审计状态
_VERDICT_TO_AUDIT_STATUS = {
    "pass": "pass",
    "revise": "revise_exhausted",
    "research_more": "research_exhausted",
    "error": "error",
}


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """兼容 dict / pydantic 对象取值（与 critic._field 同语义）。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def resolve_delivery(
    critique: Any,
    errors: list[str] | None = None,
    *,
    blocked_reason: str | None = None,
    gap_key_decisions: list | None = None,
) -> tuple[FinalAuditStatus, DeliveryStatus, str]:
    """（纯函数）由 Critique + errors 推导终态。

    顺序刻意是"先看运行是否失败，再看审计是否通过"：
    审计器自己挂了（``verdict=error``）是 ``failed``；审计器说"没通过"是 ``blocked``。
    """
    verdict = str(_field(critique, "verdict", "") or "").strip().lower()
    errors = list(errors or [])
    reason = str(_field(critique, "reason", "") or "")

    def explain(base: str) -> str:
        details: list[str] = []
        if blocked_reason:
            details.append(f"阻断原因={blocked_reason}")
        issues = unresolved_issue_summary(critique)
        if issues:
            details.append("未解决issue=" + "; ".join(issues[:5]))
        decisions = gap_key_decisions or _field(critique, "gap_key_decisions", []) or []
        if decisions:
            rendered = ", ".join(
                f"{_field(d, 'key', '?')}:{_field(d, 'reason', _field(d, 'decision', '?'))}" for d in decisions[:10]
            )
            details.append("gap=" + rendered)
        return base + ("；" + "；".join(details) if details else "")

    if verdict == "error":
        return "error", "failed", explain(reason or "Critic 审计失败，未产生可信结论")

    if not verdict:
        # 没有 critique（理论上 critic 节点总会写）——不猜、不当 pass。
        return "error", "failed", explain(reason or "本次运行没有产生审计结论")

    if verdict == "pass":
        return "pass", "verified", explain(reason or "审计通过")

    # 非 pass：保守阻断。方案 §4 阶段 5 第 3 条 —— 本版不自动生成降级报告。
    audit_status = _VERDICT_TO_AUDIT_STATUS.get(verdict, "error")
    return audit_status, "blocked", explain(reason or f"审计未通过（verdict={verdict}），不作为可信结论交付")


_DEGRADED_STRONG_RE = re.compile(
    r"最强|主线|全面|全市场|所有|全部|全线|反转|必然|一定|导致|带动|引发|造成|显著|领先|第一|"
    r"相比|相较|高于|低于|超过|占优"
)
_EVIDENCE_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_-])E\d{3,}(?![A-Za-z0-9_-])|\b[A-Za-z][A-Za-z0-9_-]*-\d{3,}\b"
)
_BAD_COMPLETENESS = frozenset({"partial", "incomplete", "truncated"})
_GLOBAL_DEGRADED_BLOCKERS = frozenset(
    {
        "invalid_reference",
        "invalid_evidence",
        "conflicting_evidence",
        "stale_date",
        "closed_day_current_market_claim",
        "unmatched_instrument",
        "unmatched_subject",
        "instrument_name_unknown",
        "partial_only",
        "invalid_entity",
        "authority_conflict",
        "unknown_alias",
        "forged_reference",
    }
)


def _report_dict(report: Any) -> dict[str, Any]:
    if report is None:
        return {}
    if isinstance(report, dict):
        return dict(report)
    if hasattr(report, "model_dump"):
        return report.model_dump(exclude_none=False)
    return dict(getattr(report, "__dict__", {}) or {})


def _evidence_field(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _claim_is_strong(claim: str) -> bool:
    return bool(_DEGRADED_STRONG_RE.search(str(claim or "")))


def _degraded_evidence_ok(item: Any, claim: str) -> bool:
    """Require a successful, dated, usable datum for a retained claim.

    Explicitly incomplete/partial ledgers are rejected here.  Historical
    gateway rows may carry ``completeness=unknown`` while still being a
    successful, non-partial datum; the existing Critic and date/instrument
    matcher remain authoritative for those rows.
    """
    if not item:
        return False
    if str(_evidence_field(item, "status", "success")).lower() != "success":
        return False
    if bool(_evidence_field(item, "partial", False)):
        return False
    completeness = str(_evidence_field(item, "completeness", "") or "").lower()
    if completeness in _BAD_COMPLETENESS:
        return False
    source_tool = str(
        _evidence_field(item, "source_tool", _evidence_field(item, "operation_id", "")) or ""
    ).strip().lower()
    source = str(_evidence_field(item, "source", "") or "").strip().lower()
    authority = str(_evidence_field(item, "authority", "") or "").strip().lower()
    if not source_tool or source_tool in {"unknown", "reasoning_engine"}:
        return False
    # News/events require an actual source/authority.  Market quote ledgers may
    # legitimately expose only the operation's source_tool.
    if re.search(r"新闻|消息|公告|报道|事件", claim) and (not source or source == "unknown") and (
        not authority or authority == "unknown"
    ):
        return False
    if _claim_is_strong(claim) and str(_evidence_field(item, "coverage", "") or "").lower() in {
        "partial",
        "sample",
        "unknown",
        "",
    }:
        return False
    return True


def _structural_date_fact(report: dict[str, Any], state: dict[str, Any]) -> str | None:
    if state.get("market_closed") is not True and report.get("market_closed") is not True:
        return None
    requested = state.get("requested_date") or report.get("requested_date") or "未知请求日"
    as_of = state.get("as_of_date") or report.get("as_of_date")
    planned = state.get("planned_as_of_date")
    if as_of:
        return f"请求日 {requested} A股休市；可核验数据截至 {as_of}。"
    if planned:
        return f"请求日 {requested} A股休市；最近交易日 {planned} 行情尚无可核验证据。"
    return f"请求日 {requested} A股休市；暂无可用交易日行情证据。"


def _split_sentences(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    return [part.strip() for part in re.split(r"[。！？；;\n]+", value) if part.strip()]


def build_degraded_report(
    state: dict[str, Any],
    report: Any,
    critique: Any,
) -> tuple[Any | None, list[str], list[str]]:
    """Deterministically isolate verified claims for controlled degraded delivery.

    The function returns ``(report, unresolved_issues, reason_codes)``.  It never
    asks the model to rewrite prose: the retained body is made directly from the
    validated claim strings and code-owned date facts.
    """
    payload = _report_dict(report)
    if not payload:
        return None, ["degraded_requires_report"], ["no_report"]
    violations = [str(v) for v in (payload.get("evidence_violations") or [])]
    conflicts = payload.get("entity_conflicts") or []
    if violations or any(str(_evidence_field(c, "authority", "")).lower() == "authoritative" for c in conflicts):
        codes = ["invalid_reference"] if violations else ["authority_conflict"]
        return None, violations or ["权威证券主数据冲突"], codes
    verdict = str(_field(critique, "verdict", "") or "").lower()
    if verdict in {"", "error"}:
        return None, ["critic_error"], ["critic_error"]
    audit_errors = [str(error).lower() for error in (state.get("errors") or [])]
    if any(re.search(r"critic|reasoning|audit", error) for error in audit_errors):
        return None, ["critic_or_reasoning_audit_error"], ["audit_error"]

    evidence = list(state.get("evidence") or payload.get("evidence") or [])
    expected_date = state.get("planned_as_of_date") or state.get("as_of_date") or payload.get("as_of_date")
    rows = match_claims_to_evidence(
        payload,
        evidence,
        expected_date=expected_date,
        requested_date=state.get("requested_date") or payload.get("requested_date"),
        market_closed=state.get("market_closed")
        if state.get("market_closed") is not None
        else payload.get("market_closed"),
    )
    # A forged/unknown reference anywhere in the report is a hard stop, even
    # when another claim could otherwise be delivered.  Silently dropping it
    # would make an attacker able to hide an invalid alias beside a real fact.
    if any("invalid_reference" in (row.get("reason_codes") or []) for row in rows):
        return None, ["报告包含 forged/unknown evidence reference"], ["invalid_reference"]
    by_id = {str(_evidence_field(item, "id", "")): item for item in evidence}
    prose_values: list[str] = []
    for key in ("title", "market_state", "what_happened"):
        prose_values.extend(_split_sentences(payload.get(key)))
    for key in ("why", "strong_areas", "what_changed", "what_matters", "risks", "data_caveats"):
        prose_values.extend(str(value) for value in (payload.get(key) or []) if isinstance(value, str))
    forged_in_prose = sorted(
        {reference for text in prose_values for reference in _EVIDENCE_REF_RE.findall(text) if reference not in by_id}
    )
    if forged_in_prose:
        return None, [f"报告正文包含 forged/unknown evidence reference：{', '.join(forged_in_prose)}"], ["invalid_reference"]
    expected_domain = str(state.get("domain") or "").strip().lower()
    if expected_domain:
        for item in evidence:
            item_domain = str(_evidence_field(item, "domain", "") or "").strip().lower()
            if item_domain and item_domain not in {"unknown", expected_domain}:
                return None, ["证据域与请求域冲突"], ["domain_mismatch"]
    hard_scope_codes = sorted(
        {
            code
            for row in rows
            for code in (row.get("reason_codes") or [])
            if code in _GLOBAL_DEGRADED_BLOCKERS
        }
    )
    if hard_scope_codes:
        return None, ["存在错误日期、标的、完整性或冲突证据，无法安全降级"], hard_scope_codes
    claims = list(payload.get("claims") or [])
    retained: list[dict[str, Any]] = []
    dropped: list[str] = []
    used_ids: list[str] = []
    for claim, row in zip(claims, rows, strict=False):
        text = str(_evidence_field(claim, "claim", "") or "").strip()
        claim_type = str(_evidence_field(claim, "claim_type", "") or "").lower()
        ids = [str(x) for x in (_evidence_field(claim, "evidence_ids", []) or [])]
        reasons = list(row.get("reason_codes") or [])
        valid_items = [by_id.get(eid) for eid in ids]
        complete = bool(ids) and bool(row.get("supported")) and all(_degraded_evidence_ok(item, text) for item in valid_items)
        if complete and not (reasons and any(code in _GLOBAL_DEGRADED_BLOCKERS for code in reasons)):
            partial_strong = any(bool(_evidence_field(item, "partial", False)) for item in valid_items) and (
                _claim_is_strong(text) or claim_type in {"comparison", "causation"}
            )
            if partial_strong:
                return None, ["partial 数据不能支撑比较、因果或绝对化结论"], ["partial_strong_claim"]
            if any(bool(_evidence_field(item, "partial", False)) for item in valid_items) and _claim_is_strong(text):
                complete = False
            else:
                retained.append(dict(claim) if isinstance(claim, dict) else claim.model_dump())
                for eid in ids:
                    if eid not in used_ids:
                        used_ids.append(eid)
        if not complete:
            dropped.append(text[:200] or "未命名 claim")

    if not retained:
        return None, ["没有同时满足 evidence、标的、日期、来源和完整性要求的 claim"], ["no_verified_claim"]

    unresolved = [f"已删除未验证 claim：{text}" for text in dropped]
    retained_text = [str(_evidence_field(item, "claim", "") or "") for item in retained]
    original_parts: list[str] = []
    for key in ("market_state", "what_happened"):
        original_parts.extend(_split_sentences(payload.get(key)))
    for key in ("why", "strong_areas", "what_changed", "what_matters", "risks"):
        for value in payload.get(key) or []:
            original_parts.extend(_split_sentences(value))
    for sentence in original_parts:
        if any(sentence == claim or sentence in claim or claim in sentence for claim in retained_text):
            continue
        if sentence and not re.search(r"请求日|交易日|休市|数据截至|无法确认|未验证|证据不足|暂无可核验", sentence):
            unresolved.append(f"已删除未验证段落：{sentence[:200]}")
    # An unresolved global conflict or an audit error is never isolated.
    issue_codes: list[str] = []
    for issue in _field(critique, "issues", []) or []:
        issue_codes.extend(str(code) for code in (_evidence_field(issue, "reason_codes", []) or []))
        kind = str(_evidence_field(issue, "kind", "") or "")
        if kind in {"invalid_entity"}:
            return None, unresolved + ["权威实体校验冲突"], ["invalid_entity"]
    if any(code in _GLOBAL_DEGRADED_BLOCKERS for code in issue_codes):
        return None, unresolved + ["存在无法安全隔离的审计冲突"], sorted(set(issue_codes) & _GLOBAL_DEGRADED_BLOCKERS)

    sources: list[str] = []
    coverages: list[str] = []
    kept_evidence = []
    for eid in used_ids:
        item = by_id.get(eid)
        if item is None:
            continue
        kept_evidence.append(
            item.model_dump()
            if hasattr(item, "model_dump")
            else dict(item)
            if isinstance(item, dict)
            else dict(getattr(item, "__dict__", {}) or {})
        )
        for value, target in ((_evidence_field(item, "source", None), sources), (_evidence_field(item, "coverage", None), coverages)):
            if value and str(value) not in target:
                target.append(str(value))

    structural = _structural_date_fact(payload, state)
    body_parts = [str(_evidence_field(c, "claim", "")).strip() for c in retained if str(_evidence_field(c, "claim", "")).strip()]
    if structural:
        body_parts.insert(0, structural)
    requested_date = state.get("requested_date") or payload.get("requested_date")
    as_of_date = state.get("as_of_date") or payload.get("as_of_date")
    market_closed = state.get("market_closed") if state.get("market_closed") is not None else payload.get("market_closed")
    caveats = list(payload.get("data_caveats") or [])
    caveats.extend(unresolved)
    caveats.append(
        "受控 degraded：正文仅保留通过 claim→evidence 校验的事实；未验证段落已删除；"
        f"requested_date={requested_date or 'unknown'}; as_of_date={as_of_date or 'unknown'}; "
        f"market_closed={market_closed if market_closed is not None else 'unknown'}; partial=true; "
        f"source={','.join(sources) or 'unknown'}; coverage={','.join(coverages) or 'unknown'}"
    )
    payload.update(
        {
            "title": "部分证据支持的市场情报",
            "requested_date": requested_date,
            "as_of_date": as_of_date,
            "market_closed": market_closed,
            "market_state": "部分证据支持",
            "state_label": "部分证据支持",
            "what_happened": "；".join(body_parts),
            "why": [],
            "strong_areas": [],
            "what_changed": [],
            "what_matters": [],
            "risks": [],
            "evidence": kept_evidence,
            "claims": retained,
            "data_caveats": list(dict.fromkeys(caveats)),
            "confidence": "low",
            "partial": True,
            "source": sources,
            "coverage": coverages,
        }
    )
    try:
        from app.models.response import MarketIntelligence

        return MarketIntelligence.model_validate(payload), unresolved, ["degraded_partial_evidence", "claims_filtered"]
    except Exception:
        return None, unresolved + ["degraded_report_validation_failed"], ["degraded_report_validation_failed"]


def unresolved_issue_summary(critique: Any) -> list[str]:
    """把未解决的 issue 压成可展示的短句（进日志与阻断原因）。"""
    issues = _field(critique, "issues", []) or []
    out: list[str] = []
    for issue in issues:
        parts = [str(_field(issue, "kind", "?"))]
        claim = str(_field(issue, "claim", "") or "")
        if claim:
            parts.append(claim[:80])
        parts.append(f"action={_field(issue, 'action', '?')}")
        out.append(" | ".join(parts))
    return out


def infer_blocked_reason(state: dict[str, Any], critique: Any, settings: Any = None) -> str:
    """Choose a non-empty structured terminal reason when route state lacks one."""
    explicit = str(state.get("blocked_reason") or "").strip()
    if explicit:
        return explicit
    if settings is not None:
        from app.graph.builder import critic_route_outcome

        decision, reason = critic_route_outcome(state, settings)
        if decision == "finalize_audit":
            return reason
    verdict = str(_field(critique, "verdict", "") or "").strip().lower()
    if verdict == "pass":
        return ""
    if verdict == "research_more":
        decisions = state.get("gap_key_decisions") or _field(critique, "gap_key_decisions", []) or []
        if decisions and not any(_field(row, "decision", "") == "kept" for row in decisions):
            return "no_executable_gap_steps"
        return "research_budget_exhausted"
    if verdict == "revise":
        return "rewrite_budget_exhausted"
    if verdict == "error" or not verdict:
        return "audit_error"
    return "unknown_verdict"


class FinalizeAuditNode:
    """终态节点：写 ``final_audit_status`` / ``delivery_status``，不产生新数据。"""

    def __init__(self, settings):
        self.settings = settings

    async def __call__(self, state):
        node_started = time.monotonic()
        try:
            if hasattr(state, "model_dump"):
                s = state.model_dump(exclude_none=False)
            else:
                s = state

            critique = s.get("critique")
            errors = list(s.get("errors") or [])
            remaining_budget = None
            if s.get("budget_deadline") is not None:
                try:
                    remaining_budget = float(s["budget_deadline"]) - time.monotonic()
                except (TypeError, ValueError):
                    remaining_budget = None
            blocked_reason = infer_blocked_reason(s, critique, self.settings)
            audit_status, delivery_status, reason = resolve_delivery(
                critique,
                errors,
                blocked_reason=blocked_reason,
                gap_key_decisions=s.get("gap_key_decisions"),
            )

            degraded_report = None
            degraded_issues: list[str] = []
            degraded_codes: list[str] = []
            # Only an exhausted, non-error audit may enter the controlled
            # degraded path.  The helper is deterministic and returns a
            # rebuilt report, so no unverified model prose survives.
            if delivery_status == "blocked" and s.get("report") is not None:
                degraded_report, degraded_issues, degraded_codes = build_degraded_report(
                    s, s.get("report"), critique
                )
                if degraded_report is not None:
                    delivery_status = "degraded"
                    reason = (
                        "degraded_partial_evidence: 仅交付可验证 claim；"
                        + "；".join(degraded_issues[:5])
                        if degraded_issues
                        else "degraded_partial_evidence: 仅交付可验证 claim"
                    )
                    blocked_reason = ""

            unresolved = list(dict.fromkeys(unresolved_issue_summary(critique) + degraded_issues))
            gap_decisions = s.get("gap_key_decisions") or _field(critique, "gap_key_decisions", []) or []
            terminal_codes = list(dict.fromkeys([*(s.get("reason_codes") or []), *degraded_codes]))
            if blocked_reason and blocked_reason not in terminal_codes and delivery_status != "verified":
                terminal_codes.append(str(blocked_reason))
            elapsed = elapsed_ms(s)
            s["elapsed_ms"] = elapsed
            run_log.log_finalize(
                s,
                final_audit_status=audit_status,
                delivery_status=delivery_status,
                reason=reason,
                unresolved_issues=unresolved,
                blocked_reason=blocked_reason,
                research_round_count=int(s.get("research_round_count", 0) or 0),
                rewrite_count=int(s.get("rewrite_count", 0) or 0),
                remaining_budget=remaining_budget,
                gap_key_decisions=gap_decisions,
                executable_gap_steps=s.get("executable_gap_steps"),
                reason_codes=terminal_codes,
            )
            if delivery_status != "verified":
                logger.warning(
                    "finalize_audit: delivery_status=%s final_audit_status=%s（不持久化，errors=%d）",
                    delivery_status,
                    audit_status,
                    len(errors),
                )

            out: dict[str, Any] = {
                "final_audit_status": audit_status,
                "delivery_status": delivery_status,
                "delivery_reason": reason,
                "blocked_reason": blocked_reason,
                "remaining_budget": remaining_budget,
                "unresolved_issues": unresolved,
                "reason_codes": terminal_codes,
                "elapsed_ms": elapsed,
            }
            # blocked / failed 时**清空正文报告**：未经审计的报告不允许作为结论返回。
            # 失败原因留在 critique（reviewer 可读）与 final_audit_status 里。
            if delivery_status == "degraded":
                out["report"] = degraded_report
                out["degraded_reason_codes"] = terminal_codes
            elif delivery_status != "verified" and s.get("report") is not None:
                out["report"] = None
            out["stage"] = s.get("stage") or "finalize"
            run_log.emit("stage", run_id=s.get("run_id"), stage="finalize", status=delivery_status,
                         elapsed_ms=round((time.monotonic()-node_started)*1000, 1), remaining_budget=remaining_budget)
            return out
        except Exception as exc:  # noqa: BLE001 — 终态节点不能把图炸掉
            logger.error("finalize_audit failed error_type=%s", type(exc).__name__)
            return {
                "final_audit_status": "error",
                "delivery_status": "failed",
                "blocked_reason": "audit_error",
                # Exception text can contain upstream payloads or user query
                # fragments. Keep the response diagnostic structured and
                # generic; the logger already records only the exception type.
                "delivery_reason": "audit_error: finalize audit failed",
                "report": None,
                "errors": ["finalize audit failed"],
            }
