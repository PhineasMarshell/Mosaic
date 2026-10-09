"""阶段 6④ 指标聚合（`app/graph/metrics.py`）的回归测试。

**mock 范围**：本文件是纯函数级测试，直接喂人造的运行日志事件（dict），
不启动图、不调用 LLM、不访问网络，因此**不覆盖**真实运行日志的产生过程
（那由 test_audit_run_log / test_research_rounds 覆盖），也不覆盖灰度决策本身
（那是一次人工评审，代码只负责把口径算对）。
"""

from __future__ import annotations

import json

from app.graph import metrics

# ------------------------------------------------------------------ #
# 人造事件（字段与 run_log 的 payload 一致）                             #
# ------------------------------------------------------------------ #


def _event(name: str, ts: float, run_id: str = "r1", **fields) -> dict:
    return {"ts": ts, "event": name, "run_id": run_id, **fields}


def _blocked_run() -> list[dict]:
    """一次"补了缺口仍不通过、研究额度耗尽"的运行。"""
    return [
        _event("run_start", 100.0, question="今天 A 股发生了什么？"),
        _event("llm", 100.5, node="supervisor", phase="initial", total_tokens=1000, prompt_tokens=800,
               completion_tokens=200, duration_ms=500.0),
        _event("plan", 100.6, steps=[{"tool_key": "sentiment"}], forced_gap_keys=["limit_up_pool"]),
        _event("executions", 101.0, category="technical", results=[
            {"tool_key": "limit_up_pool", "status": "partial", "partial": True, "duration_ms": 300.0},
            {"tool_key": "sentiment", "status": "success", "partial": False, "duration_ms": 100.0},
        ]),
        _event("critic", 102.0, verdict="research_more", missing_tool_keys=["limit_up_pool"],
               gap_key_decisions=[{"key": "limit_up_pool", "reason": "still_partial"}]),
        _event("route", 102.1, verdict="research_more", decision="finalize_audit",
               reason="research_budget_exhausted", rewrite_count=1, research_round_count=1),
        _event("llm", 102.5, node="critic", total_tokens=500, prompt_tokens=400, completion_tokens=100,
               duration_ms=250.0),
        _event("finalize", 103.0, final_audit_status="research_exhausted", delivery_status="blocked",
               reason="research_more 轮次耗尽", unresolved_issues=["缺个股明细"]),
        _event("delivery", 103.2, delivery_status="blocked", persisted=False, sink="sync", error_count=0),
    ]


def _pass_run() -> list[dict]:
    return [
        _event("run_start", 200.0, run_id="r2"),
        _event("llm", 200.4, run_id="r2", node="reasoning", total_tokens=200, duration_ms=120.0),
        _event("critic", 201.0, run_id="r2", verdict="pass", missing_tool_keys=[]),
        _event("route", 201.1, run_id="r2", verdict="pass", decision="end", reason="audit_passed",
               rewrite_count=0, research_round_count=0),
        _event("finalize", 201.2, run_id="r2", final_audit_status="pass", delivery_status="verified"),
        _event("delivery", 201.3, run_id="r2", delivery_status="verified", persisted=True, sink="sse",
               error_count=0),
    ]


# ------------------------------------------------------------------ #
# 解析                                                                 #
# ------------------------------------------------------------------ #


def test_parse_lines_skips_broken_and_non_object_lines():
    """混进非 JSON 行 / 非对象行时跳过，而不是整份日志读不出来。"""
    lines = [
        json.dumps({"event": "run_start"}),
        "",
        "not json at all",
        "[1, 2, 3]",
        json.dumps({"event": "critic", "verdict": "pass"}),
    ]
    events = metrics.parse_lines(lines)
    assert [e.get("event") for e in events] == ["run_start", "critic"]


def test_group_by_run_keeps_order_and_marks_missing_run_id():
    events = [_event("run_start", 1.0), _event("run_start", 2.0, run_id=None), _event("critic", 3.0)]
    grouped = metrics.group_by_run(events)
    assert {k: len(v) for k, v in grouped.items()} == {"r1": 2, "__unassigned__": 1}


# ------------------------------------------------------------------ #
# 单 run 汇总                                                          #
# ------------------------------------------------------------------ #


def test_summarize_run_blocked_case():
    run = metrics.summarize_run(_blocked_run(), run_id="r1")
    assert run["verdict"] == "research_more"
    assert run["final_audit_status"] == "research_exhausted"
    assert run["delivery_status"] == "blocked"
    assert (run["rewrite_count"], run["research_round_count"]) == (1, 1)
    assert run["route_reasons"] == ["research_budget_exhausted"]
    assert run["tool_calls"] == 2
    assert run["tool_duration_ms"] == 400.0
    assert run["total_tokens"] == 1500
    assert run["llm_calls"] == 2
    assert run["llm_duration_ms"] == 750.0
    assert run["duration_seconds"] == 3.2
    assert run["error_count"] == 0
    # 被代码级补进计划的 key 在最后一次裁决里仍然算缺口 → 没补齐
    assert run["gap_attempted_keys"] == ["limit_up_pool"]
    assert run["gap_backfilled_keys"] == []
    assert run["gap_unresolved_keys"] == ["limit_up_pool"]


def test_gap_backfill_needs_the_last_critic_to_stop_calling_it_missing():
    """"补齐成功"= 补进计划的 key 在**最后一次**裁决里不再算缺口。

    只看"计划里有它"会把"计划了但工具没跑出东西"也算成补齐；
    只看第一次裁决则会把后面几轮新暴露的缺口漏掉。
    """
    backfilled = [
        _event("plan", 10.0, forced_gap_keys=["overview"]),
        _event("critic", 11.0, verdict="research_more", missing_tool_keys=[]),
        _event("critic", 12.0, verdict="pass", missing_tool_keys=[],
               gap_key_decisions=[{"key": "overview", "reason": "already_satisfied"}]),
    ]
    run = metrics.summarize_run(backfilled, run_id="r1")
    assert run["gap_backfilled_keys"] == ["overview"]
    assert run["gap_unresolved_keys"] == []

    # 中间那轮把缺口列为 not_visible，最后一轮才补上 → 仍然算补齐
    late = backfilled[:1] + [
        _event("critic", 11.0, verdict="research_more", missing_tool_keys=["overview"],
               gap_key_decisions=[{"key": "overview", "reason": "not_visible"}]),
        backfilled[2],
    ]
    assert metrics.summarize_run(late, run_id="r1")["gap_backfilled_keys"] == ["overview"]

    # 最后一次裁决又把它列回缺口 → 不算补齐
    still = backfilled[:1] + [
        _event("critic", 12.0, verdict="research_more", missing_tool_keys=["overview"])
    ]
    run2 = metrics.summarize_run(still, run_id="r1")
    assert run2["gap_backfilled_keys"] == []
    assert run2["gap_unresolved_keys"] == ["overview"]


# ------------------------------------------------------------------ #
# 聚合：三态（有数据 / 0 / 无样本）                                       #
# ------------------------------------------------------------------ #


def test_aggregate_rates_and_distributions():
    summary = metrics.summarize_events(_blocked_run() + _pass_run())["summary"]
    assert summary["runs"] == 2
    assert summary["audit_pass_rate"] == 0.5
    assert summary["verdict_distribution"] == {"research_more": 1, "pass": 1}
    assert summary["exhaustion_rate"] == 0.5
    assert summary["degraded_or_blocked_rate"] == 0.5
    assert summary["failed_rate"] == 0.0
    assert summary["tokens"]["total"] == 1700
    assert summary["tokens"]["runs_with_usage"] == 2
    assert summary["tool_calls"] == 2
    assert summary["tool_duration_ms"]["per_call_mean"] == 200.0
    assert summary["route_reasons"] == {"research_budget_exhausted": 1, "audit_passed": 1}


def test_rates_are_none_without_samples():
    """没有样本时比例是 None（"没采到"），不是 0.0（"通过率 0%"）。"""
    summary = metrics.summarize_events([])["summary"]
    assert summary["runs"] == 0
    assert summary["audit_pass_rate"] is None
    assert summary["exhaustion_rate"] is None
    assert summary["degraded_or_blocked_rate"] is None
    assert summary["gap_backfill"]["rate"] is None
    assert summary["tokens"]["total"] is None
    assert summary["tool_duration_ms"]["per_call_mean"] is None


def test_gap_backfill_rate_is_per_key():
    events = []
    for index, run_id in enumerate(("r1", "r2")):
        events += [
            _event("plan", 10.0 + index, run_id=run_id, forced_gap_keys=["limit_up_pool", "overview"]),
            _event(
                "critic",
                11.0 + index,
                run_id=run_id,
                verdict="pass",
                # r1 两个都补齐；r2 只补齐 overview
                missing_tool_keys=[] if index == 0 else ["limit_up_pool"],
            ),
        ]
    summary = metrics.summarize_events(events)["summary"]
    by_key = summary["gap_backfill"]["by_key"]
    assert by_key["limit_up_pool"] == {"attempted": 2, "resolved": 1, "rate": 0.5}
    assert by_key["overview"] == {"attempted": 2, "resolved": 2, "rate": 1.0}
    assert summary["gap_backfill"]["rate"] == 0.75


def test_unassigned_events_are_counted_not_attributed():
    """没有 run_id 的事件计入 unassigned，不污染任何一次运行的指标。"""
    events = _pass_run() + [_event("critic", 300.0, run_id=None, verdict="error")]
    summary = metrics.summarize_events(events)["summary"]
    assert summary["runs"] == 1
    assert summary["unassigned_events"] == 1
    assert summary["audit_pass_rate"] == 1.0


# ------------------------------------------------------------------ #
# 渲染与入口                                                           #
# ------------------------------------------------------------------ #


def test_render_report_marks_missing_samples_and_lists_keys():
    summary = metrics.summarize_events(_blocked_run() + _pass_run())["summary"]
    text = metrics.render_report(summary)
    assert "运行数: 2" in text
    assert "审计通过率: 50.0%" in text
    assert "耗尽率: 50.0%" in text
    assert "limit_up_pool" in text

    empty = metrics.render_report(metrics.summarize_events([])["summary"])
    assert "无样本" in empty, "没采到样本必须显式写出来，而不是显示 0.0%"


def test_main_reads_jsonl_file(tmp_path, capsys):
    path = tmp_path / "run_log.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in _pass_run()), encoding="utf-8")
    assert metrics.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "运行数: 1" in out
    assert "审计通过率: 100.0%" in out


def test_main_without_argument_explains_usage(capsys):
    assert metrics.main([]) == 2
    assert "run_log.jsonl" in capsys.readouterr().err
