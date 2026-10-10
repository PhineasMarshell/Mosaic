"""阶段 6④：把结构化运行日志汇总成"能不能放开灰度"需要的指标。

数据源就是 ``app/graph/run_log.py`` 打出的单行 JSON 事件流（logger
``app.graph.run_log``，每条一个 JSON object），本模块**只读**事件、不产生新日志。

口径（与方案 §4 阶段 6④ 一一对应）：

- 审计通过率：``verdict == "pass"`` 的运行数 / 运行总数；
- 各 verdict 分布：每次 Critic 裁决的 verdict（含 ``error``）；
- 耗尽率：``final_audit_status`` 为 ``revise_exhausted`` / ``research_exhausted`` 的比例；
- 降级 / 阻断率：``delivery_status`` 为 ``degraded`` / ``blocked`` 的比例
  （``failed`` 单列 —— 它是"没跑成"，不是"跑成了但可信度不够"）；
- 每类缺口工具补齐成功率：被代码级强制补进计划的 key（plan 事件的
  ``forced_gap_keys``）中，最后一次 Critic 裁决**不再**把它当缺口的比例，按 key 分组；
- 每次研究的 token / 工具耗时：``llm`` 事件的 token 与 ``duration_ms``、
  ``executions`` 事件的每条 datum 行 ``duration_ms``。

三态数字是刻意的：``None`` = 没有数据（不要当成 0），``0.0`` = 真的量到 0。
灰度决策用前者会被"看起来通过率 0%"带偏，用后者才会正确显示"没采到样本"。
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

#: `delivery_status` 里属于"跑完了但可信度不够"的两个终态。
DEGRADED_OR_BLOCKED = ("degraded", "blocked")
#: `final_audit_status` 里属于"用完了回环额度仍不通过"的两个终态。
EXHAUSTED_STATUSES = ("revise_exhausted", "research_exhausted")

_UNASSIGNED_RUN = "__unassigned__"


def parse_lines(lines: Iterable[str]) -> list[dict]:
    """解析 JSONL 事件流；坏行/非对象行**跳过**而不是抛异常。

    运行日志可能被别的库混进非本模块的日志行，一份"有一行坏了就什么都读不出来"
    的指标工具没有用。
    """
    events: list[dict] = []
    for line in lines:
        text = (line or "").strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and obj.get("logger") == "app.graph.run_log" and isinstance(obj.get("msg"), str):
            try:
                obj = json.loads(obj["msg"])
            except ValueError:
                continue
        if isinstance(obj, dict) and isinstance(obj.get("event"), str):
            events.append(obj)
    return events


def parse_file(path: str) -> list[dict]:
    """按 JSONL 读取一个运行日志文件。"""
    with open(path, encoding="utf-8") as handle:
        return parse_lines(handle)


def group_by_run(events: Iterable[dict]) -> dict[str, list[dict]]:
    """按 ``run_id`` 分组，保持事件原始顺序（同一 run 内事件就是时间序）。"""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        run_id = event.get("run_id")
        grouped[str(run_id) if run_id else _UNASSIGNED_RUN].append(event)
    return dict(grouped)


def _sum_optional(values: Iterable[Any]) -> float | None:
    """求和；一个都没有（或全是 None）时返回 None —— 不把"没量到"当成 0。"""
    total = 0.0
    seen = False
    for value in values:
        if value is None:
            continue
        try:
            total += float(value)
        except (TypeError, ValueError):
            continue
        seen = True
    return total if seen else None


def _last(events: list[dict], name: str) -> dict | None:
    for event in reversed(events):
        if event.get("event") == name:
            return event
    return None


def _all(events: list[dict], name: str) -> list[dict]:
    return [e for e in events if e.get("event") == name]


def _string_set(raw: Any) -> set[str]:
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {str(x) for x in raw if isinstance(x, (str, int)) and str(x)}


def _still_missing(critic_event: dict | None) -> set[str]:
    """一次 Critic 裁决里"仍未满足"的 key 集合。

    ``missing_tool_keys`` 是模型自己给的缺口；``gap_key_decisions`` 是代码层的
    缺口分类 —— 除 ``already_satisfied``（已拿到充分证据）外，其余（not_visible /
    duplicated / budget_truncated）都仍然算缺口。
    """
    if not critic_event:
        return set()
    missing = _string_set(critic_event.get("missing_tool_keys"))
    decisions = critic_event.get("gap_key_decisions")
    if isinstance(decisions, list):
        for record in decisions:
            if not isinstance(record, dict):
                continue
            key = record.get("key")
            if not key:
                continue
            if str(record.get("reason") or "") != "already_satisfied":
                missing.add(str(key))
    return missing


def summarize_run(events: list[dict], *, run_id: str | None = None) -> dict:
    """单个 run 的指标（输入是同一 run_id 的事件，顺序即时间序）。"""
    ordered = sorted(events, key=lambda e: float(e.get("ts") or 0.0))
    judges = _all(ordered, "critic")
    plans = _all(ordered, "plan")
    routes = _all(ordered, "route")
    executions = _all(ordered, "executions")
    llm_calls = _all(ordered, "llm")
    persistence = _all(ordered, "persistence")
    finalize = _last(ordered, "finalize")
    delivery = _last(ordered, "delivery")

    verdicts = [str(j.get("verdict") or "") for j in judges if j.get("verdict")]
    rows = [row for e in executions for row in (e.get("results") or []) if isinstance(row, dict)]

    # 缺口补齐：代码级补进计划的 key（forced_gap_keys）里，最后一次裁决不再当缺口的。
    forced: set[str] = set()
    for plan in plans:
        forced |= _string_set(plan.get("forced_gap_keys"))
    missing_at_end = _still_missing(judges[-1] if judges else None)
    backfilled = sorted(forced - missing_at_end)

    ts_values = [float(e["ts"]) for e in ordered if isinstance(e.get("ts"), (int, float))]
    duration = (max(ts_values) - min(ts_values)) if len(ts_values) >= 2 else 0.0

    return {
        "run_id": run_id if run_id is not None else ordered[0].get("run_id") if ordered else None,
        "event_count": len(ordered),
        "duration_seconds": round(duration, 3),
        "verdict": verdicts[-1] if verdicts else None,
        "verdicts": verdicts,
        "final_audit_status": (finalize or {}).get("final_audit_status"),
        "delivery_status": (delivery or {}).get("delivery_status") or (finalize or {}).get("delivery_status"),
        "rewrite_count": max((int(r.get("rewrite_count") or 0) for r in routes), default=0),
        "research_round_count": max((int(r.get("research_round_count") or 0) for r in routes), default=0),
        "route_reasons": [str(r.get("reason")) for r in routes if r.get("reason")],
        "tool_calls": len(rows),
        "tool_errors": sum(1 for row in rows if str(row.get("status")) == "error"),
        "tool_partial": sum(1 for row in rows if row.get("partial")),
        "tool_duration_ms": _sum_optional(row.get("duration_ms") for row in rows),
        "llm_calls": len(llm_calls),
        "llm_duration_ms": _sum_optional(e.get("duration_ms") for e in llm_calls),
        "prompt_tokens": _sum_optional(e.get("prompt_tokens") for e in llm_calls),
        "completion_tokens": _sum_optional(e.get("completion_tokens") for e in llm_calls),
        "total_tokens": _sum_optional(e.get("total_tokens") for e in llm_calls),
        "gap_attempted_keys": sorted(forced),
        "gap_backfilled_keys": backfilled,
        "gap_unresolved_keys": sorted(forced & missing_at_end),
        "error_count": int((delivery or {}).get("error_count") or 0),
        "error_category": (delivery or {}).get("error_category"),
        "persisted": (delivery or {}).get("persisted"),
        "persistence_retries": sum(1 for event in persistence if event.get("outcome") == "retry"),
        "persistence_outcome": persistence[-1].get("outcome") if persistence else None,
        "persistence_errors": [str(event["error_category"]) for event in persistence if event.get("error_category")],
    }


def _rate(numerator: int, denominator: int) -> float | None:
    """比例；分母为 0 时返回 None（"没样本"不是"通过率 0%"）。"""
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def aggregate_runs(runs: list[dict], *, unassigned: int = 0) -> dict:
    """把多个 run 的 ``summarize_run`` 结果汇总成发布决策用的指标。"""
    total = len(runs)
    verdict_dist = Counter(str(r.get("verdict") or "unknown") for r in runs)
    delivery_dist = Counter(str(r.get("delivery_status") or "unknown") for r in runs)
    status_dist = Counter(str(r.get("final_audit_status") or "none") for r in runs)
    error_categories = Counter(str(r["error_category"]) for r in runs if r.get("error_category"))
    persistence_outcomes = Counter(str(r["persistence_outcome"]) for r in runs if r.get("persistence_outcome"))
    persistence_errors = Counter(str(error) for r in runs for error in r.get("persistence_errors") or [])

    by_key: dict[str, dict[str, int]] = defaultdict(lambda: {"attempted": 0, "resolved": 0})
    attempted = resolved = 0
    for run in runs:
        backfilled = set(run.get("gap_backfilled_keys") or [])
        for key in run.get("gap_attempted_keys") or []:
            by_key[key]["attempted"] += 1
            attempted += 1
            if key in backfilled:
                by_key[key]["resolved"] += 1
                resolved += 1
    for stats in by_key.values():
        stats["rate"] = _rate(stats["resolved"], stats["attempted"])

    passes = verdict_dist.get("pass", 0)
    exhausted = sum(status_dist.get(status, 0) for status in EXHAUSTED_STATUSES)
    degraded_or_blocked = sum(delivery_dist.get(status, 0) for status in DEGRADED_OR_BLOCKED)

    tool_durations = [r["tool_duration_ms"] for r in runs if r.get("tool_duration_ms") is not None]
    tool_calls = sum(int(r.get("tool_calls") or 0) for r in runs)
    measured_calls = 0
    for run in runs:
        if run.get("tool_duration_ms") is not None:
            measured_calls += int(run.get("tool_calls") or 0)

    return {
        "runs": total,
        "unassigned_events": unassigned,
        "audit_pass_rate": _rate(passes, total),
        "verdict_distribution": dict(verdict_dist),
        "final_audit_status_distribution": dict(status_dist),
        "delivery_distribution": dict(delivery_dist),
        "error_category_distribution": dict(error_categories),
        "persistence_outcome_distribution": dict(persistence_outcomes),
        "persistence_error_distribution": dict(persistence_errors),
        "persistence_retries": sum(int(r.get("persistence_retries") or 0) for r in runs),
        "persisted_runs": sum(r.get("persisted") is True for r in runs),
        "exhaustion_rate": _rate(exhausted, total),
        "degraded_or_blocked_rate": _rate(degraded_or_blocked, total),
        "failed_rate": _rate(delivery_dist.get("failed", 0), total),
        "gap_backfill": {
            "attempted": attempted,
            "resolved": resolved,
            "rate": _rate(resolved, attempted),
            "by_key": dict(sorted(by_key.items())),
        },
        "tokens": {
            "runs_with_usage": sum(1 for r in runs if r.get("total_tokens") is not None),
            "prompt": _sum_optional(r.get("prompt_tokens") for r in runs),
            "completion": _sum_optional(r.get("completion_tokens") for r in runs),
            "total": _sum_optional(r.get("total_tokens") for r in runs),
        },
        "tool_duration_ms": {
            "runs_measured": len(tool_durations),
            "total": _sum_optional(tool_durations),
            "per_call_mean": (
                round(sum(tool_durations) / measured_calls, 1) if measured_calls else None
            ),
        },
        "tool_calls": tool_calls,
        "llm_calls": sum(int(r.get("llm_calls") or 0) for r in runs),
        "llm_duration_ms_total": _sum_optional(r.get("llm_duration_ms") for r in runs),
        "run_duration_seconds": {
            "mean": (
                round(sum(float(r.get("duration_seconds") or 0.0) for r in runs) / total, 3) if total else None
            ),
            "max": max((float(r.get("duration_seconds") or 0.0) for r in runs), default=None),
        },
        "route_reasons": dict(Counter(reason for r in runs for reason in r.get("route_reasons") or [])),
    }


def summarize_events(events: Iterable[dict]) -> dict:
    """事件流 → ``{"runs": [...], "summary": {...}}``（按 run_id 分组后聚合）。"""
    grouped = group_by_run(events)
    runs = [
        summarize_run(run_events, run_id=run_id)
        for run_id, run_events in grouped.items()
        if run_id != _UNASSIGNED_RUN
    ]
    unassigned = len(grouped.get(_UNASSIGNED_RUN, []))
    return {"runs": runs, "summary": aggregate_runs(runs, unassigned=unassigned)}


def render_report(summary: dict) -> str:
    """把 ``aggregate_runs`` 的结果渲染成一段可读文本（灰度评审时贴进记录）。"""
    def pct(value: Any) -> str:
        return "无样本" if value is None else f"{float(value) * 100:.1f}%"

    lines = [
        f"运行数: {summary.get('runs', 0)}"
        + (f"（另有 {summary['unassigned_events']} 条无 run_id 的事件未计入）" if summary.get("unassigned_events") else ""),
        f"审计通过率: {pct(summary.get('audit_pass_rate'))}",
        f"verdict 分布: {summary.get('verdict_distribution')}",
        f"终态分布: {summary.get('final_audit_status_distribution')}",
        f"交付分布: {summary.get('delivery_distribution')}",
        f"持久化: 成功运行数 {summary.get('persisted_runs')}，重试 {summary.get('persistence_retries')}，终态 {summary.get('persistence_outcome_distribution')}",
        f"错误类别: {summary.get('error_category_distribution')}，持久化错误: {summary.get('persistence_error_distribution')}",
        f"耗尽率: {pct(summary.get('exhaustion_rate'))}",
        f"降级/阻断率: {pct(summary.get('degraded_or_blocked_rate'))}  "
        f"failed: {pct(summary.get('failed_rate'))}",
    ]
    gap = summary.get("gap_backfill") or {}
    lines.append(f"缺口补齐成功率: {pct(gap.get('rate'))}（{gap.get('resolved', 0)}/{gap.get('attempted', 0)}）")
    for key, stats in (gap.get("by_key") or {}).items():
        lines.append(f"  - {key}: {pct(stats.get('rate'))}（{stats.get('resolved')}/{stats.get('attempted')}）")
    tokens = summary.get("tokens") or {}
    lines.append(
        f"token: total={tokens.get('total')} prompt={tokens.get('prompt')} "
        f"completion={tokens.get('completion')}（{tokens.get('runs_with_usage', 0)} 个 run 有用量）"
    )
    tool_durations = summary.get("tool_duration_ms") or {}
    lines.append(
        f"工具耗时: 合计 {tool_durations.get('total')} ms / 单次均值 {tool_durations.get('per_call_mean')} ms"
        f"，工具调用 {summary.get('tool_calls', 0)} 次"
    )
    lines.append(f"LLM 调用 {summary.get('llm_calls', 0)} 次，合计 {summary.get('llm_duration_ms_total')} ms")
    durations = summary.get("run_duration_seconds") or {}
    lines.append(f"单次研究耗时: 均值 {durations.get('mean')} s / 最大 {durations.get('max')} s")
    lines.append(f"路由原因分布: {summary.get('route_reasons')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """``python -m app.graph.metrics <run_log.jsonl>`` → 打印指标报告。"""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("用法: python -m app.graph.metrics <run_log.jsonl>", file=sys.stderr)
        return 2
    result = summarize_events(parse_file(args[0]))
    print(render_report(result["summary"]))
    return 0


if __name__ == "__main__":  # pragma: no cover — 手工入口
    raise SystemExit(main())
