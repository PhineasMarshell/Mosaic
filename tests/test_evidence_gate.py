"""Tests for app.agent.evidence_gate module."""

from app.agent.evidence_gate import run_evidence_gate
from app.models.market import ToolResult


def test_has_evidence_true_with_success():
    results = [
        ToolResult(
            tool="sentiment",
            arguments={},
            status="success",
            normalized=[],
        ),
    ]
    gate = run_evidence_gate(results)
    assert gate.has_evidence is True
    assert "sentiment" in gate.successful_tools
    assert gate.partial_tools == []
    assert gate.error_tools == []


def test_has_evidence_false_all_errors():
    results = [
        ToolResult(
            tool="overview",
            arguments={},
            status="error",
            normalized=[],
            error="connection refused",
        ),
    ]
    gate = run_evidence_gate(results)
    assert gate.has_evidence is False
    assert gate.successful_tools == []
    assert gate.error_tools[0]["tool"] == "overview"


def test_partial_detected():
    results = [
        ToolResult(
            tool="klines",
            arguments={},
            status="partial",
            normalized=[],
            partial=True,
        ),
    ]
    gate = run_evidence_gate(results)
    # partial 也计入 has_evidence
    assert gate.has_evidence is True
    assert "klines" in gate.partial_tools


def test_mixed_status():
    results = [
        ToolResult(tool="overview", arguments={}, status="success", normalized=[]),
        ToolResult(tool="missing", arguments={}, status="error", normalized=[], error="timeout"),
        ToolResult(tool="partial_data", arguments={}, status="partial", normalized=[], partial=True),
    ]
    gate = run_evidence_gate(results)
    assert gate.has_evidence is True
    assert len(gate.successful_tools) == 1
    assert len(gate.partial_tools) == 1
    assert len(gate.error_tools) == 1
    assert gate.reason  # reason should be non-empty


def test_empty_results():
    gate = run_evidence_gate([])
    assert gate.has_evidence is False
    assert gate.reason != ""


def test_reason_includes_error_info():
    results = [
        ToolResult(
            tool="bad_tool",
            arguments={},
            status="error",
            normalized=[],
            error="upstream timeout",
        ),
        ToolResult(
            tool="another_bad",
            arguments={},
            status="error",
            normalized=[],
            error="404 not found",
        ),
    ]
    gate = run_evidence_gate(results)
    assert "bad_tool" in gate.reason or "another_bad" in gate.reason
