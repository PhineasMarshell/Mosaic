"""阶段 0：结构化运行摘要日志（单行 JSON）。

目的（docs/audit-reliability-remediation-plan.md §4 阶段 0）：一次运行必须能从日志
还原「计划了什么、实际跑了什么、缺口为什么没被补上、最终为什么交付 / 没交付」。

设计约束（方案 §4 阶段 0 第 3 条）：
- **绝不记录 API key、完整会话历史或原始大 payload**；所有文本与数组都有长度上限，
  截断时显式标注 ``...(+N)``，不允许"看起来像全量、其实被砍过"。
- 只打摘要，不打 ``raw``（gateway 原始响应可能是几百 KB）。

事件命名（稳定契约，调用方/日志检索可依赖）：
``run_start`` / ``plan`` / ``executions`` / ``critic`` / ``route`` / ``finalize`` /
``persistence`` / ``delivery``。

关于 ``errors``：本模块刻意**不提供**任何"errors 为空即报告可信"的判据。
运行是否可信只看 ``delivery_status``（见 app/models/response.py）。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger("app.graph.run_log")
request_run_id: ContextVar[str | None] = ContextVar("request_run_id", default=None)

#: 单个文本字段的截断长度
MAX_TEXT = 200
#: 单个数组最多保留的元素个数
MAX_ITEMS = 10
#: arguments 最多保留的键个数
MAX_ARGS = 8


def new_run_id() -> str:
    """生成一次调查的短 run_id（图内跨节点传递，落到每条结构化日志上）。"""
    return uuid.uuid4().hex[:12]


# ------------------------------------------------------------------ #
# 截断工具                                                             #
# ------------------------------------------------------------------ #


def clip(value: Any, limit: int = MAX_TEXT) -> str:
    """文本截断；过长时显式标注省略了多少字符。"""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...(+{len(text) - limit})"


def redact_text(value: Any) -> str:
    """Record text length without exposing text or a guessable digest."""
    text = "" if value is None else str(value)
    return f"<redacted len={len(text)}>"


_SENSITIVE_FIELDS = {
    "question",
    "conversation_id",
    "user_id",
    "answer_summary",
    "error",
    "errors",
    "delivery_reason",
    "note",
    "claim",
    "rationale",
    "missing_points",
    "unsupported_claims",
    "intent_task",
    "unresolved_issues",
    "query",
    "text",
    "content",
    "prompt",
    "keywords",
    "q",
    "keyword",
    "description",
    "reason",
}

_ROUTE_REASON_CODES = frozenset({
    "audit_passed", "revise_allowed", "research_allowed", "research_budget_available",
    "already_satisfied", "not_visible", "budget_truncated", "duplicated", "invalid", "executable",
})

_BLOCKED_REASONS = frozenset(
    {
        "research_budget_exhausted",
        "rewrite_budget_exhausted",
        "no_executable_gap_steps",
        "time_budget_insufficient",
        "tool_call_budget_insufficient",
        "no_report_to_revise",
        "critic_error",
        "audit_error",
        "unknown_verdict",
        "budget_truncated",
    }
)


def _safe_blocked_reason(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return text if text in _BLOCKED_REASONS else redact_text(text)


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (list, tuple)):
        out = [redact_text(item) if isinstance(item, str) else _redact_sensitive(item) for item in value[:MAX_ITEMS]]
        if len(value) > MAX_ITEMS:
            out.append(f"...(+{len(value) - MAX_ITEMS} more redacted)")
        return out
    if isinstance(value, dict):
        return {str(key): _redact_sensitive(item) for key, item in list(value.items())[:MAX_ARGS]}
    return redact_text(value)


def clip_list(values: Any, limit: int = MAX_ITEMS) -> list:
    """数组截断；过长时以 ``...(+N more)`` 结尾（不静默丢内容）。"""
    if not isinstance(values, (list, tuple)):
        return []
    out = [clip(v) if isinstance(v, str) else v for v in values[:limit]]
    rest = len(values) - limit
    if rest > 0:
        out.append(f"...(+{rest} more)")
    return out


def clip_args(arguments: Any, limit: int = MAX_ARGS) -> dict:
    """调用参数截断：只保留前 ``limit`` 个键，并标出被省略的键名。"""
    if not isinstance(arguments, dict):
        return {}
    out: dict = {}
    for key in list(arguments)[:limit]:
        val = arguments[key]
        out[key] = clip(val) if isinstance(val, str) else val
    rest = list(arguments)[limit:]
    if rest:
        out["_dropped_arg_keys"] = [str(k) for k in rest[:MAX_ITEMS]]
    return out


# ------------------------------------------------------------------ #
# 事件发射                                                             #
# ------------------------------------------------------------------ #


def sanitize(value: Any, _depth: int = 0) -> Any:
    """递归脱敏：长文本、长数组、长字典值一律截断并标注省略量。

    放在 ``emit`` 这一层（而不是每个调用点）是有意的：**脱敏必须是默认行为**，
    任何一个新调用点忘了 clip 都不会把大 payload 或长文本直接写进日志。
    """
    if _depth > 6:
        return "...(depth limit)"
    if isinstance(value, str):
        return clip(value)
    if isinstance(value, (list, tuple)):
        return [sanitize(v, _depth + 1) for v in clip_list(list(value))]
    if isinstance(value, dict):
        # 与 clip_args 同一语义：键数有上限，省略的键名显式标出来
        from app.gateway.arguments import redact_audit_arguments

        if "arguments" in value:
            value = {**value, "arguments": redact_audit_arguments(value["arguments"])}
        # 工具执行行是固定的小 schema；保留其全部诊断字段（尤其是
        # datum_count / duration_ms / partial），否则普通字典的键数截断会让
        # 回放误判工具没有返回数据。
        if ("tool_key" in value and "status" in value) or ("kind" in value and "action" in value):
            out = {}
            for k, v in value.items():
                key = str(k)
                if key == "datums" and isinstance(v, (list, tuple)):
                    kept = [sanitize(item, _depth + 1) for item in v[:80]]
                    if len(v) > 80:
                        kept.append(f"...(+{len(v) - 80} more)")
                    out[key] = kept
                elif key in {"evidence_ids", "claim_refs"} and isinstance(v, (list, tuple)):
                    kept = [sanitize(item, _depth + 1) for item in v[:240]]
                    if len(v) > 240:
                        kept.append(f"...(+{len(v) - 240} more)")
                    out[key] = kept
                elif key == "blocked_reason":
                    out[key] = _safe_blocked_reason(v)
                elif key == "reason" and isinstance(v, str) and v in _BLOCKED_REASONS | _ROUTE_REASON_CODES:
                    out[key] = v
                elif key in _SENSITIVE_FIELDS:
                    out[key] = _redact_sensitive(v)
                else:
                    out[key] = sanitize(v, _depth + 1)
            return out
        return {
            k: _safe_blocked_reason(v)
            if str(k) == "blocked_reason"
            else v
            if str(k) == "reason" and isinstance(v, str) and v in _BLOCKED_REASONS | _ROUTE_REASON_CODES
            else _redact_sensitive(v)
            if str(k) in _SENSITIVE_FIELDS
            else sanitize(v, _depth + 1)
            for k, v in clip_args(value).items()
        }
    return value


def emit(event: str, *, run_id: str | None = None, level: int = logging.INFO, **fields: Any) -> dict:
    """打一条单行 JSON 结构化日志并返回 payload（测试可直接断言 payload）。

    所有字段先过 ``sanitize``：调用点忘了截断也不会泄漏大文本 / 完整会话史。
    """
    payload = {"ts": round(time.time(), 3), "event": event, "run_id": run_id}
    payload.update(
        {
            k: _safe_blocked_reason(v)
            if k == "blocked_reason"
            else v
            if k == "reason" and isinstance(v, str) and v in _BLOCKED_REASONS | _ROUTE_REASON_CODES
            else _redact_sensitive(v)
            if k in _SENSITIVE_FIELDS
            else sanitize(v)
            for k, v in fields.items()
        }
    )
    logger.log(level, json.dumps(payload, ensure_ascii=False, default=str))
    return payload


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """兼容 dict / pydantic 对象取值（与 critic._field 同语义）。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _run_id_of(state: Any) -> str | None:
    return _field(state, "run_id")


def _verdict_of(critique: Any) -> str:
    verdict = str(_field(critique, "verdict", "") or "").strip().lower()
    return verdict if verdict in {"", "pass", "revise", "research_more", "error"} else "invalid"


# ------------------------------------------------------------------ #
# 各节点调用点                                                          #
# ------------------------------------------------------------------ #


def log_run_start(state: Any) -> None:
    """研究开始：问题、显式 domain。intent domain 要等 supervisor 产出后才知道，
    因此在 plan 事件里补 ``intent_domain``。"""
    emit(
        "run_start",
        run_id=_run_id_of(state),
        question=clip(_field(state, "question", "")),
        explicit_domain=_field(state, "domain"),
        conversation_id=_field(state, "conversation_id"),
    )


def log_plan(
    state: Any,
    plan: Any,
    *,
    filtered_keys: list | None = None,
    forced_gap: list | None = None,
    prefix_keys: list | None = None,
    unexecutable_keys: list | None = None,
) -> None:
    """计划了什么：每个 step 的 tool_key / operationId / arguments。

    这是"为什么没补工具"的第一现场——被域守卫过滤掉的 key 与代码级补齐的 key
    都在这里，之后才是实际执行结果。

    阶段 3 追加两个字段，让"计划与实际执行不一致"永远可解释：

    - ``prefix_keys``：代码注入的 A 股市场级最低证据集 key（不由 LLM 决定）；
    - ``unexecutable_keys``：计划里被剔除的、需要 symbol 却拿不到 symbol 的
      step（旧行为是执行层静默跳过 → 假覆盖）。
    """
    from app.gateway.arguments import canonicalize_tool_arguments
    from app.gateway.tool_registry import resolve_tool

    steps: list[dict] = []
    for step in _field(plan, "steps", []) or []:
        key = _field(step, "tool_key", "")
        try:
            operation_id = resolve_tool(key).tool_name
            arguments = canonicalize_tool_arguments(operation_id, _field(step, "arguments", {}) or {})
        except Exception:
            operation_id = None
            arguments = _field(step, "arguments", {}) or {}
        steps.append(
            {
                "tool_key": key,
                "operation_id": operation_id,
                "source_tool": operation_id,
                "arguments": clip_args(arguments),
                "priority": _field(step, "priority"),
            }
        )

    intent = _field(plan, "intent")
    emit(
        "plan",
        run_id=_run_id_of(state),
        requested_date=_field(plan, "requested_date") or _field(state, "requested_date"),
        as_of_date=_field(plan, "as_of_date") or _field(state, "planned_as_of_date"),
        market_closed=_field(plan, "market_closed")
        if _field(plan, "market_closed") is not None
        else _field(state, "market_closed"),
        intent_domain=_field(intent, "domain"),
        intent_task=_field(intent, "task"),
        steps=steps,
        filtered_keys=list(filtered_keys or []),
        forced_gap_keys=list(forced_gap or []),
        prefix_keys=list(prefix_keys or []),
        unexecutable_keys=list(unexecutable_keys or []),
        required_tool_keys=list(_field(plan, "required_tool_keys", []) or []),
        missing_required_tool_keys=list(_field(plan, "missing_required_tool_keys", []) or []),
    )


def log_executions(state: Any, results: Any, *, category: str | None = None) -> None:
    """实际跑了什么：status / partial / normalized 条数 / 缓存命中。"""
    from app.gateway.arguments import canonicalize_tool_arguments

    rows: list[dict] = []
    for r in results or []:
        normalized = _field(r, "normalized", []) or []
        cache = getattr(r, "_cache_info", None) or {}
        operation_id = _field(r, "operation_id") or _field(r, "tool")
        arguments = _field(r, "arguments", {}) or {}
        try:
            arguments = canonicalize_tool_arguments(operation_id, arguments)
        except Exception:
            pass
        business_datum_count = _field(r, "business_datum_count")
        if business_datum_count is None:
            business_datum_count = sum(
                1 for d in normalized if not bool(_field(d, "structural_metadata", False))
            )
        structural_metadata_count = _field(r, "structural_metadata_count")
        if structural_metadata_count is None:
            structural_metadata_count = len(normalized) - int(business_datum_count)
        date_annotation = _field(r, "date_annotation")
        # 逐 datum 的日期来源（observed/envelope/request_context/contract_snapshot/unknown）。
        date_annotations = sorted({
            _field(d, "date_annotation") for d in normalized if _field(d, "date_annotation")
        })
        authority_set = sorted({
            _field(d, "authority") for d in normalized if _field(d, "authority")
        })
        semantic_annotation_counts: dict[str, int] = {}
        for d in normalized:
            ann = _field(d, "semantic_annotation")
            if ann:
                semantic_annotation_counts[ann] = semantic_annotation_counts.get(ann, 0) + 1
        rows.append(
            {
                "tool_key": _field(r, "tool_key"),
                "operation_id": _field(r, "operation_id") or _field(r, "tool"),
                "source_tool": _field(r, "operation_id") or _field(r, "tool"),
                "tool": _field(r, "tool"),
                # 放在 sanitize 的前 8 个字段内，确保关键耗时不会被字典截断。
                "duration_ms": _field(r, "duration_ms"),
                "cache_hit": bool(_field(r, "cache_hit", False)),
                "refresh_requested": bool(_field(r, "refresh_requested", False)),
                "arguments": clip_args(arguments),
                "status": _field(r, "status"),
                "partial": bool(_field(r, "partial", False)),
                "result_partial": bool(_field(r, "partial", False)),
                "reason_codes": _field(r, "reason_codes", []) or [],
                "upstream_status": _field(r, "upstream_status"),
                "attempts": _field(r, "attempts", []) or [],
                "primary_source": _field(r, "primary_source"),
                "fallback_source": _field(r, "fallback_source"),
                "fallback_reason": _field(r, "fallback_reason"),
                "source_switch": bool(_field(r, "source_switch", False)),
                "source": sorted({_field(d, "source") for d in normalized if _field(d, "source")}),
                "instrument": sorted({_field(d, "instrument") for d in normalized if _field(d, "instrument")}),
                "as_of_date": _field(r, "as_of_date"),
                "retrieved_at": _field(r, "retrieved_at"),
                # 语义补标注审计：业务/结构计数、证据是否创建、fallback 逐门失败、
                # 逐行日期来源、authority 集合与语义标注分布。
                "business_datum_count": int(business_datum_count),
                "structural_metadata_count": int(structural_metadata_count),
                "evidence_created": bool(_field(r, "evidence_created", False)),
                "fallback_provenance_failures": _field(r, "fallback_provenance_failures", []) or [],
                "date_annotation": date_annotation,
                "date_annotations": date_annotations,
                "authority_set": authority_set,
                "semantic_annotation_counts": semantic_annotation_counts,
                "datum_count": len(normalized),
                "datums": [
                    {
                        "metric": _field(d, "metric"),
                        "as_of_date": _field(d, "as_of_date") or _field(d, "timestamp"),
                        "instrument": _field(d, "instrument"),
                        "instrument_name": _field(d, "instrument_name"),
                        "source": _field(d, "source") or _field(d, "authority"),
                        "coverage": _field(d, "coverage") or _field(d, "instrument_scope"),
                        "partial": bool(_field(d, "partial", False)),
                        "status": _field(d, "status"),
                    }
                    for d in normalized[:80]
                ],
                "note": clip(_field(r, "note"), 80),
                "cache": cache or None,
            }
        )
    return emit("executions", run_id=_run_id_of(state), category=category, results=rows)


def log_reasoning_context(
    state: Any,
    *,
    ledger_count: int,
    selected_count: int,
    evidence_ids: list[str],
    prompt_chars: int | None,
    context_build_ms: float | None,
    llm_request_ms: float | None,
    parse_ms: float | None,
    completion_chars: int | None = None,
    finish_reason: str | None = None,
    parse_status: str | None = None,
    request_max_tokens: int | None = None,
    usage: Any = None,
    target_filter: dict | None = None,
    phase: str | None = None,
    tool_truncation: dict | None = None,
) -> None:
    """Record bounded Reasoning context/timing diagnostics without payload text."""
    emit(
        "reasoning_context",
        run_id=_run_id_of(state),
        phase=phase,
        ledger_evidence_count=int(ledger_count),
        selected_evidence_count=int(selected_count),
        evidence_ids=list(evidence_ids),
        prompt_chars=prompt_chars,
        context_build_ms=None if context_build_ms is None else round(float(context_build_ms), 1),
        llm_request_ms=None if llm_request_ms is None else round(float(llm_request_ms), 1),
        parse_ms=None if parse_ms is None else round(float(parse_ms), 1),
        completion_chars=completion_chars,
        finish_reason=finish_reason,
        parse_status=parse_status,
        request_max_tokens=request_max_tokens,
        usage={
            "prompt_tokens": _field(usage, "prompt_tokens"),
            "completion_tokens": _field(usage, "completion_tokens"),
            "total_tokens": _field(usage, "total_tokens"),
        } if usage is not None else None,
        target_filter=target_filter or {},
        tool_truncation=tool_truncation or {},
    )


def log_evidence_fallback(
    state: Any,
    *,
    phase: str,
    fallback_enabled: bool,
    fallback_reason: str,
    candidate_count: int,
    accepted_count: int,
    rejected_count: int,
    rejection_reasons: dict | None = None,
    candidate_evidence_ids: dict | None = None,
    rejected_candidates: list | None = None,
    requested_date: str | None = None,
    target_instrument: str | None = None,
    finish_reason: str | None = None,
    parse_status: str | None = None,
    applied: bool = False,
) -> dict:
    """记录 evidence-only fallback 的准入判定（P13 §一.2/§一.3）。

    每个候选 evidence 的准入/拒绝原因都在这里落档：只有"有多少条被拒"而
    没有"为什么被拒"时，blocked 运行无法复盘是哪一道门禁挡住的。

    `applied=False` 表示这次判定只是审计留痕（正常成文路径也记一份），
    并非 fallback 真的交付了报告——不能让"有合格候选"被读成"降级生效"。
    """
    return emit(
        "evidence_fallback",
        run_id=_run_id_of(state),
        phase=phase,
        applied=bool(applied),
        fallback_enabled=bool(fallback_enabled),
        fallback_reason=str(fallback_reason or ""),
        candidate_count=int(candidate_count),
        accepted_count=int(accepted_count),
        rejected_count=int(rejected_count),
        rejection_reasons=dict(rejection_reasons or {}),
        candidate_evidence_ids=dict(candidate_evidence_ids or {}),
        rejected_candidates=list(rejected_candidates or []),
        requested_date=requested_date,
        target_instrument=target_instrument,
        finish_reason=finish_reason,
        parse_status=parse_status,
    )


def log_critic_context(
    state: Any,
    *,
    prompt_chars: int,
    ledger_count: int,
    evidence_ids: list[str],
) -> None:
    """Record Critic input size and ledger identity, never the prompt body."""
    emit(
        "critic_context",
        run_id=_run_id_of(state),
        prompt_chars=int(prompt_chars),
        ledger_evidence_count=int(ledger_count),
        evidence_ids=list(evidence_ids),
    )


def log_critic(
    state: Any,
    critique: Any,
    *,
    conflicts: list | None = None,
    gap_key_decisions: Any = None,
    context_truncation: dict | None = None,
) -> None:
    """每次 Critic 裁决：verdict / 缺口 / 无证据表述 / 结构化 issue / 动作冲突。

    ``context_truncation`` 记录这次审计上下文被截断了什么（阶段 4③）：
    截断信息必须进 telemetry，否则"Critic 为什么没发现某条无据论断"不可解释。
    """
    issues = _field(critique, "issues", []) or []
    decisions = gap_key_decisions or _field(critique, "gap_key_decisions", []) or []
    gap_raw = [d.get("key") for d in decisions if isinstance(d, dict) and d.get("key")]
    gap_kept = [d.get("key") for d in decisions if isinstance(d, dict) and d.get("decision") == "kept"]
    gap_dropped = [
        {"key": d.get("key"), "reason": d.get("reason")}
        for d in decisions
        if isinstance(d, dict) and d.get("decision") == "dropped"
    ]
    emit(
        "critic",
        run_id=_run_id_of(state),
        verdict=_verdict_of(critique),
        reason=redact_text(_field(critique, "reason", "")),
        missing_points=clip_list(_field(critique, "missing_points", [])),
        missing_tool_keys=clip_list(_field(critique, "missing_tool_keys", [])),
        unsupported_claims=clip_list(_field(critique, "unsupported_claims", [])),
        issues=[
            {
                "kind": _field(i, "kind"),
                "severity": _field(i, "severity"),
                "action": _field(i, "action"),
                "claim": clip(_field(i, "claim")),
                "rationale": clip(_field(i, "rationale")),
                "required_tool_keys": clip_list(_field(i, "required_tool_keys")),
                "required_coverage": _field(i, "required_coverage", {}) or {},
                "reason_codes": _field(i, "reason_codes", []) or [],
            }
            for i in issues
        ],
        verdict_conflicts=list(conflicts or []),
        gap_key_decisions=decisions,
        gap_raw=gap_raw,
        gap_kept=gap_kept,
        gap_dropped=gap_dropped,
        context_truncation=context_truncation or {},
        claims=[
            {"claim": _field(c, "claim"), "claim_type": _field(c, "claim_type"),
             "evidence_ids": _field(c, "evidence_ids", []), "instrument": _field(c, "instrument"),
             "as_of_date": _field(c, "as_of_date")}
            for c in (_field(_field(state, "report"), "claims", []) or [])
        ],
        evidence_identity=[
            {
                "id": _field(e, "id"),
                "tool_key": _field(e, "tool_key"),
                "operation_id": _field(e, "operation_id") or _field(e, "source_tool"),
                "source_tool": _field(e, "source_tool"),
            }
            for e in (_field(state, "evidence", []) or [])
        ],
    )


def log_route(
    state: Any,
    decision: str,
    *,
    rewrite_count: int = 0,
    research_round_count: int = 0,
    revision_count: int | None = None,
    max_rewrites: int = 0,
    max_research_rounds: int = 0,
    reason: str | None = None,
    budget_remaining: float | None = None,
    required_minimum: float | None = None,
    tool_calls_remaining: int | None = None,
    required_tool_calls: int | None = None,
    gap_key_decisions: Any = None,
    executable_gap_steps: Any = None,
    blocked_reason: str | None = None,
) -> None:
    """路由决定 + 轮次计数（路由日志让"为什么提前结束"可解释）。

    阶段 6：两条回环路径分开计数，并把**当时生效的上限**一起记下来 ——
    事后看"为什么没再补一轮"时不必去猜当时的部署配置是多少。
    ``reason`` 是机器可读的原因码（revise_allowed / rewrite_budget_exhausted /
    research_budget_exhausted / insufficient_remaining_budget / ...）。
    """
    total = rewrite_count + research_round_count if revision_count is None else int(revision_count)
    emit(
        "route",
        run_id=_run_id_of(state),
        verdict=_verdict_of(_field(state, "critique")),
        decision=decision,
        reason=reason,
        rewrite_count=rewrite_count,
        research_round_count=research_round_count,
        revision_count=total,
        max_rewrites=max_rewrites,
        max_research_rounds=max_research_rounds,
        budget_remaining=budget_remaining,
        required_minimum=required_minimum,
        tool_calls_remaining=tool_calls_remaining,
        required_tool_calls=required_tool_calls,
        gap_key_decisions=gap_key_decisions or [],
        executable_gap_steps=executable_gap_steps or [],
        blocked_reason=blocked_reason,
    )


def log_llm_call(
    state: Any,
    *,
    node: str,
    model: str | None = None,
    usage: Any = None,
    duration_ms: float | None = None,
    phase: str | None = None,
    request_max_tokens: int | None = None,
    completion_chars: int | None = None,
    finish_reason: str | None = None,
) -> dict:
    """一次 LLM 调用的用量与耗时（阶段 6 指标的数据源）。

    ``usage`` 直接来自 SDK 的 ``response.usage``；打桩客户端 / 兼容端点可能没有，
    此时记 ``None`` 而不是 0 —— "没量到"和"用了 0 token"是两件事。
    """
    return emit(
        "llm",
        run_id=_run_id_of(state),
        node=node,
        phase=phase,
        model=model,
        prompt_tokens=_field(usage, "prompt_tokens"),
        completion_tokens=_field(usage, "completion_tokens"),
        total_tokens=_field(usage, "total_tokens"),
        duration_ms=None if duration_ms is None else round(float(duration_ms), 1),
        request_max_tokens=request_max_tokens,
        completion_chars=completion_chars,
        finish_reason=finish_reason,
    )


def log_finalize(
    state: Any,
    *,
    final_audit_status: str,
    delivery_status: str,
    reason: str,
    unresolved_issues: list | None = None,
    blocked_reason: str | None = None,
    research_round_count: int = 0,
    rewrite_count: int = 0,
    remaining_budget: float | None = None,
    gap_key_decisions: Any = None,
    executable_gap_steps: Any = None,
    reason_codes: Any = None,
) -> None:
    """finalize_audit 节点的判定依据（为什么 verified / blocked / failed）。"""
    emit(
        "finalize",
        run_id=_run_id_of(state),
        final_audit_status=final_audit_status,
        delivery_status=delivery_status,
        reason=redact_text(reason),
        unresolved_issues=clip_list(unresolved_issues),
        blocked_reason=clip(blocked_reason),
        research_round_count=research_round_count,
        rewrite_count=rewrite_count,
        remaining_budget=remaining_budget,
        elapsed_ms=_field(state, "elapsed_ms"),
        gap_key_decisions=gap_key_decisions or [],
        executable_gap_steps=executable_gap_steps or [],
        reason_codes=reason_codes or [],
    )


def log_delivery(state: Any, *, delivery_status: str, persisted: bool, sink: str) -> None:
    """最终交付决策：是否持久化、走的哪条路径（SSE / sync）。

    verified 结果也可能因存储故障而 persisted=False；记录实际写入结果。
    """
    results = _field(state, "results", None) or _field(state, "tool_results", []) or []
    source_switch = any(bool(_field(result, "source_switch", False)) for result in results)
    emit(
        "delivery",
        run_id=_run_id_of(state),
        delivery_status=delivery_status,
        persisted=persisted,
        sink=sink,
        error_count=len(_field(state, "errors", []) or []),
        final_audit_status=_field(state, "final_audit_status"),
        delivery_reason=clip(_field(state, "delivery_reason", "")),
        research_round_count=int(_field(state, "research_round_count", 0) or 0),
        rewrite_count=int(_field(state, "rewrite_count", 0) or 0),
        remaining_budget=_field(state, "remaining_budget"),
        elapsed_ms=_field(state, "elapsed_ms"),
        gap_key_decisions=_field(state, "gap_key_decisions", []) or [],
        executable_gap_steps=_field(state, "executable_gap_steps", []) or [],
        blocked_reason=clip(_field(state, "blocked_reason", "")),
        issues=[
            {"kind": _field(i, "kind"), "severity": _field(i, "severity"),
             "action": _field(i, "action"), "reason_codes": _field(i, "reason_codes", []) or []}
            for i in (_field(_field(state, "critique"), "issues", []) or [])
        ],
        error_category=_field(state, "error_category"),
        exception_type=_field(state, "exception_type"),
        stage=_field(state, "stage"),
        reason_codes=_field(state, "reason_codes", []) or [],
        source_switch=source_switch,
    )


def log_persistence(
    state: Any,
    *,
    attempt: int,
    max_attempts: int,
    outcome: str,
    retryable: bool = False,
    idempotent: bool = False,
    error: str = "",
    error_category: str | None = None,
) -> None:
    """Record each persistence attempt without exposing response payloads."""
    emit(
        "persistence",
        run_id=_run_id_of(state),
        attempt=attempt,
        max_attempts=max_attempts,
        outcome=outcome,
        retryable=retryable,
        idempotent=idempotent,
        error=clip(error),
        error_category=error_category,
    )
