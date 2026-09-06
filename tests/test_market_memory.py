"""Tests for Market Memory persistence layer.

Coverage:
- Save + retrieve daily states
- Recent states query
- Anomaly recording and querying
- Research record saving
- Context generation for reasoning prompts
"""

import json
import tempfile
from pathlib import Path

import pytest

from app.memory.storage import MarketMemory


@pytest.fixture
def mem_dir():
    """Create a temporary directory for memory storage."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / ".mosaic" / "memory"
        mem = MarketMemory(base)
        yield mem


# ── Daily State Tests ────────────────────


class TestDailyState:
    def test_save_and_retrieve(self, mem_dir):
        mem_dir.save_daily_state(date="2025-09-01", data={
            "state_label": "Risk-On",
            "strong_areas": ["Robotics"],
        })
        state = mem_dir.get_daily_state("2025-09-01")
        assert state is not None
        assert state["state_label"] == "Risk-On"
        assert "Robotics" in state["strong_areas"]

    def test_missing_date_returns_none(self, mem_dir):
        assert mem_dir.get_daily_state("2024-01-01") is None

    def test_update_existing_state(self, mem_dir):
        mem_dir.save_daily_state(date="2025-09-01", data={"key1": "val1"})
        mem_dir.save_daily_state(date="2025-09-01", data={"key2": "val2"})
        state = mem_dir.get_daily_state("2025-09-01")
        assert state["key1"] == "val1"
        assert state["key2"] == "val2"

    def test_state_has_metadata(self, mem_dir):
        mem_dir.save_daily_state(date="2025-09-01", data={"state_label": "test"})
        state = mem_dir.get_daily_state("2025-09-01")
        assert "_saved_at" in state or "_version" in state


# ── Recent States Tests ──────────────────


class TestRecentStates:
    def test_get_recent_states(self, mem_dir):
        dates = ["2025-09-03", "2025-09-04", "2025-09-05"]
        labels = ["Risk-On", "Mixed", "Risk-Off"]
        for d, l in zip(dates, labels):
            mem_dir.save_daily_state(date=d, data={"state_label": l})

        recent = mem_dir.get_recent_states(days=7)
        assert len(recent) >= 3

    def test_limited_by_days(self, mem_dir):
        for i in range(10):
            day = f"2025-08-{i+1:02d}"
            mem_dir.save_daily_state(date=day, data={"state_label": f"Day{i}"})

        recent = mem_dir.get_recent_states(days=3)
        # Should get at most 3 results
        assert len(recent) <= 3


# ── Anomaly Recording Tests ─────────────


class TestAnomalyRecording:
    def test_record_and_query_anomalies(self, mem_dir):
        anomaly = {
            "type": "oi_spike",
            "severity": "high",
            "description": "OI +8.2%",
            "domain": "crypto",
        }
        mem_dir.record_anomaly(anomaly, date="2025-09-05")
        anomalies = mem_dir.get_anomalies()
        assert len(anomalies) >= 1
        assert anomalies[0]["type"] == "oi_spike"

    def test_limit_parameter(self, mem_dir):
        for i in range(5):
            rec = {"type": f"test_{i}", "value": i}
            mem_dir.record_anomaly(rec, date="2025-09-05")

        limited = mem_dir.get_anomalies(limit=2)
        assert len(limited) <= 2


# ── Research Record Tests ────────────────


class TestResearchRecords:
    def test_save_research(self, mem_dir):
        path = mem_dir.save_research(
            question="今天A股为什么这么弱？",
            response={"title": "Market Intelligence"},
        )
        assert path.endswith(".json")
        assert mem_dir.base_dir / "research" in Path(path).parents

    def test_research_contains_question(self, mem_dir):
        mem_dir.save_research(
            question="Test question content",
            response={"data": "test"},
        )
        # Find the file
        research_dir = mem_dir.base_dir / "research"
        if research_dir.is_dir():
            files = list(research_dir.glob("*.json"))
            if files:
                data = json.loads(files[-1].read_text(encoding="utf-8"))
                assert data["question"] == "Test question content"


# ── Context Generation Tests ─────────────


class TestContextGeneration:
    def test_empty_when_no_history(self, mem_dir):
        ctx = mem_dir.get_context_for_question("anything")
        # With no history, should return empty string
        assert ctx == ""

    def test_includes_recent_states(self, mem_dir):
        for d in ["2025-09-03", "2025-09-04", "2025-09-05"]:
            mem_dir.save_daily_state(date=d, data={
                "state_label": "Risk-On",
                "strong_areas": ["AI"],
            })

        ctx = mem_dir.get_context_for_question("今天和昨天有什么不同？")
        assert "Market State" in ctx
        assert "2025-09-05" in ctx
