"""P14 acceptance probe: real sync/SSE THS paths and structured telemetry."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).parents[1]
ARTIFACT = ROOT / "memory" / "p14-acceptance.jsonl"
QUESTIONS = {
    "market": "查询贵州茅台 SH600519 在 2026-10-09 的收盘价、成交量或涨跌幅",
    "announcement": "查询 600519 2026年10月公告",
}


def _sse(text: str) -> tuple[dict | None, int, int]:
    terminal = []
    heartbeats = 0
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            payload = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("terminal") is True:
            terminal.append(payload)
        elif payload.get("step") == "working":
            heartbeats += 1
    return (terminal[-1] if terminal else None, heartbeats, len(terminal))


def _row(payload: dict | None, *, scenario: str, transport: str, status_code: int,
         heartbeat_count: int = 0, terminal_event_count: int = 1) -> dict:
    payload = payload or {}
    report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
    return {
        "run_id": payload.get("run_id"),
        "scenario": scenario,
        "transport": transport,
        "status_code": status_code,
        "status": payload.get("status") or payload.get("delivery_status"),
        "code": payload.get("code"),
        "final_audit_status": payload.get("final_audit_status"),
        "delivery_status": payload.get("delivery_status"),
        "reason_codes": payload.get("reason_codes") or [],
        "persisted": payload.get("persisted", False),
        "collected_datum_count": sum(
            (item.get("datum_count", 0) or len(item.get("normalized", []) or []))
            for item in payload.get("tool_results", []) if isinstance(item, dict)
        ),
        "ledger_evidence_count": None,
        "report_evidence_count": len(report.get("evidence", []) or []),
        "claim_count": len(report.get("claims", []) or []),
        "finish_reason": payload.get("finish_reason"),
        "parse_status": payload.get("parse_status"),
        "candidate_count": payload.get("candidate_count"),
        "accepted_count": payload.get("accepted_count"),
        "rejected_count": payload.get("rejected_count"),
        "heartbeat_count": heartbeat_count,
        "terminal_event_count": terminal_event_count,
        "cleanup_status": "pending",
        "persistence_targets": ["research_records", "conversation", "daily_state", "memory"] if payload.get("persisted") else [],
    }


class _RunEvents(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            outer = json.loads(record.getMessage())
            row = outer if isinstance(outer, dict) and outer.get("event") else json.loads(outer.get("msg", "")) if isinstance(outer, dict) else None
        except (TypeError, ValueError):
            return
        if isinstance(row, dict) and row.get("event"):
            self.rows.append(row)


def _telemetry(row: dict, events: list[dict]) -> None:
    if not row.get("run_id"):
        return
    correlated = [event for event in events if event.get("run_id") == row.get("run_id")]
    contexts = [event for event in correlated if event.get("event") == "reasoning_context"]
    ledger_contexts = [event for event in correlated if event.get("event") in {"reasoning_context", "critic_context"}
                       and event.get("ledger_evidence_count") is not None]
    row["ledger_evidence_count"] = max((int(event["ledger_evidence_count"]) for event in ledger_contexts), default=None)
    row["finish_reason"] = next((item.get("finish_reason") for item in reversed(contexts) if item.get("finish_reason")), None)
    row["parse_status"] = next((item.get("parse_status") for item in reversed(contexts) if item.get("parse_status")), None)
    executions = [event for event in correlated if event.get("event") == "executions"]
    if executions:
        row["collected_datum_count"] = sum(
            int(item.get("datum_count", 0) or 0)
            for event in executions for item in event.get("results", []) if isinstance(item, dict)
        )


def main() -> None:
    run_id = f"p14-{time.time_ns():x}"
    tmp = tempfile.TemporaryDirectory(prefix="mosaic-p14-")
    cleanup = "ok"
    scenario_rows: list[dict] = []
    collector = None
    run_logger = None
    try:
        os.environ["MOSAIC_MEMORY_DB"] = str(Path(tmp.name) / "memory.db")
        from app import main as app_main
        from app.config import get_settings

        collector = _RunEvents()
        run_logger = logging.getLogger("app.graph.run_log")
        run_logger.addHandler(collector)

        get_settings.cache_clear()
        app_main._settings = get_settings()
        app_main._orchestrator = None
        with TestClient(app_main.app) as client:
            for scenario, question in QUESTIONS.items():
                sync_response = client.post("/api/ask", json={"question": question, "domain": "a_share"})
                sync_payload = sync_response.json()
                sync = _row(sync_payload, scenario=scenario, transport="sync", status_code=sync_response.status_code)
                _telemetry(sync, collector.rows)
                sync["probe_run_id"] = run_id
                scenario_rows.append(sync)
                sse_response = client.post("/api/ask/stream", json={"question": question, "domain": "a_share"})
                terminal, heartbeats, terminals = _sse(sse_response.text)
                sse = _row(terminal, scenario=scenario, transport="sse", status_code=sse_response.status_code,
                            heartbeat_count=heartbeats, terminal_event_count=terminals)
                _telemetry(sse, collector.rows)
                sse["probe_run_id"] = run_id
                scenario_rows.append(sse)
    finally:
        if run_logger is not None and collector is not None:
            run_logger.removeHandler(collector)
        try:
            tmp.cleanup()
        except OSError:
            cleanup = "warning"
        ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        with ARTIFACT.open("a", encoding="utf-8") as stream:
            for row in scenario_rows:
                row["cleanup_status"] = cleanup
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.write(json.dumps({"kind": "run_complete", "probe_run_id": run_id,
                                     "cleanup_status": cleanup, "production_ready": False}, ensure_ascii=False) + "\n")
    print(json.dumps({"probe_run_id": run_id, "artifact": str(ARTIFACT), "cleanup_status": cleanup}))


if __name__ == "__main__":
    main()
