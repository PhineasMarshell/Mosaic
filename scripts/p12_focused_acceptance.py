"""P12 narrow real acceptance for the explicit-date / explicit-instrument route.

The artifact contains terminal metadata and structured telemetry only.  It
never stores model completions, raw gateway responses, prompts, or environment
values.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import p9_e2e_acceptance as harness  # noqa: E402

ARTIFACT = ROOT / "memory" / "p12-focused-acceptance.jsonl"
SCENARIOS = {
    "quote": "查询贵州茅台 SH600519 在 2026-10-09 的行情",
    "announcement": "查询 SH600519 2026年10月公告",
}


def _append(row: dict[str, Any]) -> None:
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    with ARTIFACT.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


@contextmanager
def _best_effort_tempdir():
    temp_dir = tempfile.TemporaryDirectory(prefix="mosaic-p12-focused-", ignore_cleanup_errors=True)
    warning: str | None = None
    try:
        yield temp_dir.name
    finally:
        try:
            temp_dir.cleanup()
        except OSError as exc:
            warning = type(exc).__name__
        _append({
            "kind": "cleanup",
            "cleanup_status": "warning" if warning else "ok",
            "exception_type": warning,
        })
        print(json.dumps({"cleanup_status": "warning" if warning else "ok", "exception_type": warning}))


class _RunEvents(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            row = json.loads(record.getMessage())
        except (TypeError, ValueError):
            return
        if isinstance(row, dict) and row.get("event"):
            self.rows.append(row)


def _event_summary(events: list[dict[str, Any]], run_id: str | None, *, report_evidence_count: int = 0) -> dict[str, Any]:
    correlated = [row for row in events if row.get("run_id") == run_id]
    reasoning = [row for row in correlated if row.get("event") == "reasoning_context"]
    llm = [row for row in correlated if row.get("event") == "llm"]
    executions = [row for row in correlated if row.get("event") == "executions"]
    return {
        "events": correlated,
        "collected_datum_count": sum(item.get("datum_count", 0) for row in executions for item in row.get("results", [])),
        "ledger_evidence_count": max((row.get("ledger_evidence_count", 0) for row in reasoning), default=0),
        "report_evidence_count": report_evidence_count,
        "reasoning": [
            {
                "max_tokens": row.get("request_max_tokens"),
                "completion": row.get("completion_tokens")
                if row.get("completion_tokens") is not None
                else (row.get("usage") or {}).get("completion_tokens"),
                "completion_chars": row.get("completion_chars"),
                "finish_reason": row.get("finish_reason"),
                "parse_status": row.get("parse_status"),
            }
            for row in [*reasoning, *llm]
        ],
    }


def _sse_result(response_text: str) -> tuple[dict[str, Any], int, int]:
    results: list[dict[str, Any]] = []
    heartbeat_count = 0
    for line in response_text.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            payload = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("step") == "working":
            heartbeat_count += 1
        if isinstance(payload, dict) and ("delivery_status" in payload or payload.get("code")):
            results.append(payload)
    if not results:
        raise AssertionError("SSE stream did not contain a terminal result event")
    return results[-1], heartbeat_count, len(results)


def probe() -> None:
    from app import main
    from app.config import get_settings

    probe_id = f"p12-focused-{time.time_ns():x}"
    collector = _RunEvents()
    logger = logging.getLogger("app.graph.run_log")
    logger.addHandler(collector)
    try:
        with _best_effort_tempdir() as temp_dir:
            os.environ["MOSAIC_MEMORY_DB"] = str(Path(temp_dir) / "memory.db")
            get_settings.cache_clear()
            main._settings = get_settings()
            main._orchestrator = None
            with TestClient(main.app) as client:
                for scenario, question in SCENARIOS.items():
                    sync_response = client.post("/api/ask", json={"question": question, "domain": "a_share"})
                    sync_payload = sync_response.json()
                    sync_run = sync_payload.get("run_id")
                    sync_terminal = harness._redacted_payload(sync_payload)
                    _append({
                        "kind": "scenario", "probe_run_id": probe_id, "scenario": scenario,
                        "transport": "sync", "run_id": sync_run, "status_code": sync_response.status_code,
                        "terminal": sync_terminal, "telemetry": _event_summary(
                            collector.rows, sync_run, report_evidence_count=sync_terminal.get("evidence_count", 0)
                        ),
                        "production_ready": False,
                    })

                    sse_response = client.post("/api/ask/stream", json={"question": question, "domain": "a_share"})
                    sse_payload, heartbeat_count, terminal_count = _sse_result(sse_response.text)
                    sse_run = sse_payload.get("run_id")
                    sse_terminal = harness._redacted_payload(sse_payload)
                    parity_fields = {
                        field: {
                            "sync_present": field in sync_terminal,
                            "sse_present": field in sse_terminal,
                            "sync": sync_terminal.get(field),
                            "sse": sse_terminal.get(field),
                        }
                        for field in ("run_id", "delivery_status", "final_audit_status", "reason_codes", "persisted")
                    }
                    _append({
                        "kind": "scenario", "probe_run_id": probe_id, "scenario": scenario,
                        "transport": "sse", "run_id": sse_run, "status_code": sse_response.status_code,
                        "heartbeat_count": heartbeat_count, "terminal_event_count": terminal_count,
                        "terminal": sse_terminal, "parity_fields": parity_fields,
                        "telemetry": _event_summary(
                            collector.rows, sse_run, report_evidence_count=sse_terminal.get("evidence_count", 0)
                        ),
                        "production_ready": False,
                    })
                    if terminal_count != 1:
                        raise AssertionError(f"SSE terminal event count={terminal_count}")
        _append({"kind": "run_complete", "probe_run_id": probe_id, "production_ready": False})
        print(json.dumps({"probe_run_id": probe_id, "artifact": str(ARTIFACT), "production_ready": False}))
    finally:
        logger.removeHandler(collector)


if __name__ == "__main__":
    probe()
