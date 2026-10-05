"""Tests for app.agent.evidence_gate module."""

import pytest

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


# ── T15/D2：Reasoning 消费 gate 结论 —— 降级不短路 ──────────────


def _reasoning_node_with_stubbed_llm(report_confidence="high"):
    """构造 ReasoningNode，其 LLM stub 无视零证据、返回高置信度报告。"""
    import json
    from types import SimpleNamespace

    from app.config import Settings
    from app.graph.nodes.reasoning import ReasoningNode

    report_json = json.dumps(
        {
            "title": "t",
            "market_state": "s",
            "state_label": "sl",
            "what_happened": "w",
            "why": [],
            "strong_areas": [],
            "what_changed": [],
            "what_matters": [],
            "risks": [],
            "data_caveats": [],
            "confidence": report_confidence,  # 模型自以为很有把握
            "used_tools": [],
            "evidence": [],
        }
    )

    async def create(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=report_json), finish_reason="stop")]
        )

    node = ReasoningNode(Settings())
    node._engine.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return node


@pytest.mark.asyncio
async def test_zero_evidence_degrades_report_but_still_produces_it():
    """has_evidence=False → 报告仍产出（不短路），但 confidence 强制 low + caveats + errors。

    LLM stub 返回 confidence="high"：模型自以为的置信度必须被 gate 覆盖。
    旧实现 has_evidence 无消费方 → 该用例红（confidence 仍是 high、errors 为空）。
    """
    from app.agent.evidence_gate import EvidenceGateResult

    node = _reasoning_node_with_stubbed_llm()
    gate = EvidenceGateResult(has_evidence=False, reason="no success/partial tool results")
    state = {"question": "q", "results": [], "evidence": [], "findings": [], "gate": gate}

    out = await node(state)

    assert out["report"] is not None, "降级不短路：零证据也必须产出报告"
    assert out["report"].confidence == "low"
    assert any("证据链为空" in c for c in out["report"].data_caveats)
    assert any("Evidence gate" in e for e in out.get("errors", []))


@pytest.mark.asyncio
async def test_zero_evidence_dict_gate_degrades_report():
    """gate 经 reducer/序列化变成 dict 时同样要被消费。"""
    node = _reasoning_node_with_stubbed_llm()
    state = {
        "question": "q",
        "results": [],
        "evidence": [],
        "findings": [],
        "gate": {"has_evidence": False, "reason": "none"},
    }
    out = await node(state)
    assert out["report"] is not None
    assert out["report"].confidence == "low"
    assert out.get("errors")


@pytest.mark.asyncio
async def test_with_evidence_report_keeps_model_confidence():
    """has_evidence=True（或 gate 缺失）→ 不降级，模型置信度保留。"""
    node = _reasoning_node_with_stubbed_llm(report_confidence="high")
    state = {
        "question": "q",
        "results": [],
        "evidence": [],
        "findings": [],
        "gate": {"has_evidence": True, "reason": "ok"},
    }
    out = await node(state)
    assert out["report"].confidence == "high"
    assert not out.get("errors")
