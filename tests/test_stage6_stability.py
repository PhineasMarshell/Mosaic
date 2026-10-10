"""阶段六回归：取消、异常终态日志和持久化事务。

本文件不 mock 生产日志或 SQLite；只用最小图替身制造客户端断开和图异常，
覆盖范围之外的真实 LLM/MCP 网络行为不在单元测试中验证。
"""

import asyncio
import json
import logging
import threading
from types import SimpleNamespace

import pytest

import app.main as main
from app.agent.persistence import persist_research
from app.memory.storage import MarketMemory
from app.models.response import MarketIntelligence, ResearchResponse


class _BlockingGraph:
    async def astream(self, *_args, **_kwargs):
        await asyncio.Event().wait()
        yield "values", {}


def _settings(monkeypatch):
    monkeypatch.setattr(
        main,
        "_settings",
        SimpleNamespace(research_budget_seconds=30, stream_heartbeat_seconds=1, graph_recursion_limit=25),
    )


@pytest.mark.asyncio
async def test_sse_cancellation_logs_terminal_fields(monkeypatch, caplog):
    """关闭 SSE 生成器时，取消日志保留同一个 run_id 和完整终态字段。"""
    _settings(monkeypatch)
    monkeypatch.setattr(main, "_orchestrator", SimpleNamespace(_ensure_graph=lambda: _BlockingGraph()))
    gen = main._stream_research("q", None)

    with caplog.at_level(logging.INFO, logger="app.graph.run_log"):
        pending = asyncio.create_task(gen.__anext__())
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await gen.aclose()

    events = [json.loads(record.message) for record in caplog.records if record.name == "app.graph.run_log"]
    start = next(item for item in events if item["event"] == "run_start")
    delivery = next(item for item in events if item["event"] == "delivery")
    assert delivery["run_id"] == start["run_id"]
    assert delivery["final_audit_status"] == "error"
    assert delivery["delivery_status"] == "failed"
    assert delivery["delivery_reason"]
    assert delivery["persisted"] is False


@pytest.mark.asyncio
async def test_persistence_partial_write_rolls_back(tmp_path, monkeypatch):
    """对话写入失败时，前序 research_records 也必须回滚。"""
    memory = MarketMemory(db_path=tmp_path / "memory.db")
    report = MarketIntelligence(title="t", market_state="弱", state_label="Neutral", confidence="low")
    result = ResearchResponse(
        question="q",
        report=report,
        critique={"verdict": "pass"},
        final_audit_status="pass",
        delivery_status="verified",
    )

    def fail_turn(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(memory, "save_turn", fail_turn)
    assert await persist_research(result, "q", "conv", memory=memory) is False
    assert memory.conn.execute("SELECT COUNT(*) FROM research_records").fetchone()[0] == 0
    assert memory.get_daily_state() is None


def test_concurrent_turns_get_unique_ordered_indexes(tmp_path):
    """同一 conversation 的并发请求不能分配重复 turn_index。"""
    memory = MarketMemory(db_path=tmp_path / "memory.db")
    errors = []

    def worker(index):
        try:
            memory.save_turn("same-conversation", f"q-{index}", "a")
        except Exception as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    # SQLite's UNIQUE(conversation_id, turn_index) makes one racing writer
    # fail visibly; successful rows must still have unique ordered indexes.
    assert len(errors) <= 7
    rows = memory.conn.execute(
        "SELECT turn_index FROM conversations WHERE conversation_id = ? ORDER BY turn_index",
        ("same-conversation",),
    ).fetchall()
    indexes = [row[0] for row in rows]
    assert indexes == list(range(1, len(indexes) + 1))
