"""阶段三：研究回环、终态门控和日志回放回归。"""

import json
import logging
import time

from app.config import Settings
from app.graph import run_log
from app.graph.builder import critic_route_decision
from app.graph.gap_loop import classify_missing_tool_keys
from app.graph.nodes.finalize import resolve_delivery
from app.models.response import build_response_from_state


def test_all_discarded_gap_keys_keep_structured_reasons():
    decision = classify_missing_tool_keys(
        ["invisible", "invisible", "satisfied", "a", "b"],
        allowed={"satisfied", "a", "b"},
        satisfied={"satisfied"},
        max_keys=1,
    )
    assert decision.as_state() == [
        {"key": "invisible", "decision": "dropped", "reason": "not_visible"},
        {"key": "invisible", "decision": "dropped", "reason": "duplicated"},
        {"key": "satisfied", "decision": "dropped", "reason": "already_satisfied"},
        {"key": "a", "decision": "kept", "reason": "executable"},
        {"key": "b", "decision": "dropped", "reason": "budget_truncated"},
    ]


def test_research_more_budget_checks_time_and_tool_calls():
    settings = Settings(max_tool_calls=1, research_round_min_remaining_seconds=60)
    state = {
        "run_id": "stage3-budget",
        "critique": {"verdict": "research_more", "missing_tool_keys": ["x"]},
        "report": {"what_happened": "existing"},
        "results": [{"tool": "x"}],
        "budget_deadline": time.monotonic() + 120,
    }
    assert critic_route_decision(state, settings) == "finalize_audit"


def test_research_more_without_executable_gap_goes_to_finalize():
    settings = Settings()
    state = {
        "run_id": "stage3-gap",
        "critique": {
            "verdict": "research_more",
            "missing_tool_keys": ["not_visible"],
            "gap_key_decisions": [
                {"key": "not_visible", "decision": "dropped", "reason": "not_visible"}
            ],
        },
        "report": {"what_happened": "existing"},
        "budget_deadline": time.monotonic() + 300,
    }
    assert critic_route_decision(state, settings) == "finalize_audit"


def test_delivery_reason_contains_unresolved_issue_and_gap_reason():
    state = {
        "question": "q",
        "report": {"what_happened": "unsafe"},
        "critique": {
            "verdict": "research_more",
            "reason": "证据不足",
            "issues": [{"kind": "missing_evidence", "claim": "缺少成交额", "action": "research_more"}],
            "gap_key_decisions": [{"key": "x", "decision": "dropped", "reason": "budget_truncated"}],
        },
        "final_audit_status": "research_exhausted",
        "delivery_status": "blocked",
    }
    response = build_response_from_state(state, "q")
    assert response.report is None
    assert "缺少成交额" in response.delivery_reason
    assert "budget_truncated" in response.delivery_reason
    assert response.unresolved_issues


def test_run_log_chain_preserves_run_id_and_terminal_fields(caplog):
    state = {"run_id": "stage3-chain", "research_round_count": 1, "rewrite_count": 0}
    with caplog.at_level(logging.INFO, logger="app.graph.run_log"):
        run_log.log_run_start(state)
        run_log.log_route(
            state,
            "finalize_audit",
            reason="no_executable_gap_steps",
            budget_remaining=12.0,
            required_minimum=60.0,
            gap_key_decisions=[{"key": "x", "decision": "dropped", "reason": "not_visible"}],
            executable_gap_steps=[],
            blocked_reason="no_executable_gap_steps",
        )
        run_log.log_finalize(
            {**state, "final_audit_status": "research_exhausted", "delivery_reason": "x"},
            final_audit_status="research_exhausted",
            delivery_status="blocked",
            reason="x",
            blocked_reason="no_executable_gap_steps",
        )
        run_log.log_delivery(
            {**state, "final_audit_status": "research_exhausted", "delivery_reason": "x"},
            delivery_status="blocked",
            persisted=False,
            sink="sync",
        )
    events = [json.loads(record.getMessage()) for record in caplog.records]
    assert {event["event"] for event in events} == {"run_start", "route", "finalize", "delivery"}
    assert {event["run_id"] for event in events} == {"stage3-chain"}
    assert events[-1]["delivery_status"] == "blocked"
    assert events[-1]["persisted"] is False
