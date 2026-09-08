"""Tests for Market Memory (SQLite) persistence layer.

Coverage:
- Save + retrieve daily states (upsert)
- Recent states query with limit and domain filter
- Anomaly recording and querying
- Research record saving
- Conversation history (append-store, turn ordering)
- Context generation for reasoning prompts
"""

import json
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from app.memory.storage import MarketMemory


@pytest.fixture
def memory():
    """Create an in-memory-like temp DB for each test."""
    tmp = Path(tempfile.mkdtemp()) / "memory.db"
    mem = MarketMemory(db_path=tmp)
    yield mem
    # Cleanup
    mem.conn.close()
    try:
        tmp.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            p = tmp.with_suffix(tmp.suffix + suffix)
            p.unlink(missing_ok=True)
    except OSError:
        pass


# ── Daily State Tests ──────────────────────────────────────────────────────


class TestDailyState:
    def test_save_and_retrieve(self, memory):
        memory.save_daily_state(date="2025-09-01", data={
            "state_label": "Risk-On",
            "strong_areas": ["Robotics"],
        })
        state = memory.get_daily_state("2025-09-01")
        assert state is not None
        assert state["state_label"] == "Risk-On"
        assert "Robotics" in state["strong_areas"]

    def test_missing_date_returns_none(self, memory):
        assert memory.get_daily_state("2024-01-01") is None

    def test_upsert_updates_existing(self, memory):
        memory.save_daily_state(date="2025-09-01", data={"key1": "val1"})
        memory.save_daily_state(date="2025-09-01", data={"key2": "val2"})
        state = memory.get_daily_state("2025-09-01")
        assert state["key1"] == "val1"
        assert state["key2"] == "val2"
        # Should still be exactly one row
        rows = list(memory.conn.execute("SELECT COUNT(*) FROM daily_states").fetchone())
        assert rows[0] == 1

    def test_state_has_metadata(self, memory):
        memory.save_daily_state(date="2025-09-01", data={"state_label": "test"})
        state = memory.get_daily_state("2025-09-01")
        assert "_saved_at" in state
        assert state["_version"] == "0.1"


# ── Recent States Tests ────────────────────────────────────────────────────


class TestRecentStates:
    def test_get_recent_states(self, memory):
        dates = ["2025-09-03", "2025-09-04", "2025-09-05"]
        labels = ["Risk-On", "Mixed", "Risk-Off"]
        for d, l in zip(dates, labels):
            memory.save_daily_state(date=d, data={"state_label": l})

        recent = memory.get_recent_states(days=7)
        assert len(recent) >= 3
        # Most recent first
        assert recent[0]["_file_date"] == "2025-09-05"

    def test_limited_by_days(self, memory):
        for i in range(10):
            day = f"2025-08-{i+1:02d}"
            memory.save_daily_state(date=day, data={"state_label": f"Day{i}"})

        recent = memory.get_recent_states(days=3)
        assert len(recent) <= 3

    def test_domain_filter(self, memory):
        memory.save_daily_state(date="2025-09-01", data={
            "a-share": {"state_label": "Cooling"},
            "state_label": "Mixed",
        })
        memory.save_daily_state(date="2025-09-02", data={
            "state_label": "Risk-On",
        })

        filtered = memory.get_recent_states(days=7, domain="a_share")
        assert all("_file_date" in r and "a-share" in r for r in filtered)
        # Should only return dates that have the a-share key
        assert len(filtered) >= 1

    def test_empty_returns_empty_list(self, memory):
        assert memory.get_recent_states(days=7) == []


# ── Anomaly Recording Tests ────────────────────────────────────────────────


class TestAnomalyRecording:
    def test_record_and_query_anomalies(self, memory):
        anomaly = {
            "type": "oi_spike",
            "severity": "high",
            "description": "OI +8.2%",
            "domain": "crypto",
        }
        record_id = memory.record_anomaly(anomaly, date="2025-09-05")
        anomalies = memory.get_anomalies()
        assert len(anomalies) >= 1
        assert anomalies[0]["type"] == "oi_spike"
        assert anomalies[0]["_source_date"] == "2025-09-05"

    def test_limit_parameter(self, memory):
        for i in range(5):
            rec = {"type": f"test_{i}", "value": i}
            memory.record_anomaly(rec, date="2025-09-05")

        limited = memory.get_anomalies(limit=2)
        assert len(limited) <= 2

    def test_since_filter(self, memory):
        memory.record_anomaly({"type": "old"}, date="2025-08-01")
        memory.record_anomaly({"type": "recent"}, date="2025-09-05")

        filtered = memory.get_anomalies(since="2025-09-01")
        types = [a["type"] for a in filtered]
        assert "old" not in types
        assert "recent" in types

    def test_multiple_dates_sorted_desc(self, memory):
        memory.record_anomaly({"type": "earlier"}, date="2025-09-03")
        memory.record_anomaly({"type": "later"}, date="2025-09-05")
        memory.record_anomaly({"type": "oldest"}, date="2025-09-01")

        results = memory.get_anomalies()
        # Latest date should come first
        assert results[0]["_source_date"] == "2025-09-05"

    def test_empty_returns_empty_list(self, memory):
        assert memory.get_anomalies() == []


# ── Research Record Tests ──────────────────────────────────────────────────


class TestResearchRecords:
    def test_save_research(self, memory):
        record_id = memory.save_research(
            question="今天A股为什么这么弱？",
            response={"title": "Market Intelligence"},
        )
        assert record_id  # UUID string
        row = memory.conn.execute(
            "SELECT question, response FROM research_records WHERE id = ?",
            (record_id,),
        ).fetchone()
        assert row is not None
        assert json.loads(row[1])["title"] == "Market Intelligence"

    def test_research_contains_question(self, memory):
        memory.save_research(
            question="Test question content",
            response={"data": "test"},
        )
        rows = list(memory.conn.execute(
            "SELECT question FROM research_records ORDER BY created_at DESC"
        ).fetchall())
        assert any(r[0] == "Test question content" for r in rows)

    def test_default_user_id(self, memory):
        memory.save_research(question="Q", response={})
        row = memory.conn.execute(
            "SELECT user_id FROM research_records LIMIT 1"
        ).fetchone()
        assert row[0] == "anonymous"

    def test_custom_user_id(self, memory):
        memory.save_research(question="Q", response={}, user_id="alice")
        row = memory.conn.execute(
            "SELECT user_id FROM research_records"
        ).fetchone()
        assert row[0] == "alice"


# ── Conversation History Tests ─────────────────────────────────────────────


class TestConversationHistory:
    def test_save_and_get_turns(self, memory):
        conv_id = str(uuid4())

        idx1 = memory.save_turn(conv_id, "问题一", "回答一")
        idx2 = memory.save_turn(conv_id, "问题二", "回答二")
        assert idx1 == 1
        assert idx2 == 2

        history = memory.get_conversation_history(conv_id)
        assert "--- 对话历史" in history
        assert "Q1: 问题一" in history
        assert "A1: 回答一" in history
        assert "Q2: 问题二" in history
        assert "A2: 回答二" in history

    def test_unique_turn_index_per_conversation(self, memory):
        conv_id = str(uuid4())

        memory.save_turn(conv_id, "Q1", "A1")
        memory.save_turn(conv_id, "Q2", "A2")
        # Same conversation, duplicate insert should fail or not create
        # SQLite UNIQUE constraint will prevent it
        idx3 = memory.save_turn(conv_id, "Q3", "A3")
        assert idx3 == 3

    def test_last_n_limit(self, memory):
        conv_id = str(uuid4())
        for i in range(20):
            memory.save_turn(conv_id, f"Q{i+1}", f"A{i+1}")

        history = memory.get_conversation_history(conv_id, last_n=3)
        assert "Q18: Q18" in history
        assert "Q19: Q19" in history
        assert "Q20: Q20" in history
        assert "Q1: Q1" not in history

    def test_empty_conv_returns_empty_string(self, memory):
        assert memory.get_conversation_history("nonexistent") == ""

    def test_answer_summary_can_be_empty(self, memory):
        conv_id = str(uuid4())
        memory.save_turn(conv_id, "Only question", "")
        history = memory.get_conversation_history(conv_id)
        assert "Q1: Only question" in history
        assert "A1:" not in history  # No A line when answer is empty


# ── Context Generation Tests ───────────────────────────────────────────────


class TestContextGeneration:
    def test_empty_when_no_history(self, memory):
        ctx = memory.get_context_for_question("anything")
        assert ctx == ""

    def test_includes_recent_states(self, memory):
        for d in ["2025-09-03", "2025-09-04", "2025-09-05"]:
            memory.save_daily_state(date=d, data={
                "state_label": "Risk-On",
                "strong_areas": ["AI"],
            })

        ctx = memory.get_context_for_question("今天和昨天有什么不同？")
        assert "Market State" in ctx
        assert "2025-09-05" in ctx

    def test_context_format_with_domains(self, memory):
        memory.save_daily_state(date="2025-09-05", data={
            "a-share": "Theme Cooling",
            "crypto": "Funding Extreme",
            "strong_areas": ["Semiconductors", "EV Battery"],
        })

        ctx = memory.get_context_for_question("市场怎么样？")
        assert "A股=Theme Cooling" in ctx
        assert "Crypto=Funding Extreme" in ctx
        assert "Themes=[" in ctx
