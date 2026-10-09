"""Stage 4 integration regressions.

These tests deliberately use a repository controlled SQLite directory.  The
Windows runner used for this project can deny pytest's default global
``tmp_path`` root, which would otherwise hide the behavior under test.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.agent.persistence import persist_research
from app.memory.storage import MarketMemory
from app.models.response import MarketIntelligence, ResearchResponse, build_response_from_state


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROLLED_TMP = REPO_ROOT / ".tmp" / "stage4-integration"


def _report() -> MarketIntelligence:
    return MarketIntelligence(
        title="今日市场情报",
        market_state="震荡偏强",
        state_label="Neutral",
        what_happened="指数震荡，成交额保持稳定",
        confidence="medium",
    )


def _memory() -> tuple[MarketMemory, Path]:
    CONTROLLED_TMP.mkdir(parents=True, exist_ok=True)
    path = CONTROLLED_TMP / f"memory-{uuid.uuid4().hex}.db"
    return MarketMemory(db_path=path), path


class _ValuesGraph:
    def __init__(self, state: dict):
        self.state = state

    async def astream(self, input_state, stream_mode=None, config=None):
        yield "values", self.state


def _events(response) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    event_name = None
    for line in response.iter_lines():
        if line.startswith("event: "):
            event_name = line[7:].strip()
        elif line.startswith("data: ") and event_name:
            events.append((event_name, json.loads(line[6:])))
            event_name = None
    return events


@pytest.fixture(autouse=True)
def _reset_main(monkeypatch):
    monkeypatch.setattr(main, "_orchestrator", None, raising=False)
    monkeypatch.setattr(main, "_settings", None, raising=False)
    # setup_logging installs a non-propagating handler; allow caplog to see
    # the structured run_log records for this integration assertion.
    run_logger = logging.getLogger("app.graph.run_log")
    monkeypatch.setattr(run_logger, "propagate", True)


def test_api_sse_model_and_delivery_log_share_terminal_fields(monkeypatch, caplog):
    """A market-level pass has identical terminal fields on every boundary."""
    mem, db_path = _memory()
    report = _report()
    final_state = {
        "question": "今天A股发生了什么？",
        "domain": "a_share",
        "run_id": "sse-stage4",
        "report": report,
        "results": [],
        "critique": {"verdict": "pass", "reason": "证据充分"},
        "errors": [],
        # Deliberately omit these two fields: pass is derived by the response
        # builder, which used to make the SSE delivery log record null here.
    }

    async def save(result, question, conversation_id, **kwargs):
        return await persist_research(result, question, conversation_id, memory=mem)

    monkeypatch.setattr(main, "persist_research", save)
    monkeypatch.setattr(
        main,
        "_orchestrator",
        SimpleNamespace(_ensure_graph=lambda: _ValuesGraph(final_state)),
        raising=False,
    )
    monkeypatch.setattr(
        main,
        "_settings",
        SimpleNamespace(research_budget_seconds=30, stream_heartbeat_seconds=5, graph_recursion_limit=25),
        raising=False,
    )

    with caplog.at_level(logging.INFO, logger="app.graph.run_log"):
        with TestClient(main.app).stream(
            "POST",
            "/api/ask/stream",
            json={"question": final_state["question"], "conversation_id": "stage4-conv"},
        ) as response:
            assert response.status_code == 200
            sse_events = _events(response)

        # Exercise the synchronous API against the same resolved state shape.
        api_result = ResearchResponse(
            question=final_state["question"],
            report=report,
            conversation_id="stage4-conv",
            critique=final_state["critique"],
            final_audit_status="pass",
            delivery_status="verified",
            delivery_reason="证据充分",
            run_id="api-stage4",
        )

        async def run(_question, domain=None, conversation_id=None):
            return api_result

        monkeypatch.setattr(main, "_orchestrator", SimpleNamespace(run=run), raising=False)
        api_response = TestClient(main.app).post(
            "/api/ask",
            json={"question": final_state["question"], "conversation_id": "stage4-conv"},
        )

    assert api_response.status_code == 200, api_response.text
    sse_result = [data for name, data in sse_events if name == "result"][-1]
    api_body = api_response.json()
    terminal_fields = ("final_audit_status", "delivery_status", "delivery_reason", "unresolved_issues", "persisted")
    assert {field: sse_result.get(field) for field in terminal_fields} == {
        field: api_body.get(field) for field in terminal_fields
    }
    assert (sse_result["final_audit_status"], sse_result["delivery_status"]) == ("pass", "verified")
    assert sse_result["persisted"] is True

    model = build_response_from_state(final_state, question=final_state["question"])
    resolved_fields = terminal_fields[:-1]
    assert {field: getattr(model, field) for field in resolved_fields} == {
        field: sse_result.get(field) for field in resolved_fields
    }
    assert model.persisted is None, "响应组装器在持久化执行前不能猜测 persisted"

    delivery_logs = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.getMessage().startswith('{"ts"') and '"event": "delivery"' in record.getMessage()
    ]
    assert len(delivery_logs) == 2
    assert all(log["final_audit_status"] == "pass" for log in delivery_logs)
    assert all(log["delivery_status"] == "verified" for log in delivery_logs)
    assert all(log["persisted"] is True for log in delivery_logs)
    assert mem.get_daily_state()["state_label"] == report.state_label
    assert "今天A股发生了什么？" in mem.get_conversation_history("stage4-conv")
    mem.close()
    assert db_path.exists()  # Keep the controlled artifact inspectable on failure.


@pytest.mark.parametrize(
    ("delivery_status", "final_audit_status", "verdict", "should_persist"),
    [
        ("verified", "pass", "pass", True),
        ("blocked", "revise_exhausted", "revise", False),
        ("degraded", "research_exhausted", "research_more", False),
        ("failed", "error", "error", False),
    ],
)
@pytest.mark.asyncio
async def test_persistence_gate_matrix_uses_controlled_memory(
    delivery_status, final_audit_status, verdict, should_persist
):
    """Only the complete verified/pass/pass tuple reaches all memory sinks."""
    mem, db_path = _memory()
    question = f"stage4-{delivery_status}"
    result = ResearchResponse(
        question=question,
        report=_report(),
        conversation_id="stage4-gate",
        critique={"verdict": verdict, "reason": "test"},
        final_audit_status=final_audit_status,
        delivery_status=delivery_status,
    )

    persisted = await persist_research(result, question, "stage4-gate", memory=mem)
    assert persisted is should_persist
    records = mem.conn.execute("SELECT COUNT(*) FROM research_records").fetchone()[0]
    turns = mem.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    if should_persist:
        assert records == 1
        assert turns == 1
        assert mem.get_daily_state()["state_label"] == "Neutral"
    else:
        assert records == 0
        assert turns == 0
        assert mem.get_daily_state() is None
        assert mem.get_conversation_history("stage4-gate") == ""
    mem.close()
    assert db_path.exists()


def test_terminal_state_matrix_handles_degraded_no_report_and_critic_failure():
    """All terminal states stay explicit at the shared response boundary."""
    degraded = build_response_from_state(
        {
            "report": _report(),
            "critique": {"verdict": "revise", "reason": "仅保留已证实事实"},
            "final_audit_status": "revise_exhausted",
            "delivery_status": "degraded",
            "unresolved_issues": ["missing_evidence | 量能 | action=research_more"],
        },
        question="q",
    )
    assert degraded.delivery_status == "degraded"
    assert degraded.final_audit_status == "revise_exhausted"
    assert degraded.report is not None
    assert degraded.unresolved_issues
    assert degraded.persisted is None

    no_report = build_response_from_state(
        {"report": None, "critique": {"verdict": "pass", "reason": "ok"}, "errors": []},
        question="q",
    )
    assert no_report.delivery_status == "failed"
    assert no_report.final_audit_status == "error"
    assert no_report.report is None
    assert "reasoning produced no report" in no_report.errors

    critic_failed = build_response_from_state(
        {
            "report": _report(),
            "critique": {"verdict": "error", "reason": "Critic audit failed"},
            "errors": ["Critic audit failed"],
        },
        question="q",
    )
    assert critic_failed.delivery_status == "failed"
    assert critic_failed.final_audit_status == "error"
    assert critic_failed.report is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_sink", ["save_research", "save_turn", "save_daily_state"])
async def test_persistence_partial_failure_reports_false(failed_sink, monkeypatch):
    """A failed or partial memory write must never report persisted=True."""
    mem, db_path = _memory()
    result = ResearchResponse(
        question="stage4-persist-failure",
        report=_report(),
        conversation_id="stage4-persist-failure",
        critique={"verdict": "pass", "reason": "证据充分"},
        final_audit_status="pass",
        delivery_status="verified",
    )

    def fail(*args, **kwargs):
        raise OSError(failed_sink)

    monkeypatch.setattr(mem, failed_sink, fail)
    persisted = await persist_research(result, result.question, result.conversation_id, memory=mem)

    assert persisted is False
    assert result.persisted is False
    mem.close()
    assert db_path.exists()


def test_sync_api_degraded_and_failed_paths_expose_terminal_fields(monkeypatch):
    degraded = ResearchResponse(
        question="q",
        report=_report(),
        critique={"verdict": "revise", "reason": "只交付已证实事实"},
        final_audit_status="revise_exhausted",
        delivery_status="degraded",
        delivery_reason="部分证据缺口未解决",
        unresolved_issues=["missing_evidence | 量能 | action=research_more"],
    )

    async def run_degraded(_question, domain=None, conversation_id=None):
        return degraded

    async def persist_false(*args, **kwargs):
        return False

    monkeypatch.setattr(main, "_orchestrator", SimpleNamespace(run=run_degraded), raising=False)
    monkeypatch.setattr(main, "_settings", SimpleNamespace(research_budget_seconds=30), raising=False)
    monkeypatch.setattr(main, "persist_research", persist_false)
    body = TestClient(main.app).post("/api/ask", json={"question": "q"})

    assert body.status_code == 200
    payload = body.json()
    assert payload["report"] is not None
    assert payload["final_audit_status"] == "revise_exhausted"
    assert payload["delivery_status"] == "degraded"
    assert payload["delivery_reason"] == degraded.delivery_reason
    assert payload["unresolved_issues"] == degraded.unresolved_issues
    assert payload["persisted"] is False

    failed = ResearchResponse(
        question="q",
        report=None,
        critique={"verdict": "error", "reason": "Critic audit failed"},
        errors=["Critic audit failed"],
        final_audit_status="error",
        delivery_status="failed",
        delivery_reason="Critic 审计失败",
    )

    async def run_failed(_question, domain=None, conversation_id=None):
        return failed

    monkeypatch.setattr(main, "_orchestrator", SimpleNamespace(run=run_failed), raising=False)
    response = TestClient(main.app).post("/api/ask", json={"question": "q"})
    assert response.status_code == 502
    payload = response.json()
    assert payload["code"] == "failed"
    assert payload["delivery_status"] == "failed"
    assert payload["final_audit_status"] == "error"
    assert payload["persisted"] is False


def test_sync_timeout_exposes_failed_terminal_fields(monkeypatch):
    async def slow_run(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(main, "_orchestrator", SimpleNamespace(run=slow_run), raising=False)
    monkeypatch.setattr(main, "_settings", SimpleNamespace(research_budget_seconds=0.01), raising=False)
    response = TestClient(main.app).post("/api/ask", json={"question": "q"})

    assert response.status_code == 504
    payload = response.json()
    assert payload["code"] == "timeout"
    assert payload["delivery_status"] == "failed"
    assert payload["final_audit_status"] == "error"
    assert payload["persisted"] is False
