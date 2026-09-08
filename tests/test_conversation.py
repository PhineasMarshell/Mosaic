"""Tests for Conversation History in Market Memory (SQLite).

Coverage:
- Save a turn (append-store pattern)
- Retrieve conversation history with last_n truncation
- Empty conversation handling
- Multiple turns accumulate correctly
- Prompt-injectable format is correct
"""

import json
import tempfile
from pathlib import Path

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


# ── Save Turn Tests ──────────────────────


class TestSaveTurn:
    def test_save_single_turn(self, memory):
        idx = memory.save_turn(
            conversation_id="test-conv-1",
            question="今天A股发生了什么？",
            answer_summary="Risk-Off：市场整体走弱，涨停降温。",
        )
        assert idx == 1
        row = memory.conn.execute(
            "SELECT conversation_id, question, answer_summary FROM conversations WHERE id = ?",
            (memory.conn.execute(
                "SELECT id FROM conversations WHERE conversation_id = ? AND turn_index = ?",
                ("test-conv-1", 1),
            ).fetchone()[0],),
        ).fetchone()
        assert row is not None
        assert row[1] == "今天A股发生了什么？"
        memory.save_turn(
            conversation_id="test-conv-2",
            question="第一问",
            answer_summary="回答一",
        )
        rows = list(memory.conn.execute(
            "SELECT turn_index, question, answer_summary, created_at FROM conversations WHERE conversation_id = ? ORDER BY turn_index",
            ("test-conv-2",),
        ).fetchall())
        assert len(rows) == 1
        assert rows[0][0] == 1
        assert rows[0][1] == "第一问"
        assert rows[0][2] == "回答一"
        assert rows[0][3] is not None

    def test_append_multiple_turns(self, memory):
        memory.save_turn("test-conv-3", "Q1", "A1")
        memory.save_turn("test-conv-3", "Q2", "A2")
        memory.save_turn("test-conv-3", "Q3", "A3")

        rows = list(memory.conn.execute(
            "SELECT turn_index FROM conversations WHERE conversation_id = ? ORDER BY turn_index",
            ("test-conv-3",),
        ).fetchall())
        indices = [r[0] for r in rows]
        assert indices == [1, 2, 3]

    def test_independent_conversations(self, memory):
        memory.save_turn("conv-a", "A问", "A答")
        memory.save_turn("conv-b", "B问", "B答")

        rows_a = list(memory.conn.execute(
            "SELECT question FROM conversations WHERE conversation_id = ?",
            ("conv-a",),
        ).fetchall())
        rows_b = list(memory.conn.execute(
            "SELECT question FROM conversations WHERE conversation_id = ?",
            ("conv-b",),
        ).fetchall())
        assert len(rows_a) == 1 and rows_a[0][0] == "A问"
        assert len(rows_b) == 1 and rows_b[0][0] == "B问"


# ── Get Conversation History Tests ─────────


class TestGetConversationHistory:
    def test_empty_returns_empty_string(self, memory):
        result = memory.get_conversation_history("nonexistent-convo")
        assert result == ""

    def test_basic_format(self, memory):
        memory.save_turn("test-hist-1", question="hello-q", answer_summary="hello-a")

        result = memory.get_conversation_history("test-hist-1")
        assert "Q1:" in result
        assert "A1: hello-a" in result
        assert "--- 对话历史结束 ---" in result
        assert "--- 对话历史 (" in result

    def test_last_n_truncation(self, memory):
        # Create 5 turns
        for i in range(5):
            memory.save_turn("trunc-test", f"Question {i+1}", f"Answer {i+1}")

        full = memory.get_conversation_history("trunc-test")
        assert "Q5:" in full  # All 5 visible

        limited = memory.get_conversation_history("trunc-test", last_n=2)
        assert "Q4:" in limited
        assert "Q5:" in limited
        assert "Q1:" not in limited
        assert "Q2:" not in limited

    def test_multi_turn_format(self, memory):
        memory.save_turn("fmt-test", "Q1内容", "A1摘要")
        memory.save_turn("fmt-test", "Q2内容", "A2摘要")

        result = memory.get_conversation_history("fmt-test")
        assert "Q1: Q1内容" in result
        assert "A1: A1摘要" in result
        assert "Q2: Q2内容" in result
        assert "A2: A2摘要" in result
        assert "最后 2 轮" in result

    def test_corrupted_db_returns_empty(self, memory):
        """Even with corrupted data in the DB, non-matching conv returns empty."""
        # Insert garbage into a different conversation's data
        memory.conn.execute(
            """INSERT INTO conversations (id, conversation_id, turn_index, question, answer_summary, created_at)
               VALUES ('garbage-id', 'safe-conv', 1, 'Q', 'A', 'now')""",
        )
        # Querying nonexistent should still work fine
        result = memory.get_conversation_history("corrupt-not-exists")
        assert result == ""

    def test_long_answer_summary_is_preserved(self, memory):
        long_summary = "Risk-Off：" + "市场整体走弱，涨停数量同步减少，高位题材分化严重。" * 10
        memory.save_turn("long-test", "问题", long_summary)
        result = memory.get_conversation_history("long-test")
        assert "Risk-Off" in result
        assert "市场整体走弱" in result


# ── Database Setup Tests ───


class TestDatabaseSetup:
    def test_db_file_created_on_init(self):
        tmp = tempfile.mkdtemp()
        try:
            db_path = Path(tmp) / "memory.db"
            mem = MarketMemory(db_path=db_path)
            assert db_path.exists()
            mem.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            mem.conn.close()
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_tables_exist(self):
        tmp = tempfile.mkdtemp()
        try:
            db_path = Path(tmp) / "memory.db"
            mem = MarketMemory(db_path=db_path)
            tables = [
                row[0] for row in mem.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            ]
            assert "conversations" in tables
            assert "daily_states" in tables
            assert "anomalies" in tables
            assert "research_records" in tables
            mem.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            mem.conn.close()
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
