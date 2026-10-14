"""P16 public-entry live observations and strictly offline artifact replay.

Live changes only memory/data paths: production settings, ToolRuntime, Graph,
LLM, Critic, Finalize and persistence run unchanged. Each request has its own
retained temporary SQLite. Replay validates observations without requesting
services or presenting offline validation as a live success.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import socket
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.p14_acceptance import _RunEvents, _sse, _telemetry

ARTIFACT = ROOT / "memory" / "p16-acceptance.jsonl"
SCENARIOS = {
    "quote": "SH600519 在 2026-10-09 的收盘价是多少",
    "today": "今天 A 股发生了什么",
    "closed_day": "2026-10-11 A 股发生了什么",
    "announcement": "600519 2026年10月公告",
}
TABLES = ("research_records", "conversations", "daily_states")
AUDIT_FIELDS = (
    "run_id", "status", "code", "delivery_status", "final_audit_status",
    "reason_codes", "persisted", "requested_date", "as_of_date", "market_closed",
    "partial", "source", "coverage", "source_switch", "stage", "exception_type",
    "fallback_diagnostics", "candidate_count", "accepted_count", "rejected_count",
    "rejection_reasons", "critique",
)


@contextmanager
def offline():
    """Fail closed on DNS and socket connections, including accidental imports."""
    def denied(*args, **kwargs):
        raise AssertionError("P16 replay must never access the network")

    with (patch.object(socket, "getaddrinfo", denied),
          patch.object(socket, "create_connection", denied),
          patch.object(socket.socket, "connect", denied),
          patch.object(socket.socket, "connect_ex", denied)):
        yield


def append(path: Path, rows: list[dict], secrets: tuple[str, ...] = ()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for row in rows:
            line = json.dumps(row, ensure_ascii=False, default=str)
            for secret in secrets:
                if len(secret) >= 8:
                    line = line.replace(secret, "<credential-redacted>")
            stream.write(line + "\n")


def preservation_snapshot() -> dict:
    """Hash existing DBs, evening files, JSONL and logs; never change them."""
    files: dict[str, str] = {}
    inaccessible: list[str] = []
    for base in (ROOT / "memory", ROOT / "tmp", ROOT / ".tmp"):
        for directory, _, names in os.walk(base, onerror=lambda exc: inaccessible.append(str(exc.filename))):
            for name in names:
                path = Path(directory) / name
                if path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".jsonl", ".log"} or "evening" in path.parts:
                    try:
                        files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
                    except OSError:
                        inaccessible.append(str(path))
    for path in ROOT.glob("*.db*"):
        files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"files": files, "inaccessible": sorted(set(inaccessible))}


def check_preservation(baseline: dict) -> dict:
    changed, missing, appended = [], [], []
    for name, digest in baseline["files"].items():
        path = Path(name)
        if not path.exists():
            missing.append(name)
        elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            # The real MCP client can append to its existing operational log.
            # Prove the entire original prefix hash remains intact rather than
            # declaring any changed hash an overwrite or restoring its bytes.
            current = path.read_bytes()
            prefix_hash = hashlib.sha256()
            original_bytes = 0
            intact = False
            if path.suffix == ".log":
                for line in current.splitlines(keepends=True):
                    prefix_hash.update(line)
                    original_bytes += len(line)
                    if prefix_hash.hexdigest() == digest:
                        intact = True
                        break
            if intact:
                appended.append({"path": name, "original_prefix_bytes": original_bytes,
                                 "current_bytes": len(current), "original_prefix_hash_verified": True})
            else:
                changed.append(name)
    return {"protected_files": len(baseline["files"]), "changed": changed,
            "missing": missing, "append_only_logs": appended, "inaccessible_at_start": baseline["inaccessible"]}


def database_observation(path: Path, conversation_id: str, run_id: str | None) -> dict:
    """Read committed data on a separate connection, then use existing retrieval."""
    from app.memory.storage import MarketMemory

    memory = MarketMemory(db_path=path)
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as conn:
            counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES}
            saved = conn.execute("SELECT response FROM research_records WHERE persistence_key = ?", (run_id,)).fetchone()
            daily = conn.execute("SELECT date FROM daily_states ORDER BY date DESC LIMIT 1").fetchone()
            conv_count = conn.execute("SELECT COUNT(*) FROM conversations WHERE conversation_id = ?", (conversation_id,)).fetchone()[0]
        response = json.loads(saved[0]) if saved else {}
        return {
            "persistence_counts": counts, "database_path": str(path),
            "history_retrieval": {
                "conversation_readable": bool(memory.get_conversation_history(conversation_id)),
                "conversation_rows": conv_count,
                "daily_state_readable": bool(daily and memory.get_daily_state(daily[0])),
                "recent_states_readable": bool(memory.get_recent_states()),
                "research_run_readable": bool(saved and memory.has_persisted_run(run_id)),
                "saved_delivery_status": response.get("delivery_status"),
                "saved_final_audit_status": response.get("final_audit_status"),
                "saved_claim_count": len((response.get("report") or {}).get("claims") or []),
                "memory_is_existing_storage_and_retrieval": True,
            },
        }
    finally:
        memory.close()


def observation(payload: dict, events: list[dict], *, scenario: str, transport: str,
                http_status: int | None, heartbeat_count: int, terminal_count: int,
                origin: str = "real_network") -> dict:
    report = payload.get("report") or {}
    from app.agent.evidence_gate import _code_owned_limitation

    pure_structure = bool(report.get("claims")) and all(
        _code_owned_limitation(c.get("claim", ""), requested_date=payload.get("requested_date"),
                               expected_date=payload.get("as_of_date"), market_closed=payload.get("market_closed"))
        for c in report["claims"]
    )
    row = {name: payload.get(name) for name in AUDIT_FIELDS}
    row.update({
        "kind": "scenario", "mode": "live" if origin == "real_network" else "offline_fixture", "observation_origin": origin,
        "scenario": scenario, "question": SCENARIOS[scenario], "transport": transport,
        "status_code": http_status, "persisted": payload.get("persisted") is True,
        "reason_codes": payload.get("reason_codes") or [],
        "heartbeat_count": heartbeat_count, "terminal_event_count": terminal_count,
        "collected_datum_count": 0, "ledger_evidence_count": None,
        "report_evidence_count": len(report.get("evidence") or []),
        "claim_count": len(report.get("claims") or []),
        "report_is_evidence_only_fallback": "evidence_only_fallback" in (payload.get("reason_codes") or []),
        "pure_structure": pure_structure,
        "terminal_audit_fields": sorted(name for name in AUDIT_FIELDS if name in payload),
        "production_ready": False,
    })
    _telemetry(row, events)
    correlated = [event for event in events if event.get("run_id") == row.get("run_id")]
    executions = [item for event in correlated if event.get("event") == "executions"
                  for item in event.get("results", []) if isinstance(item, dict)]
    row["actual_routes"] = executions
    row["primary_sources"] = sorted({item["primary_source"] for item in executions if item.get("primary_source")})
    row["fallback_sources"] = sorted({item["fallback_source"] for item in executions if item.get("fallback_source")})
    row["network_tool_attempt_count"] = sum(
        isinstance(attempt, dict) and isinstance(attempt.get("attempt"), int)
        for item in executions for attempt in item.get("attempts") or []
    )
    row["events"] = correlated
    row["claim_audits"] = [event for event in correlated if event.get("event") == "claim_audit"]
    ledger = [event for event in correlated if event.get("event") == "ledger_summary"]
    if ledger:
        row["ledger_evidence_count"] = ledger[-1]["ledger_evidence_count"]
    elif row["ledger_evidence_count"] is None and not executions:
        row["ledger_evidence_count"] = 0
    row["ledger_summaries"] = ledger
    row["model_calls"] = [event for event in correlated if event.get("event") == "llm"]
    fallback_events = [event for event in correlated if event.get("event") == "evidence_fallback"]
    row["fallback_events"] = fallback_events
    fallback = payload.get("fallback_diagnostics") or (fallback_events[-1] if fallback_events else {})
    for key in ("candidate_count", "accepted_count", "rejected_count", "rejection_reasons", "candidate_evidence_ids",
                "rejected_candidates", "candidate_rejections", "report_evidence_ids"):
        row[key] = fallback.get(key)
    row["fallback_applied"] = fallback.get("applied", False)
    # Only display evidence actually selected by the audited response; never raw tool bodies.
    row["report"] = {key: report.get(key) for key in ("title", "claims", "evidence", "data_caveats")} if report else None
    row["market_success"] = bool(origin == "real_network" and row["status"] == "verified" and row["persisted"]
                                 and row["claim_count"] and not row["pure_structure"] and scenario != "announcement"
                                 and any(re.search(r"price|close|last|change|收盘|涨跌|点位|成交|家数", str(e.get("metric") or ""), re.I)
                                         for e in report.get("evidence") or []))
    return row


def validate_row(row: dict) -> None:
    assert row["cleanup_status"] in {"ok", "warning"}, "unfinished cleanup"
    assert row["production_ready"] is False
    assert row["run_id"], "missing request correlation"
    assert row["status"] in {"verified", "degraded", "blocked", "failed"}
    assert row["terminal_event_count"] == 1, "terminal must occur exactly once"
    assert isinstance(row["reason_codes"], list)
    assert row["collected_datum_count"] >= 0
    assert isinstance(row["ledger_evidence_count"], int), "ledger count not observed"
    counts = row["persistence_counts"]
    if row["status"] != "verified":
        assert row["persisted"] is False and set(counts.values()) == {0}, "unsafe persistence"
    else:
        assert row["final_audit_status"] == "pass"
        assert row["persisted"] is True and set(counts.values()) == {1}, "verified delivery not committed"
        history = row["history_retrieval"]
        assert all(history[key] for key in ("conversation_readable", "daily_state_readable", "recent_states_readable", "research_run_readable"))
        assert history["saved_delivery_status"] == "verified" and history["saved_final_audit_status"] == "pass"
    if row["report_is_evidence_only_fallback"]:
        assert row["status"] == "degraded" and not row["persisted"]
    if row["pure_structure"]:
        assert row["market_success"] is False
    for route in row["actual_routes"]:
        if route.get("status") in {"error", "timeout"}:
            assert route["datum_count"] == 0, "failed source contributed data"
    report = row.get("report")
    if report:
        from app.agent.evidence_gate import match_claims_to_evidence

        checks = match_claims_to_evidence(
            report, report.get("evidence") or [], strict_provenance=row["status"] == "verified",
            expected_date=row.get("as_of_date"), requested_date=row.get("requested_date"),
            market_closed=row.get("market_closed"), expected_domain="a_share",
            expected_instruments=["600519"] if row["scenario"] in {"quote", "announcement"} else [],
        )
        assert checks and all(check["supported"] for check in checks), checks
    if row["candidate_count"] is not None:
        assert row["candidate_count"] == row["accepted_count"] + row["rejected_count"]
    if row.get("mode") == "live":
        assert row["observation_origin"] == "real_network"


def live(args) -> None:
    from fastapi.testclient import TestClient

    probe_id = f"p16-{time.time_ns():x}"
    scenarios = args.scenario or list(SCENARIOS)
    transports = ["sync", "sse"] if args.transport == "both" else [args.transport]
    completed_cleanup_statuses: list[str] = []
    for scenario in scenarios:
        for transport in transports:
            # Retain the new SQLite for inspection, but close handles before recording cleanup.
            work = ROOT / ".tmp" / f"{probe_id}-{scenario}-{transport}-{uuid.uuid4().hex[:8]}"
            work.mkdir(parents=True, exist_ok=False)
            db = work / "memory.db"
            conversation_id = f"{probe_id}-{scenario}-{transport}"
            previous_env = {key: os.environ.get(key) for key in ("MOSAIC_MEMORY_DB", "MOSAIC_DATA_DIR")}
            os.environ["MOSAIC_MEMORY_DB"] = str(db)
            os.environ["MOSAIC_DATA_DIR"] = str(work / "data")
            from app import main as app_main
            from app.cache import market_cache
            from app.config import get_settings
            from app.memory import storage

            get_settings.cache_clear()
            settings = get_settings()
            assert Path(settings.mosaic_memory_db).resolve() == db.resolve(), "refuse non-isolated configured DB"
            assert Path(settings.mosaic_data_dir).resolve() == (work / "data").resolve(), "refuse non-isolated data directory"
            app_main._settings = settings
            app_main._orchestrator = None
            market_cache.clear()
            assert storage._shared_memory is None, "refuse to reuse an existing memory connection"
            collector = _RunEvents()
            run_logger = logging.getLogger("app.graph.run_log")
            run_logger.addHandler(collector)
            row = None
            cleanup_errors: list[str] = []
            # Endpoint-only TestClient: no scheduled briefs or unrelated MCP startup selfcheck.
            # Endpoint initialization still creates the production orchestrator/Graph/ToolRuntime.
            client = TestClient(app_main.app)
            try:
                body = {"question": SCENARIOS[scenario], "domain": "a_share", "conversation_id": conversation_id}
                endpoint = "/api/ask" if transport == "sync" else "/api/ask/stream"
                response = client.post(endpoint, json=body)
                if transport == "sync":
                    payload, heartbeat_count, terminal_count = response.json(), 0, 1
                else:
                    payload, heartbeat_count, terminal_count = _sse(response.text)
                row = observation(payload or {}, collector.rows, scenario=scenario, transport=transport,
                                  http_status=response.status_code, heartbeat_count=heartbeat_count,
                                  terminal_count=terminal_count)
                row.update(database_observation(db, conversation_id, row["run_id"]))
            except Exception as exc:
                # Preserve evidence of the business exception; cleanup must not replace it.
                ids = [event.get("run_id") for event in collector.rows if event.get("run_id")]
                if row is None:
                    row = observation({"run_id": ids[-1] if ids else f"unobserved-{uuid.uuid4().hex}",
                                       "status": "failed", "code": "probe_exception", "final_audit_status": "error",
                                       "reason_codes": ["probe_request_exception"], "exception_type": type(exc).__name__},
                                      collector.rows, scenario=scenario, transport=transport, http_status=None,
                                      heartbeat_count=0, terminal_count=0)
                row["business_exception_type"] = type(exc).__name__
                row.update(database_observation(db, conversation_id, row["run_id"]))
                raise
            finally:
                for close in (client.close, lambda: storage._shared_memory.close() if storage._shared_memory else None):
                    try:
                        close()
                    except Exception as exc:  # noqa: BLE001 - retain business errors and collection on cleanup failure
                        cleanup_errors.append(type(exc).__name__)
                storage._shared_memory = None
                run_logger.removeHandler(collector)
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                get_settings.cache_clear()
                app_main._settings = None
                app_main._orchestrator = None
                if row is not None:
                    row.update({"probe_run_id": probe_id, "conversation_id": conversation_id,
                                "label": args.label, "cleanup_status": "warning" if cleanup_errors else "ok",
                                "cleanup_exception_types": cleanup_errors, "temporary_database_retained": True,
                                "runtime_asia_shanghai": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                                "production_config": {"model": settings.openai_model,
                                    "primary_source": settings.market_primary_source,
                                    "routing_enabled": settings.market_source_routing_enabled,
                                    "research_budget_seconds": settings.research_budget_seconds,
                                    "llm_max_completion_tokens": settings.llm_max_completion_tokens,
                                    "max_rewrites": settings.effective_max_rewrites,
                                    "max_research_rounds": settings.effective_max_research_rounds}})
                    try:
                        validate_row(row)
                    except AssertionError as exc:
                        row["acceptance_errors"] = [str(exc)]
                    else:
                        row["acceptance_errors"] = []
                    append(args.artifact, [row], (settings.openai_api_key, settings.iwencai_api_key,
                                               settings.market_gateway_api_key))
                    completed_cleanup_statuses.append(row["cleanup_status"])
                    print(json.dumps({key: row.get(key) for key in ("run_id", "scenario", "transport", "status",
                                     "finish_reason", "parse_status", "persisted", "acceptance_errors", "cleanup_status")}, ensure_ascii=False), flush=True)
    append(args.artifact, [{"kind": "run_complete", "probe_run_id": probe_id, "mode": "live",
                           "scenarios": scenarios, "transports": transports, "label": args.label,
                           "production_ready": False,
                           "cleanup_status": "warning" if "warning" in completed_cleanup_statuses else "ok"}])


def replay(path: Path) -> None:
    with offline():
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        scenarios = [row for row in rows if row.get("kind") == "scenario"]
        assert scenarios, "no saved observations"
        assert {row["scenario"] for row in scenarios} >= set(SCENARIOS)
        for scenario in SCENARIOS:
            assert {row["transport"] for row in scenarios if row["scenario"] == scenario} >= {"sync", "sse"}
        for row in scenarios:
            validate_row(row)
            assert row.get("acceptance_errors") == [], row.get("acceptance_errors")
            if row["observation_origin"] == "real_network":
                assert set(AUDIT_FIELDS) <= set(row["terminal_audit_fields"]), "incomplete terminal audit schema"
        # Independent runs may return different values. Compare only presence
        # of audit fields when the two transports reached the same status.
        for status in ("verified", "degraded", "blocked", "failed"):
            same_status = [row for row in scenarios if row["status"] == status]
            schemas = {tuple(row["terminal_audit_fields"]) for row in same_status
                       if row["observation_origin"] == "real_network"}
            assert len(schemas) <= 1, f"sync/SSE audit schema drift: {status}"
        for marker in ("Bearer ", "Authorization:", "IWENCAI_API_KEY=", "OPENAI_API_KEY="):
            assert marker not in path.read_text(encoding="utf-8"), "credential marker in artifact"
        print(json.dumps({"mode": "offline_replay", "network_access": "forbidden", "rows": len(scenarios),
                          "origins": sorted({row["observation_origin"] for row in scenarios}),
                          "live_market_verified": sum(row["market_success"] for row in scenarios
                                                      if row["observation_origin"] == "real_network"),
                          "pure_structure_rows": sum(row["pure_structure"] for row in scenarios),
                          "production_ready": False}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--mode", choices=("live", "replay"), required=True)
    parser.add_argument("--artifact", type=Path, default=ARTIFACT)
    parser.add_argument("--scenario", choices=tuple(SCENARIOS), action="append")
    parser.add_argument("--transport", choices=("both", "sync", "sse"), default="both")
    parser.add_argument("--label", default="initial")
    args = parser.parse_args()
    if args.mode == "replay" and (args.scenario or args.transport != "both" or args.label != "initial"):
        parser.error("live selection options are not valid in replay")
    live(args) if args.mode == "live" else replay(args.artifact)


if __name__ == "__main__":
    main()
