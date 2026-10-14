"""Run one isolated real sync probe per P10 target and append redacted telemetry."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).parents[1]
ARTIFACT = ROOT / "memory" / "p10-focused-acceptance.jsonl"
SCENARIOS = {
    "announcement": "查询 600519 2026年10月公告",
    "quote": "查询贵州茅台 600519 在 2026-10-09 的行情",
}


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


def _append(row: dict[str, Any]) -> None:
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    with ARTIFACT.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


@contextmanager
def _best_effort_tempdir():
    """Finish a probe even when a Windows child still holds a temp handle.

    Cleanup is outside the business result.  A failed request remains visible
    in the redacted JSONL and a cleanup warning is recorded separately; no
    exception from rmtree can turn an already completed probe into a false
    business failure.
    """
    temp_dir = tempfile.TemporaryDirectory(prefix="mosaic-p10-focused-", ignore_cleanup_errors=True)
    cleanup_warning: str | None = None
    try:
        yield temp_dir.name
    finally:
        try:
            temp_dir.cleanup()
        except OSError as exc:
            cleanup_warning = type(exc).__name__
        if cleanup_warning:
            _append({"kind": "cleanup_warning", "cleanup_status": "warning", "exception_type": cleanup_warning})
            print(json.dumps({"cleanup_status": "warning", "exception_type": cleanup_warning}, ensure_ascii=False))
        else:
            print(json.dumps({"cleanup_status": "ok"}, ensure_ascii=False))


def _scenario_summary(scenario: str, response, wall_ms: float, events: list[dict]) -> dict[str, Any]:
    payload = response.json()
    run_id = payload.get("run_id")
    correlated = [row for row in events if row.get("run_id") == run_id]
    executions = [row for row in correlated if row.get("event") == "executions"]
    reasoning = [row for row in correlated if row.get("event") == "reasoning_context"]
    critics = [row for row in correlated if row.get("event") == "critic"]
    return {
        "kind": "scenario",
        "scenario": scenario,
        "transport": "sync",
        "status_code": response.status_code,
        "run_id": run_id,
        "wall_ms": round(wall_ms, 1),
        "terminal": {
            "delivery_status": payload.get("delivery_status"),
            "final_audit_status": payload.get("final_audit_status"),
            "persisted": payload.get("persisted"),
            "stage": payload.get("stage"),
            "exception_type": payload.get("exception_type"),
            "reason_codes": sorted(set(payload.get("reason_codes") or [])),
            "elapsed_ms": payload.get("elapsed_ms"),
            "report_evidence_count": len((payload.get("report") or {}).get("evidence") or [])
            if isinstance(payload.get("report"), dict) else 0,
            "claim_count": len((payload.get("report") or {}).get("claims") or [])
            if isinstance(payload.get("report"), dict) else 0,
            "claim_refs": [
                {"evidence_ids": claim.get("evidence_ids") or [], "claim_type": claim.get("claim_type")}
                for claim in ((payload.get("report") or {}).get("claims") or [])
                if isinstance(claim, dict)
            ] if isinstance(payload.get("report"), dict) else [],
            "tool_results": [
                {
                    "tool_key": item.get("tool_key"),
                    "operation_id": item.get("operation_id") or item.get("tool"),
                    "status": item.get("status"),
                    "partial": bool(item.get("partial")),
                    "datum_count": len(item.get("normalized") or []),
                    "duration_ms": item.get("duration_ms"),
                }
                for item in payload.get("tool_results") or [] if isinstance(item, dict)
            ],
        },
        "telemetry": {
            "events": correlated,
            "collected_datum_count": sum(
                result.get("datum_count", 0)
                for row in executions for result in row.get("results", [])
            ),
            "ledger_evidence_count": max(
                (row.get("ledger_evidence_count", 0) for row in reasoning), default=0
            ),
            "selected_reasoning_evidence_count": max(
                (row.get("selected_evidence_count", 0) for row in reasoning), default=0
            ),
            "critic_issue_count": sum(
                len(row.get("issues", [])) for row in critics
            ),
        },
        "production_ready": False,
    }


def probe() -> None:
    from app import main
    from app.config import get_settings

    probe_id = f"p10-focused-{time.time_ns():x}"
    _append({"kind": "run_start", "probe_run_id": probe_id, "transport": "sync", "scenarios": list(SCENARIOS)})
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
                    started = time.perf_counter()
                    response = client.post("/api/ask", json={"question": question, "domain": "a_share"})
                    wall_ms = (time.perf_counter() - started) * 1000
                    row = _scenario_summary(scenario, response, wall_ms, collector.rows)
                    row["probe_run_id"] = probe_id
                    _append(row)
                    print(json.dumps({
                        "scenario": scenario,
                        "run_id": row["run_id"],
                        "delivery_status": row["terminal"]["delivery_status"],
                        "persisted": row["terminal"]["persisted"],
                        "wall_ms": row["wall_ms"],
                    }, ensure_ascii=False))
        _append({"kind": "run_complete", "probe_run_id": probe_id, "production_ready": False})
    finally:
        logger.removeHandler(collector)


if __name__ == "__main__":
    probe()
