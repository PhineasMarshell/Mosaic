"""Tests for Market Memory (SQLite) persistence layer.

Coverage:
- Save + retrieve daily states (upsert)
- Recent states query with limit and domain filter
- Anomaly recording and querying
- Research record saving
- Conversation history (append-store, turn ordering)
- Conversation (conversation_id, turn_index) UNIQUE constraint: direct duplicate
  INSERT must raise sqlite3.IntegrityError, and concurrent save_turn from two
  connections must never persist a duplicate turn index (TOCTOU in MAX+1 ->
  the constraint is the backstop; the loser must fail loudly, not silently)
- Context generation for reasoning prompts
"""

import json
import sqlite3
import tempfile
import threading
from datetime import datetime as real_datetime
from pathlib import Path
from uuid import uuid4

import pytest

import app.memory.storage as storage
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
        memory.save_daily_state(
            date="2025-09-01",
            data={
                "state_label": "Risk-On",
                "strong_areas": ["Robotics"],
            },
        )
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
        for d, label in zip(dates, labels, strict=True):
            memory.save_daily_state(date=d, data={"state_label": label})

        recent = memory.get_recent_states(days=7)
        assert len(recent) >= 3
        # Most recent first
        assert recent[0]["_file_date"] == "2025-09-05"

    def test_limited_by_days(self, memory):
        for i in range(10):
            day = f"2025-08-{i + 1:02d}"
            memory.save_daily_state(date=day, data={"state_label": f"Day{i}"})

        recent = memory.get_recent_states(days=3)
        assert len(recent) <= 3

    def test_domain_filter(self, memory):
        memory.save_daily_state(
            date="2025-09-01",
            data={
                "a-share": {"state_label": "Cooling"},
                "state_label": "Mixed",
            },
        )
        memory.save_daily_state(
            date="2025-09-02",
            data={
                "state_label": "Risk-On",
            },
        )

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
        memory.record_anomaly(anomaly, date="2025-09-05")
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
        rows = list(memory.conn.execute("SELECT question FROM research_records ORDER BY created_at DESC").fetchall())
        assert any(r[0] == "Test question content" for r in rows)

    def test_default_user_id(self, memory):
        memory.save_research(question="Q", response={})
        row = memory.conn.execute("SELECT user_id FROM research_records LIMIT 1").fetchone()
        assert row[0] == "anonymous"

    def test_custom_user_id(self, memory):
        memory.save_research(question="Q", response={}, user_id="alice")
        row = memory.conn.execute("SELECT user_id FROM research_records").fetchone()
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

    def test_turn_indices_increment_per_turn(self, memory):
        """顺序追加时轮次索引递增。

        注意：本用例**不触发**唯一约束（save_turn 的 MAX+1 在顺序场景下永不撞车，
        T32 探针证实：把 schema 里的 UNIQUE 约束删掉它照样绿）。约束本身由
        下面两条用例验证：直接 INSERT 重复行必抛 IntegrityError；两条连接并发
        save_turn 不允许静默落库重复行。
        """
        conv_id = str(uuid4())

        idx1 = memory.save_turn(conv_id, "Q1", "A1")
        idx2 = memory.save_turn(conv_id, "Q2", "A2")
        idx3 = memory.save_turn(conv_id, "Q3", "A3")
        assert idx1 == 1
        assert idx2 == 2
        assert idx3 == 3

    def test_duplicate_turn_index_raises_integrity_error(self, memory):
        """直接 INSERT 重复 (conversation_id, turn_index) 必须触发唯一约束。

        T32：名为"唯一约束"的旧用例从不触发约束。这里绕过 save_turn 直接落一条
        重复行——约束若丢失（被改坏），本用例必红。
        """
        conv_id = str(uuid4())
        memory.save_turn(conv_id, "Q1", "A1")

        with pytest.raises(sqlite3.IntegrityError):
            memory.conn.execute(
                """INSERT INTO conversations
                       (id, conversation_id, turn_index, question, answer_summary, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (str(uuid4()), conv_id, 1, "Q-dup", "A-dup", "2025-01-01T00:00:00+00:00"),
            )

    def test_concurrent_save_turn_from_two_connections_no_silent_duplicate(self, memory, monkeypatch):
        """两条连接并发 save_turn：MAX+1 有 TOCTOU，唯一约束是最后防线。

        save_turn 先 SELECT MAX(turn_index) 再 INSERT（storage.py 的 MAX+1），
        两条连接同时读、同时写会算出同一个索引。用 datetime.now 门闩把两条执行
        确定性拦在 SELECT 之后、INSERT 之前（复现 TOCTOU 窗口），断言：
        - 赢家拿到索引、输家**大声**抛 IntegrityError（不许静默成功）；
        - 表里不落重复 (conversation_id, turn_index) 行；
        - 撞车之后下一条 save_turn 正常续号（序列未被破坏）。
        若删掉 schema 里的 UNIQUE 约束：两方都"成功"，表里落两条 turn_index=1
        —— 本用例必红（探针实测）。
        """
        conv_id = str(uuid4())
        arrived = threading.Semaphore(0)
        release = threading.Event()

        class _GatedDatetime:
            """把 save_turn 拦在 SELECT MAX 之后、INSERT 之前（TOCTOU 窗口）。"""

            def now(self, tz=None):
                arrived.release()
                release.wait(timeout=10)
                return real_datetime.now(tz)

        monkeypatch.setattr(storage, "datetime", _GatedDatetime())

        successes: list[int] = []
        failures: list[Exception] = []

        def worker(tag: str) -> None:
            try:
                successes.append(memory.save_turn(conv_id, f"Q-{tag}", f"A-{tag}"))
            except sqlite3.IntegrityError as exc:
                failures.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in ("A", "B")]
        for t in threads:
            t.start()
        for _ in threads:
            arrived.acquire(timeout=10)  # 两条执行都已过 SELECT、卡在 INSERT 前
        release.set()
        for t in threads:
            t.join(timeout=10)

        # 撞车是确定性的：两人都算出 1，先 INSERT 的赢，输家必须大声失败
        assert failures, "两条并发 save_turn 没有撞车（TOCTOU 窗口未复现），门闩失效"
        assert all(isinstance(e, sqlite3.IntegrityError) for e in failures)
        assert successes == [1]
        # 表里不落重复行：唯一约束兜底成功
        rows = memory.conn.execute(
            "SELECT turn_index FROM conversations WHERE conversation_id = ?", (conv_id,)
        ).fetchall()
        indices = [r[0] for r in rows]
        assert indices == [1]
        # 撞车之后序列正常续号
        assert memory.save_turn(conv_id, "Q3", "A3") == 2
        memory.close()  # 收掉工作线程的连接（T21 登记表），避免残留句柄

    def test_last_n_limit(self, memory):
        conv_id = str(uuid4())
        for i in range(20):
            memory.save_turn(conv_id, f"Q{i + 1}", f"A{i + 1}")

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
            memory.save_daily_state(
                date=d,
                data={
                    "state_label": "Risk-On",
                    "strong_areas": ["AI"],
                },
            )

        ctx = memory.get_context_for_question("今天和昨天有什么不同？")
        assert "Market State" in ctx
        assert "2025-09-05" in ctx

    def test_context_format_with_domains(self, memory):
        memory.save_daily_state(
            date="2025-09-05",
            data={
                "a-share": "Theme Cooling",
                "crypto": "Funding Extreme",
                "strong_areas": ["Semiconductors", "EV Battery"],
            },
        )

        ctx = memory.get_context_for_question("市场怎么样？")
        assert "A股=Theme Cooling" in ctx
        assert "Crypto=Funding Extreme" in ctx
        assert "Themes=[" in ctx
