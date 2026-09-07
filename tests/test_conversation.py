"""Tests for Conversation History in Market Memory.

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
def mem_dir():
    """Create a temporary directory for memory storage."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / ".mosaic" / "memory"
        mem = MarketMemory(base)
        yield mem


# ── Save Turn Tests ──────────────────────


class TestSaveTurn:
    def test_save_single_turn(self, mem_dir):
        path = mem_dir.save_turn(
            conversation_id="test-conv-1",
            question="今天A股发生了什么？",
            answer_summary="Risk-Off：市场整体走弱，涨停降温。",
        )
        assert path.endswith(".json")
        # File should be in conversations/ dir
        assert mem_dir.base_dir / "conversations" in Path(path).parents

    def test_conversation_file_content(self, mem_dir):
        mem_dir.save_turn(
            conversation_id="test-conv-2",
            question="第一问",
            answer_summary="回答一",
        )
        filepath = mem_dir.base_dir / "conversations" / "test-conv-2.json"
        data = json.loads(filepath.read_text(encoding="utf-8"))
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["question"] == "第一问"
        assert data[0]["answer_summary"] == "回答一"
        assert data[0]["turn_index"] == 1
        assert "_timestamp" in data[0]

    def test_append_multiple_turns(self, mem_dir):
        mem_dir.save_turn("test-conv-3", "Q1", "A1")
        mem_dir.save_turn("test-conv-3", "Q2", "A2")
        mem_dir.save_turn("test-conv-3", "Q3", "A3")

        filepath = mem_dir.base_dir / "conversations" / "test-conv-3.json"
        data = json.loads(filepath.read_text(encoding="utf-8"))
        assert len(data) == 3
        assert data[0]["turn_index"] == 1
        assert data[1]["turn_index"] == 2
        assert data[2]["turn_index"] == 3

    def test_independent_conversations(self, mem_dir):
        mem_dir.save_turn("conv-a", "A问", "A答")
        mem_dir.save_turn("conv-b", "B问", "B答")

        file_a = json.loads(
            (mem_dir.base_dir / "conversations" / "conv-a.json").read_text(encoding="utf-8")
        )
        file_b = json.loads(
            (mem_dir.base_dir / "conversations" / "conv-b.json").read_text(encoding="utf-8")
        )
        assert len(file_a) == 1 and file_a[0]["question"] == "A问"
        assert len(file_b) == 1 and file_b[0]["question"] == "B问"


# ── Get Conversation History Tests ─────────


class TestGetConversationHistory:
    def test_empty_returns_empty_string(self, mem_dir):
        result = mem_dir.get_conversation_history("nonexistent-convo")
        assert result == ""

    def test_basic_format(self, mem_dir):
        mem_dir.save_turn("test-hist-1", question="hello-q", answer_summary="hello-a")

        result = mem_dir.get_conversation_history("test-hist-1")
        assert "Q1:" in result
        assert "A1: hello-a" in result
        assert "--- 对话历史结束 ---" in result
        assert "--- 对话历史 (" in result

    def test_last_n_truncation(self, mem_dir):
        # Create 5 turns
        for i in range(5):
            mem_dir.save_turn("trunc-test", f"Question {i+1}", f"Answer {i+1}")

        full = mem_dir.get_conversation_history("trunc-test")
        assert "Q5:" in full  # All 5 visible

        limited = mem_dir.get_conversation_history("trunc-test", last_n=2)
        assert "Q4:" in limited
        assert "Q5:" in limited
        assert "Q1:" not in limited
        assert "Q2:" not in limited

    def test_multi_turn_format(self, mem_dir):
        mem_dir.save_turn("fmt-test", "Q1内容", "A1摘要")
        mem_dir.save_turn("fmt-test", "Q2内容", "A2摘要")

        result = mem_dir.get_conversation_history("fmt-test")
        assert "Q1: Q1内容" in result
        assert "A1: A1摘要" in result
        assert "Q2: Q2内容" in result
        assert "A2: A2摘要" in result
        assert "最后 2 轮" in result

    def test_corrupted_file_returns_empty(self, mem_dir):
        # Write garbage to the file
        filepath = mem_dir.base_dir / "conversations" / "corrupt.json"
        filepath.write_text("not valid json {{{", encoding="utf-8")

        result = mem_dir.get_conversation_history("corrupt")
        assert result == ""

    def test_long_answer_summary_is_preserved(self, mem_dir):
        long_summary = "Risk-Off：" + "市场整体走弱，涨停数量同步减少，高位题材分化严重。" * 10
        mem_dir.save_turn("long-test", "问题", long_summary)
        result = mem_dir.get_conversation_history("long-test")
        assert "Risk-Off" in result
        assert "市场整体走弱" in result


# ── Conversations Directory Setup Tests ───


class TestConversationsDirSetup:
    def test_conversations_dir_created_on_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / ".mosaic" / "memory"
            mem = MarketMemory(base)
            conv_dir = mem.base_dir / "conversations"
            assert conv_dir.is_dir()
