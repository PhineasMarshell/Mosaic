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
from typing import Any

logger = logging.getLogger("app.graph.run_log")

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
    "blocked_reason",
    "note",
    "claim",
    "missing_points",
    "unsupported_claims",
    "intent_task",
    "unresolved_issues",
    "query",
    "text",
    "content",
    "prompt",
    "keywords",
}


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
        # 工具执行行是固定的小 schema；保留其全部诊断字段（尤其是
        # datum_count / duration_ms / partial），否则普通字典的键数截断会让
        # 回放误判工具没有返回数据。
        if "tool_key" in value and "status" in value:
            return {
                k: _redact_sensitive(v) if str(k) in _SENSITIVE_FIELDS else sanitize(v, _depth + 1)
                for k, v in value.items()
            }
        return {
            k: _redact_sensitive(v) if str(k) in _SENSITIVE_FIELDS else sanitize(v, _depth + 1)
            for k, v in clip_args(value).items()
        }
    return value


def emit(event: str, *, run_id: str | None = None, level: int = logging.INFO, **fields: Any) -> dict:
    """打一条单行 JSON 结构化日志并返回 payload（测试可直接断言 payload）。

    所有字段先过 ``sanitize``：调用点忘了截断也不会泄漏大文本 / 完整会话史。
    """
    payload = {"ts": round(time.time(), 3), "event": event, "run_id": run_id}
    payload.update({k: _redact_sensitive(v) if k in _SENSITIVE_FIELDS else sanitize(v) for k, v in fields.items()})
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
    return str(_field(critique, "verdict", "") or "").strip().lower()


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
    from app.gateway.tool_registry import resolve_tool
    from app.gateway.arguments import canonicalize_tool_arguments

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
        intent_domain=_field(intent, "domain"),
        intent_task=_field(intent, "task"),
        steps=steps,
        filtered_keys=list(filtered_keys or []),
        forced_gap_keys=list(forced_gap or []),
        prefix_keys=list(prefix_keys or []),
        unexecutable_keys=list(unexecutable_keys or []),
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
        rows.append(
            {
                "tool_key": _field(r, "tool_key"),
                "operation_id": _field(r, "operation_id") or _field(r, "tool"),
                "source_tool": _field(r, "operation_id") or _field(r, "tool"),
                "tool": _field(r, "tool"),
                # 放在 sanitize 的前 8 个字段内，确保关键耗时不会被字典截断。
                "duration_ms": _field(r, "duration_ms"),
                "arguments": clip_args(arguments),
                "status": _field(r, "status"),
                "partial": bool(_field(r, "partial", False)),
                "datum_count": len(normalized),
                "note": clip(_field(r, "note"), 80),
                "cache": cache or None,
            }
        )
    return emit("executions", run_id=_run_id_of(state), category=category, results=rows)


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
                "required_tool_keys": clip_list(_field(i, "required_tool_keys")),
                "required_coverage": _field(i, "required_coverage", {}) or {},
            }
            for i in issues
        ],
        verdict_conflicts=list(conflicts or []),
        gap_key_decisions=decisions,
        gap_raw=gap_raw,
        gap_kept=gap_kept,
        gap_dropped=gap_dropped,
        context_truncation=context_truncation or {},
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
        gap_key_decisions=gap_key_decisions or [],
        executable_gap_steps=executable_gap_steps or [],
    )


def log_delivery(state: Any, *, delivery_status: str, persisted: bool, sink: str) -> None:
    """最终交付决策：是否持久化、走的哪条路径（SSE / sync）。

    verified 结果也可能因存储故障而 persisted=False；记录实际写入结果。
    """
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
        gap_key_decisions=_field(state, "gap_key_decisions", []) or [],
        executable_gap_steps=_field(state, "executable_gap_steps", []) or [],
        blocked_reason=clip(_field(state, "blocked_reason", "")),
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
    )
