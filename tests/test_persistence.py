"""T3 回归：研究结果持久化必须从 ``result.report`` 读字段。

旧 SSE 逻辑把 ``ResearchResponse.model_dump()`` 当成扁平 report dict 读取，
但字段嵌套在 ``report`` 键下，导致对话摘要恒空、当日状态被空值覆盖、漏写
market_state。这里用真实的 ResearchResponse + 临时 SQLite 库验证统一持久化。
"""

import logging
import sqlite3

import pytest

from app.agent.persistence import _answer_summary, persist_research
from app.memory.storage import MarketMemory
from app.models.response import MarketIntelligence, ResearchResponse

CONV_ID = "conv-1"


def _make_report() -> MarketIntelligence:
    return MarketIntelligence.model_validate(
        {
            "title": "t",
            "market_state": "弱",
            "state_label": "Neutral",
            "what_happened": "指数普跌，成交额萎缩",
            "strong_areas": ["AI", "算力"],
            "confidence": "low",
        }
    )


def _make_good_response() -> ResearchResponse:
    return ResearchResponse(question="今天市场怎么样", report=_make_report(), conversation_id=CONV_ID)


@pytest.fixture
def mem(tmp_path) -> MarketMemory:
    return MarketMemory(db_path=tmp_path / "m.db")


async def test_persistence_uses_report_fields(mem):
    result = _make_good_response()
    await persist_research(result, result.question, CONV_ID, memory=mem)

    state = mem.get_daily_state()
    assert state is not None
    assert state["state_label"] == "Neutral"
    assert state["market_state"] == "弱"
    assert state["strong_areas"] == ["AI", "算力"]
    assert state["confidence"] == "low"

    history = mem.get_conversation_history(CONV_ID)
    assert "Neutral" in history
    assert "指数普跌" in history
    assert "今天市场怎么样" in history


async def test_none_report_does_not_clear_daily_state(mem):
    # 先写一份正常快照
    good = _make_good_response()
    await persist_research(good, good.question, CONV_ID, memory=mem)
    before = mem.get_daily_state()
    assert before["state_label"] == "Neutral"

    # 再用 report=None 的结果持久化
    bad = ResearchResponse(question="第二次问题", report=None, errors=["reasoning failed"])
    await persist_research(bad, "第二次问题", CONV_ID, memory=mem)

    after = mem.get_daily_state()
    assert after["state_label"] == "Neutral"
    assert after["market_state"] == "弱"
    assert after["strong_areas"] == ["AI", "算力"]

    # report=None 不写对话轮次：第二次问题不进入历史
    history = mem.get_conversation_history(CONV_ID, last_n=10)
    assert "第二次问题" not in history
    assert "今天市场怎么样" in history

    # 但研究记录仍保存（便于排查）：两次都落库
    count = mem.conn.execute("SELECT COUNT(*) FROM research_records").fetchone()[0]
    assert count == 2


def test_answer_summary_format():
    summary = _answer_summary(_make_report())
    assert summary == "Neutral：指数普跌，成交额萎缩…"


async def test_save_failure_logs_warning(mem, caplog, monkeypatch):
    """T21 防回归：落库失败必须 WARNING 可见，不许静默降回 debug（旧实现即如此）。"""

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(mem, "save_research", boom)
    result = _make_good_response()
    with caplog.at_level(logging.WARNING, logger="app.agent.persistence"):
        await persist_research(result, result.question, CONV_ID, memory=mem)

    assert "Research save failed (non-fatal)" in caplog.text
