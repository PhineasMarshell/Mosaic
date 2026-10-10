"""Stage 7: concurrent persistence, bounded retries, and SSE task hygiene."""

import asyncio
import json
import logging
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import app.main as main
from app.agent.persistence import persist_research
from app.memory.storage import MarketMemory
from app.models.response import MarketIntelligence, ResearchResponse


def _result(run_id: str, conversation_id: str = "conv") -> ResearchResponse:
    return ResearchResponse(
        question=f"question-{run_id}",
        report=MarketIntelligence(title="t", market_state="stable", state_label="Neutral", confidence="low"),
        conversation_id=conversation_id,
        critique={"verdict": "pass"},
        final_audit_status="pass",
        delivery_status="verified",
        run_id=run_id,
    )


def test_concurrent_verified_persistence_serializes_same_conversation(tmp_path):
    """BEGIN IMMEDIATE makes every verified batch succeed with ordered turns."""
    memory = MarketMemory(db_path=tmp_path / "memory.db")
    results: list[bool] = []

    def worker(index: int) -> None:
        results.append(asyncio.run(persist_research(_result(f"run-{index}"), f"q-{index}", "same", memory=memory)))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 12
    assert all(results)
    assert memory.conn.execute("SELECT COUNT(*) FROM research_records").fetchone()[0] == 12
    indexes = [row[0] for row in memory.conn.execute(
        "SELECT turn_index FROM conversations WHERE conversation_id = ? ORDER BY turn_index", ("same",)
    ).fetchall()]
    assert indexes == list(range(1, 13))
    memory.close()


def test_concurrent_verified_persistence_across_conversations(tmp_path):
    """Different sessions share one SQLite writer safely without cross-talk."""
    memory = MarketMemory(db_path=tmp_path / "memory.db")
    results: list[bool] = []

    def worker(index: int) -> None:
        results.append(
            asyncio.run(
                persist_research(
                    _result(f"different-{index}", conversation_id=f"conv-{index}"),
                    f"q-{index}",
                    f"conv-{index}",
                    memory=memory,
                )
            )
        )

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert results == [True] * 8
    assert memory.conn.execute("SELECT COUNT(*) FROM research_records").fetchone()[0] == 8
    assert memory.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 8
    memory.close()


@pytest.mark.asyncio
async def test_repeated_same_run_is_idempotent(tmp_path):
    memory = MarketMemory(db_path=tmp_path / "memory.db")
    result = _result("same-run")

    persisted = await asyncio.gather(*(
        persist_research(result, result.question, result.conversation_id, memory=memory) for _ in range(5)
    ))

    assert persisted == [True] * 5
    assert memory.conn.execute("SELECT COUNT(*) FROM research_records").fetchone()[0] == 1
    assert memory.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
    memory.close()


@pytest.mark.asyncio
async def test_locked_transaction_retries_after_rollback(tmp_path, caplog):
    memory = MarketMemory(db_path=tmp_path / "memory.db")
    original = memory.transaction
    calls = 0

    @contextmanager
    def flaky_transaction():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise __import__("sqlite3").OperationalError("database is locked")
        with original() as connection:
            yield connection

    memory.transaction = flaky_transaction
    result = _result("retry-run")
    with caplog.at_level(logging.INFO, logger="app.graph.run_log"):
        assert await persist_research(result, result.question, result.conversation_id, memory=memory, retry_backoff_ms=0)

    events = [json.loads(record.message) for record in caplog.records if record.name == "app.graph.run_log"]
    persistence = [event for event in events if event["event"] == "persistence"]
    assert [event["outcome"] for event in persistence] == ["retry", "committed"]
    assert persistence[0]["retryable"] is True
    assert persistence[-1]["run_id"] == "retry-run"
    assert memory.conn.execute("SELECT COUNT(*) FROM research_records").fetchone()[0] == 1
    memory.close()


class _ParallelGraph:
    async def astream(self, input_state, stream_mode=None, config=None):
        await asyncio.sleep(0)
        yield "updates", {"supervisor": {"run_id": input_state["run_id"]}}
        yield "values", {
            **input_state,
            "report": MarketIntelligence(title="t", market_state="stable", state_label="Neutral", confidence="low"),
            "critique": {"verdict": "pass"},
            "errors": [],
        }


@pytest.mark.asyncio
async def test_parallel_sse_runs_keep_unique_correlated_run_ids(monkeypatch, caplog):
    monkeypatch.setattr(
        main,
        "_settings",
        SimpleNamespace(research_budget_seconds=5, stream_heartbeat_seconds=1, graph_recursion_limit=25),
    )
    monkeypatch.setattr(main, "_orchestrator", SimpleNamespace(_ensure_graph=lambda: _ParallelGraph()))
    logger = logging.getLogger("app.graph.run_log")
    monkeypatch.setattr(logger, "propagate", True)

    async def consume(question: str) -> list[tuple[str, dict]]:
        events = []
        async for chunk in main._stream_research(question, None, conversation_id=question):
            lines = chunk.strip().splitlines()
            events.append((lines[0][7:], json.loads(lines[1][6:])))
        return events

    with caplog.at_level(logging.INFO, logger="app.graph.run_log"):
        first, second = await asyncio.gather(consume("one"), consume("two"))

    starts = [json.loads(record.message) for record in caplog.records if record.name == "app.graph.run_log"]
    starts = [event for event in starts if event["event"] == "run_start"]
    deliveries = [event for event in [json.loads(record.message) for record in caplog.records
                                      if record.name == "app.graph.run_log"] if event["event"] == "delivery"]
    assert len({event["run_id"] for event in starts}) == 2
    assert {event["run_id"] for event in deliveries} == {event["run_id"] for event in starts}
    assert all(events[-1][0] == "result" and events[-1][1]["delivery_status"] == "verified"
               for events in (first, second))


class _CancellableGraph:
    def __init__(self):
        self.cleaned = asyncio.Event()

    async def astream(self, *_args, **_kwargs):
        try:
            await asyncio.Event().wait()
            yield "values", {}
        finally:
            self.cleaned.set()


@pytest.mark.asyncio
async def test_sse_disconnect_cleans_graph_task(monkeypatch):
    graph = _CancellableGraph()
    monkeypatch.setattr(
        main,
        "_settings",
        SimpleNamespace(research_budget_seconds=30, stream_heartbeat_seconds=1, graph_recursion_limit=25),
    )
    monkeypatch.setattr(main, "_orchestrator", SimpleNamespace(_ensure_graph=lambda: graph))
    generator = main._stream_research("cancel", None)
    pending = asyncio.create_task(generator.__anext__())
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await generator.aclose()
    assert graph.cleaned.is_set()
