"""Release rehearsal against a real service process and isolated SQLite files."""

import json
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest

from app.graph import metrics, run_log
from app.memory.storage import MarketMemory


def _legacy_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE research_records (
                id TEXT PRIMARY KEY, question TEXT NOT NULL, response TEXT NOT NULL,
                created_at TEXT NOT NULL, user_id TEXT DEFAULT 'anonymous'
            )"""
        )
        conn.execute(
            "INSERT INTO research_records (id, question, response, created_at) VALUES (?, ?, ?, ?)",
            ("old-1", "synthetic-history", "{}", "2025-01-01T00:00:00+00:00"),
        )


def _backup(source: Path, target: Path) -> None:
    with sqlite3.connect(source) as original, sqlite3.connect(target) as backup:
        original.backup(backup)


def test_service_process_health_loads_release_settings(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "OPENAI_API_KEY": "synthetic-test-key",
        "MARKET_GATEWAY_MODE": "http",
        "MARKET_GATEWAY_API_KEY": "synthetic-test-key",
        "RESEARCH_BUDGET_SECONDS": "241",
        "GRAPH_RECURSION_LIMIT": "27",
        "SQLITE_BUSY_TIMEOUT_MS": "1300",
        "PERSISTENCE_MAX_RETRIES": "3",
        "PERSISTENCE_RETRY_BACKOFF_MS": "41",
        "MOSAIC_MEMORY_DB": str(tmp_path / "memory.db"),
        "MOSAIC_DATA_DIR": str(tmp_path),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 25
        while True:
            if process.poll() is not None:
                pytest.fail(f"service exited during startup: {process.returncode}")
            try:
                with urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                    health = json.load(response)
                break
            except (URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    pytest.fail("service health check did not become ready")
                time.sleep(0.2)
        assert health["status"] == "ok"
        assert health["gateway_mode"] == "http"
        assert health["gateway_channel"] == "not_applicable"
        assert health["brief_scheduler"] == "running"
        for key, value in {
            "research_budget_seconds": 241,
            "graph_recursion_limit": 27,
            "sqlite_busy_timeout_ms": 1300,
            "persistence_max_retries": 3,
            "persistence_retry_backoff_ms": 41,
        }.items():
            assert health[key] == value
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def test_legacy_backup_migration_failure_restore_and_restart(tmp_path):
    db = tmp_path / "legacy.db"
    backup = tmp_path / "backup.db"
    _legacy_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE INDEX idx_research_persistence_key ON research_records(id)")
    _backup(db, backup)

    with pytest.raises(RuntimeError, match="incompatible idx_research_persistence_key"):
        MarketMemory(db_path=db)
    _backup(backup, db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT question FROM research_records").fetchone()[0] == "synthetic-history"
        conn.execute("DROP INDEX idx_research_persistence_key")

    memory = MarketMemory(db_path=db)
    try:
        assert memory.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert memory.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        index = next(row for row in memory.conn.execute("PRAGMA index_list(research_records)")
                     if row[1] == "idx_research_persistence_key")
        assert index[2] == 1 and index[4] == 1
        assert memory.conn.execute("PRAGMA index_info(idx_research_persistence_key)").fetchone()[2] == "persistence_key"
        assert memory.conn.execute("SELECT question FROM research_records WHERE id='old-1'").fetchone()[0] == "synthetic-history"
        memory.save_research("synthetic-new", {"run_id": "stage9-run"})
        memory.save_turn("stage9-conversation", "synthetic-new", "synthetic-answer")
        memory.save_daily_state(date="2025-01-02", data={"market_state": "synthetic-stable"})
    finally:
        memory.close()

    reopened = MarketMemory(db_path=db)
    try:
        assert reopened.has_persisted_run("stage9-run")
        assert "synthetic-answer" in reopened.get_conversation_history("stage9-conversation")
        assert reopened.get_daily_state("2025-01-02")["market_state"] == "synthetic-stable"
        assert reopened.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        reopened.close()


def test_migration_rejects_same_name_index_with_wrong_predicate(tmp_path):
    db = tmp_path / "wrong-predicate.db"
    _legacy_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE research_records ADD COLUMN persistence_key TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX idx_research_persistence_key "
            "ON research_records(persistence_key) WHERE persistence_key = 'special-only'"
        )

    with pytest.raises(RuntimeError, match="incompatible idx_research_persistence_key"):
        MarketMemory(db_path=db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT question FROM research_records WHERE id = 'old-1'").fetchone()[0] == "synthetic-history"


def test_metrics_parse_collected_json_logs_and_summarize_retry_category():
    events = [
        {"ts": 1.0, "event": "run_start", "run_id": "synthetic-run"},
        {"ts": 2.0, "event": "persistence", "run_id": "synthetic-run", "outcome": "retry",
         "error": "<redacted len=16>", "error_category": "OperationalError"},
        {"ts": 3.0, "event": "persistence", "run_id": "synthetic-run", "outcome": "committed"},
        {"ts": 4.0, "event": "delivery", "run_id": "synthetic-run", "delivery_status": "verified",
         "final_audit_status": "pass", "persisted": True, "error_category": None},
        {"ts": 5.0, "event": "delivery", "run_id": "synthetic-fail", "delivery_status": "failed",
         "persisted": False, "error_category": "timeout"},
    ]
    collected = [json.dumps({"ts": "2026-10-10T00:00:00Z", "level": "INFO", "logger": "app.graph.run_log",
                             "msg": json.dumps(event)}) for event in events]
    collected.append(json.dumps({"logger": "app.main", "msg": "unrelated"}))
    result = metrics.summarize_events(metrics.parse_lines(collected))
    assert result["summary"]["runs"] == 2
    assert result["summary"]["persistence_retries"] == 1
    assert result["summary"]["persisted_runs"] == 1
    assert result["summary"]["persistence_outcome_distribution"] == {"committed": 1}
    assert result["summary"]["persistence_error_distribution"] == {"OperationalError": 1}
    assert result["summary"]["delivery_distribution"] == {"verified": 1, "failed": 1}
    assert result["summary"]["error_category_distribution"] == {"timeout": 1}


def test_persistence_log_category_does_not_copy_exception_text(caplog):
    secret = "database is locked at C:/private/production/memory.db"
    with caplog.at_level(logging.INFO, logger="app.graph.run_log"):
        run_log.log_persistence(
            {"run_id": "safe-run"},
            attempt=1,
            max_attempts=2,
            outcome="retry",
            retryable=True,
            error=secret,
            error_category="OperationalError",
        )
    message = caplog.records[-1].getMessage()
    assert secret not in message
    payload = json.loads(message)
    assert payload["error_category"] == "OperationalError"
    assert payload["error"].startswith("<redacted len=")
