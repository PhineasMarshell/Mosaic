"""Stage 8: release migration, compatibility, configuration, and log safety.

Migration and settings tests use real SQLite files and validation. The process
startup case launches two Python workers against one legacy database. API/CLI
compatibility tests replace only the graph and persistence dependencies; they
do not cover a live model or gateway.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

import app.main as main
import app.cli as cli
from app.agent.persistence import persist_research
from app.config import Settings
from app.graph import run_log
from app.memory.storage import MarketMemory
from app.models.response import MarketIntelligence, ResearchResponse


def _legacy_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE research_records (
           id TEXT PRIMARY KEY,
           question TEXT NOT NULL,
           response TEXT NOT NULL,
           created_at TEXT NOT NULL,
           user_id TEXT DEFAULT 'anonymous'
        )"""
    )
    conn.execute(
        "INSERT INTO research_records (id, question, response, created_at) VALUES (?, ?, ?, ?)",
        ("legacy-1", "历史问题", json.dumps({"legacy": True}), "2025-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()


def test_legacy_schema_history_migrates_and_reopens_idempotently(tmp_path):
    path = tmp_path / "legacy.db"
    _legacy_database(path)

    first = MarketMemory(db_path=path)
    try:
        columns = {row[1] for row in first.conn.execute("PRAGMA table_info(research_records)")}
        assert "persistence_key" in columns
        assert first.conn.execute("SELECT question FROM research_records").fetchone()[0] == "历史问题"
        first.save_research("新问题", {"run_id": "release-run"})
    finally:
        first.close()

    second = MarketMemory(db_path=path)
    try:
        assert second.conn.execute("SELECT COUNT(*) FROM research_records").fetchone()[0] == 2
        assert second.has_persisted_run("release-run")
    finally:
        second.close()


def test_empty_persistence_keys_are_normalized_and_never_deduplicate(tmp_path):
    path = tmp_path / "empty-key.db"
    memory = MarketMemory(db_path=path)
    memory.conn.execute(
        "INSERT INTO research_records (id, question, response, created_at, persistence_key) VALUES (?, ?, ?, ?, ?)",
        ("empty-legacy", "q", "{}", "2025-01-01T00:00:00+00:00", "   "),
    )
    memory.close()

    reopened = MarketMemory(db_path=path)
    try:
        assert reopened.conn.execute(
            "SELECT persistence_key FROM research_records WHERE id = 'empty-legacy'"
        ).fetchone()[0] is None
        first = reopened.save_research("q1", {"run_id": " "})
        second = reopened.save_research("q2", {})
        assert first != second
        assert reopened.conn.execute(
            "SELECT COUNT(*) FROM research_records WHERE persistence_key IS NULL"
        ).fetchone()[0] == 3
        assert not reopened.has_persisted_run(" ")
    finally:
        reopened.close()


def test_duplicate_existing_keys_do_not_block_index_migration(tmp_path):
    path = tmp_path / "duplicate-keys.db"
    _legacy_database(path)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE research_records ADD COLUMN persistence_key TEXT")
    conn.execute("UPDATE research_records SET persistence_key = 'same-run'")
    conn.execute(
        "INSERT INTO research_records (id, question, response, created_at, persistence_key) VALUES (?, ?, ?, ?, ?)",
        ("legacy-2", "历史问题二", "{}", "2025-01-02T00:00:00+00:00", "same-run"),
    )
    conn.commit()
    conn.close()

    memory = MarketMemory(db_path=path)
    try:
        rows = memory.conn.execute(
            "SELECT persistence_key FROM research_records ORDER BY created_at"
        ).fetchall()
        assert rows == [("same-run",), (None,)]
        assert memory.has_persisted_run("same-run")
    finally:
        memory.close()


def test_two_processes_can_start_and_migrate_one_legacy_database(tmp_path):
    path = tmp_path / "multi-process.db"
    _legacy_database(path)
    script = """
from pathlib import Path
import sys
from app.memory.storage import MarketMemory
memory = MarketMemory(db_path=Path(sys.argv[1]))
memory.save_research('worker', {'run_id': sys.argv[2]})
memory.close()
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(path), f"worker-{index}"],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(2)
    ]
    outputs = [process.communicate(timeout=20) for process in processes]
    assert all(process.returncode == 0 for process in processes), outputs

    memory = MarketMemory(db_path=path)
    try:
        assert memory.conn.execute("SELECT COUNT(*) FROM research_records").fetchone()[0] == 3
        assert memory.conn.execute(
            "SELECT COUNT(*) FROM pragma_table_info('research_records') WHERE name = 'persistence_key'"
        ).fetchone()[0] == 1
    finally:
        memory.close()


def test_committed_wal_data_is_visible_after_reopen(tmp_path):
    path = tmp_path / "wal-reopen.db"
    writer = MarketMemory(db_path=path)
    writer.save_research("history", {"run_id": "wal-run"})
    reader = MarketMemory(db_path=path)
    try:
        assert writer.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert reader.has_persisted_run("wal-run")
    finally:
        reader.close()
        writer.close()

    reopened = MarketMemory(db_path=path)
    try:
        assert reopened.has_persisted_run("wal-run")
        assert reopened.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        reopened.close()


def test_commit_failure_rolls_back_transaction():
    class FailingConnection:
        def __init__(self):
            self.rollback_count = 0

        def execute(self, statement):
            assert statement == "BEGIN IMMEDIATE"

        def commit(self):
            raise sqlite3.OperationalError("disk I/O error")

        def rollback(self):
            self.rollback_count += 1

    connection = FailingConnection()
    memory = object.__new__(MarketMemory)
    memory._local = SimpleNamespace(conn=connection, generation=0)
    memory._generation = 0
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        with memory.transaction():
            pass
    assert connection.rollback_count == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sqlite_busy_timeout_ms", -1),
        ("sqlite_busy_timeout_ms", 30_001),
        ("persistence_max_retries", -1),
        ("persistence_max_retries", 6),
        ("persistence_retry_backoff_ms", -1),
        ("persistence_retry_backoff_ms", 1_001),
        ("research_budget_seconds", 0),
        ("research_budget_seconds", 86_401),
        ("graph_recursion_limit", 0),
        ("graph_recursion_limit", 1_001),
        ("sqlite_busy_timeout_ms", "not-an-integer"),
        ("persistence_max_retries", "not-an-integer"),
        ("persistence_retry_backoff_ms", "not-an-integer"),
        ("research_budget_seconds", "not-an-integer"),
        ("graph_recursion_limit", "not-an-integer"),
    ],
)
def test_release_settings_reject_invalid_bounds(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_release_settings_accept_documented_edges():
    settings = Settings(
        sqlite_busy_timeout_ms=0,
        persistence_max_retries=5,
        persistence_retry_backoff_ms=1_000,
        research_budget_seconds=86_400,
        graph_recursion_limit=1_000,
    )
    assert settings.sqlite_busy_timeout_ms == 0
    assert settings.persistence_max_retries == 5
    assert settings.persistence_retry_backoff_ms == 1_000
    assert settings.research_budget_seconds == 86_400
    assert settings.graph_recursion_limit == 1_000


def test_busy_timeout_is_applied_to_real_connection(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("SQLITE_BUSY_TIMEOUT_MS", "47")
    get_settings.cache_clear()
    memory = MarketMemory(db_path=tmp_path / "timeout.db")
    try:
        assert memory.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 47
    finally:
        memory.close()
        get_settings.cache_clear()


def test_invalid_environment_timeout_cannot_silently_use_default(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("SQLITE_BUSY_TIMEOUT_MS", "not-an-integer")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError):
            MarketMemory(db_path=tmp_path / "invalid-timeout.db")
    finally:
        get_settings.cache_clear()


def test_sync_and_sse_success_remain_readable_by_legacy_client(monkeypatch):
    report = MarketIntelligence(title="t", market_state="stable", state_label="Neutral", confidence="low")

    class Graph:
        async def astream(self, initial_state, stream_mode=None, config=None):
            yield "values", {
                **initial_state,
                "report": report,
                "results": [],
                "critique": {"verdict": "pass"},
                "errors": [],
            }

    class Orchestrator:
        async def run(self, question, domain=None, conversation_id=None):
            return ResearchResponse(
                question=question,
                report=report,
                conversation_id=conversation_id,
                critique={"verdict": "pass"},
                final_audit_status="pass",
                delivery_status="verified",
                run_id="private-run-id",
            )

        def _ensure_graph(self):
            return Graph()

    async def persist(_result, _question, _conversation_id):
        return True

    monkeypatch.setattr(main, "_orchestrator", Orchestrator())
    monkeypatch.setattr(main, "_settings", SimpleNamespace(
        research_budget_seconds=30,
        stream_heartbeat_seconds=1,
        graph_recursion_limit=25,
    ))
    monkeypatch.setattr(main, "persist_research", persist)
    client = TestClient(main.app)

    sync = client.post("/api/ask", json={"question": "q"})
    assert sync.status_code == 200
    body = sync.json()
    legacy_view = {field: body[field] for field in ("question", "report", "errors")}
    assert legacy_view["question"] == "q"
    assert legacy_view["report"]["market_state"] == "stable"
    assert legacy_view["errors"] == []
    assert (body["delivery_status"], body["persisted"]) == ("verified", True)
    assert "run_id" not in body

    stream = client.post("/api/ask/stream", json={"question": "q"})
    assert stream.status_code == 200
    chunks = [line[6:] for line in stream.text.splitlines() if line.startswith("data: ")]
    final = json.loads(chunks[-1])
    legacy_stream_view = {field: final[field] for field in ("question", "report", "errors")}
    assert legacy_stream_view == legacy_view
    assert (final["delivery_status"], final["persisted"]) == ("verified", True)
    assert "run_id" not in final


def test_cli_success_does_not_print_internal_run_id(monkeypatch, capsys):
    class Orchestrator:
        def __init__(self, _settings):
            pass

        async def run(self, question):
            return ResearchResponse(
                question=question,
                report=MarketIntelligence(
                    title="t", market_state="stable", state_label="Neutral", confidence="low"
                ),
                critique={"verdict": "pass"},
                final_audit_status="pass",
                delivery_status="verified",
                run_id="private-cli-run-id",
            )

    async def persist(_result, _question, _conversation_id):
        return True

    monkeypatch.setattr(cli, "Orchestrator", Orchestrator)
    monkeypatch.setattr(cli, "persist_research", persist)
    asyncio.run(cli.main("q"))
    captured = capsys.readouterr()
    assert "private-cli-run-id" not in captured.out + captured.err
    assert "delivery_status=verified" in captured.err
    assert "persisted=True" in captured.err


def test_structured_logs_do_not_contain_free_text_or_user_identifiers(caplog):
    secret = "USER_PRIVATE_QUESTION_MODEL_OUTPUT_7f3d9c"
    with caplog.at_level(logging.INFO, logger="app.graph.run_log"):
        payload = run_log.emit(
            "run_start",
            run_id="run-safe",
            question=secret,
            conversation_id="private-session-42",
            error=secret,
        )
    message = caplog.records[-1].getMessage()
    assert secret not in message
    assert "private-session-42" not in message
    assert payload["question"].startswith("<redacted len=")
    assert payload["error"].startswith("<redacted len=")


def test_rejected_question_is_not_copied_into_application_log(caplog):
    secret = "PRIVATE_INVALID_QUESTION_7f3d9c"
    with caplog.at_level(logging.WARNING, logger="app.main"):
        with pytest.raises(Exception) as error:
            main._parse_ask_payload({"question": [secret]}, "POST /api/ask")
    assert getattr(error.value, "status_code", None) == 400
    assert secret not in caplog.text


def test_nested_tool_query_is_redacted(caplog):
    secret = "MODEL_OUTPUT_AND_USER_QUERY_9a2b"
    with caplog.at_level(logging.INFO, logger="app.graph.run_log"):
        run_log.emit(
            "executions",
            run_id="run-safe",
            results=[{"tool_key": "news", "status": "success", "arguments": {"query": secret}}],
        )
    assert secret not in caplog.records[-1].getMessage()


def test_non_transactional_lock_failure_is_not_retried_or_duplicated():
    class NonTransactionalMemory:
        def __init__(self):
            self.research_writes = 0

        def save_research(self, *_args, **_kwargs):
            self.research_writes += 1

        def save_turn(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        def save_daily_state(self, *_args, **_kwargs):
            raise AssertionError("daily state must not be reached")

    result = ResearchResponse(
        question="q",
        report=MarketIntelligence(title="t", market_state="stable", state_label="Neutral", confidence="low"),
        critique={"verdict": "pass"},
        final_audit_status="pass",
        delivery_status="verified",
        run_id="non-tx-run",
    )
    memory = NonTransactionalMemory()
    assert asyncio.run(
        persist_research(result, "q", "conv", memory=memory, max_retries=5, retry_backoff_ms=0)
    ) is False
    assert memory.research_writes == 1


@pytest.mark.parametrize("message, expected_attempts", [("database is locked", 3), ("disk I/O error", 1)])
def test_retry_is_bounded_and_permanent_errors_stop(tmp_path, message, expected_attempts):
    memory = MarketMemory(db_path=tmp_path / "retry.db")
    calls = 0

    @contextmanager
    def failing_transaction():
        nonlocal calls
        calls += 1
        raise sqlite3.OperationalError(message)
        yield

    memory.transaction = failing_transaction
    result = ResearchResponse(
        question="q",
        report=MarketIntelligence(title="t", market_state="stable", state_label="Neutral", confidence="low"),
        critique={"verdict": "pass"},
        final_audit_status="pass",
        delivery_status="verified",
        run_id="retry-run",
    )
    try:
        assert asyncio.run(
            persist_research(result, "q", "conv", memory=memory, max_retries=2, retry_backoff_ms=0)
        ) is False
        assert result.persisted is False
        assert calls == expected_attempts
        assert memory.conn.execute("SELECT COUNT(*) FROM research_records").fetchone()[0] == 0
    finally:
        memory.close()
