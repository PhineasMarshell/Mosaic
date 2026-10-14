"""P13 real acceptance: truncation-safe delivery, sync/SSE symmetry, real evidence.

Records only structured telemetry (run ids, statuses, counts, reason codes and
the bounded fallback rejection map).  Model completions, prompts, raw gateway
payloads and environment values are never written to the artifact.
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

ARTIFACT = ROOT / "memory" / "p13-focused-acceptance.jsonl"
#: 场景行先缓冲、临时目录清理后再落盘，因此每一行都带本次运行真实的
#: ``cleanup_status``；同一结论也写在 ``cleanup`` 行与 ``run_complete`` 行上。
_LAST_CLEANUP: dict[str, Any] = {"cleanup_status": "pending", "exception_type": None}
SCENARIOS = {
    "market": "查询贵州茅台 SH600519 在 2026-10-09 的行情",
    "announcement": "查询 SH600519 2026年10月公告",
}
REQUIRED_FIELDS = (
    "run_id",
    "status",
    "code",
    "reason_codes",
    "finish_reason",
    "parse_status",
    "candidate_count",
    "accepted_count",
    "rejected_count",
    "rejection_reasons",
    "fallback_applied",
    "collected_datum_count",
    "ledger_evidence_count",
    "report_evidence_count",
    "claim_count",
    "report_is_evidence_only_fallback",
    "heartbeat_count",
    "terminal_event_count",
    "persisted",
    "cleanup_status",
)


def _append(row: dict[str, Any]) -> None:
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    with ARTIFACT.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


@contextmanager
def _best_effort_tempdir():
    temp_dir = tempfile.TemporaryDirectory(prefix="mosaic-p13-focused-", ignore_cleanup_errors=True)
    warning: str | None = None
    try:
        yield temp_dir.name
    finally:
        try:
            temp_dir.cleanup()
        except OSError as exc:
            warning = type(exc).__name__
        status = "warning" if warning else "ok"
        _LAST_CLEANUP["cleanup_status"] = status
        _LAST_CLEANUP["exception_type"] = warning
        _append({"kind": "cleanup", "cleanup_status": status, "exception_type": warning})
        print(json.dumps({"cleanup_status": status, "exception_type": warning}))


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


def _event_summary(events: list[dict[str, Any]], run_id: str | None) -> dict[str, Any]:
    correlated = [row for row in events if row.get("run_id") == run_id]
    reasoning = [row for row in correlated if row.get("event") == "reasoning_context"]
    llm = [row for row in correlated if row.get("event") == "llm"]
    executions = [row for row in correlated if row.get("event") == "executions"]
    fallbacks = [row for row in correlated if row.get("event") == "evidence_fallback"]
    reasoning_rows = [*reasoning, *llm]
    last_fallback = fallbacks[-1] if fallbacks else {}
    return {
        "events": correlated,
        "collected_datum_count": sum(
            item.get("datum_count", 0) for row in executions for item in row.get("results", [])
        ),
        "ledger_evidence_count": max((row.get("ledger_evidence_count", 0) for row in reasoning), default=0),
        "selected_evidence_count": max((row.get("selected_evidence_count", 0) for row in reasoning), default=0),
        "fallback_events": fallbacks,
        "reasoning": [
            {
                "max_tokens": row.get("request_max_tokens"),
                "completion": row.get("completion_tokens")
                if row.get("completion_tokens") is not None
                else (row.get("usage") or {}).get("completion_tokens"),
                "completion_chars": row.get("completion_chars"),
                "finish_reason": row.get("finish_reason"),
                "parse_status": row.get("parse_status"),
                "phase": row.get("phase"),
                "target_filter": row.get("target_filter"),
            }
            for row in reasoning_rows
        ],
        "finish_reason": next(
            (row.get("finish_reason") for row in reversed(reasoning_rows) if row.get("finish_reason")), None
        ),
        "parse_status": next(
            (row.get("parse_status") for row in reversed(reasoning_rows) if row.get("parse_status")), None
        ),
        "fallback": {
            "phase": last_fallback.get("phase"),
            "applied": last_fallback.get("applied"),
            "enabled": last_fallback.get("fallback_enabled"),
            "reason": last_fallback.get("fallback_reason"),
            "candidate_count": last_fallback.get("candidate_count"),
            "accepted_count": last_fallback.get("accepted_count"),
            "rejected_count": last_fallback.get("rejected_count"),
            "rejection_reasons": last_fallback.get("rejection_reasons") or {},
            "candidate_rejections": last_fallback.get("candidate_rejections") or {},
            "candidate_evidence_ids": last_fallback.get("candidate_evidence_ids") or {},
        },
    }


def _sse_terminal(response_text: str) -> tuple[dict[str, Any] | None, int, int, list[dict[str, Any]]]:
    """Return (terminal payload, heartbeat count, terminal count, non-terminal payloads)."""
    terminal: list[dict[str, Any]] = []
    progress: list[dict[str, Any]] = []
    heartbeat_count = 0
    for line in response_text.splitlines():
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
            continue
        progress.append(payload)
        if payload.get("step") == "working":
            heartbeat_count += 1
    return (terminal[-1] if terminal else None), heartbeat_count, len(terminal), progress


def _terminal_row(payload: dict[str, Any]) -> dict[str, Any]:
    from app.research.reasoning import EVIDENCE_ONLY_FALLBACK_TITLE

    report = payload.get("report") if isinstance(payload.get("report"), dict) else None
    evidence = (report or {}).get("evidence") or []
    claims = (report or {}).get("claims") or []
    return {
        "run_id": payload.get("run_id"),
        "status": payload.get("status") or payload.get("delivery_status"),
        "code": payload.get("code"),
        "delivery_status": payload.get("delivery_status"),
        "final_audit_status": payload.get("final_audit_status"),
        "reason_codes": sorted({str(code) for code in (payload.get("reason_codes") or [])}),
        "persisted": payload.get("persisted"),
        "report_present": report is not None,
        "claim_count": len(claims),
        "report_evidence_count": len(evidence),
        # 代码拥有的字面量比较，不读正文：这份交付物到底是 fallback 造的，
        # 还是模型成文经 degraded 过滤后的产物。
        "report_is_evidence_only_fallback": bool(
            report and report.get("title") == EVIDENCE_ONLY_FALLBACK_TITLE
        ),
        "evidence_ids": sorted({str(item.get("id")) for item in evidence if isinstance(item, dict) and item.get("id")}),
        "claim_refs": [
            {"evidence_ids": [str(v) for v in (claim.get("evidence_ids") or [])], "claim_type": claim.get("claim_type")}
            for claim in claims
            if isinstance(claim, dict)
        ],
    }


def _fallback_fields(terminal: dict[str, Any], telemetry: dict[str, Any]) -> dict[str, Any]:
    diagnostics = terminal.get("fallback_diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    fallback = telemetry.get("fallback") or {}
    candidate = diagnostics.get("candidate_count")
    accepted = diagnostics.get("accepted_count")
    rejected = diagnostics.get("rejected_count")
    reasons = diagnostics.get("rejection_reasons")
    if candidate is None:
        candidate = fallback.get("candidate_count")
    if accepted is None:
        accepted = fallback.get("accepted_count")
    if rejected is None:
        rejected = fallback.get("rejected_count")
    if not reasons:
        reasons = fallback.get("rejection_reasons") or {}
    return {
        "fallback_applied": bool(diagnostics.get("applied", fallback.get("applied"))),
        "fallback_enabled": diagnostics.get("enabled", fallback.get("enabled")),
        "fallback_reason": diagnostics.get("reason") or fallback.get("reason"),
        "candidate_count": candidate,
        "accepted_count": accepted,
        "rejected_count": rejected,
        "rejection_reasons": reasons,
        "candidate_rejections": diagnostics.get("candidate_rejections") or fallback.get("candidate_rejections") or {},
        "candidate_evidence_ids": diagnostics.get("candidate_evidence_ids") or fallback.get("candidate_evidence_ids") or {},
    }


def _assert_controlled(transport: str, scenario: str, row: dict[str, Any]) -> None:
    status = row.get("status")
    code = row.get("code")
    if code == "blocked" or status == "blocked":
        assert row["report_present"] is False, f"{transport}/{scenario}: blocked must not deliver a report"
    if status == "degraded":
        assert row["report_present"] is True, f"{transport}/{scenario}: degraded must deliver facts"
        assert row["persisted"] is False, f"{transport}/{scenario}: degraded must not persist"
    if status in {"blocked", "failed"}:
        assert row["persisted"] is False, f"{transport}/{scenario}: {status} must not persist"
    if status == "verified":
        assert row["final_audit_status"] == "pass", f"{transport}/{scenario}: verified requires pass audit"
    # §二.4：模型截断 + 有合格直接证据 ⇒ 受控 degraded；没有 ⇒ blocked。
    if row.get("finish_reason") == "length":
        accepted = row.get("accepted_count") or 0
        if accepted > 0:
            assert status == "degraded", f"{transport}/{scenario}: truncated with evidence must degrade"
            assert row.get("fallback_applied") is True, f"{transport}/{scenario}: degraded must be the fallback"
            assert row.get("report_is_evidence_only_fallback") is True, (
                f"{transport}/{scenario}: a truncated last attempt must deliver the fallback report"
            )
        else:
            assert status == "blocked", f"{transport}/{scenario}: truncated without evidence must block"
    if status == "verified":
        assert row.get("fallback_applied") is False, f"{transport}/{scenario}: fallback must not back a verified run"


def probe() -> None:
    from app import main
    from app.config import get_settings

    probe_id = f"p13-focused-{time.time_ns():x}"
    collector = _RunEvents()
    logger = logging.getLogger("app.graph.run_log")
    logger.addHandler(collector)
    # 场景行在临时目录清理之前生成，因此先缓冲、清理后再落盘，
    # 这样 JSONL 里每一行的 cleanup_status 都是本次运行的真实终值。
    scenario_rows: list[dict[str, Any]] = []
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
                    sync_terminal = _terminal_row(sync_payload)
                    sync_telemetry = _event_summary(collector.rows, sync_terminal["run_id"])
                    sync_row = {
                        "kind": "scenario",
                        "probe_run_id": probe_id,
                        "scenario": scenario,
                        "transport": "sync",
                        "status_code": sync_response.status_code,
                        "heartbeat_count": 0,
                        "terminal_event_count": 1,
                        **sync_terminal,
                        **_fallback_fields(sync_payload, sync_telemetry),
                        "finish_reason": sync_telemetry["finish_reason"],
                        "parse_status": sync_telemetry["parse_status"],
                        "collected_datum_count": sync_telemetry["collected_datum_count"],
                        "ledger_evidence_count": sync_telemetry["ledger_evidence_count"],
                        "selected_evidence_count": sync_telemetry["selected_evidence_count"],
                        "reasoning": sync_telemetry["reasoning"],
                        "cleanup_status": _LAST_CLEANUP["cleanup_status"],
                        "production_ready": False,
                    }
                    _assert_controlled("sync", scenario, sync_row)
                    scenario_rows.append(sync_row)

                    sse_response = client.post("/api/ask/stream", json={"question": question, "domain": "a_share"})
                    sse_payload, heartbeat_count, terminal_count, progress = _sse_terminal(sse_response.text)
                    assert sse_payload is not None, f"{scenario}: SSE produced no terminal event"
                    assert terminal_count == 1, f"{scenario}: SSE terminal event count={terminal_count}"
                    for item in progress:
                        assert "delivery_status" not in item, f"{scenario}: progress event looked terminal"
                        assert item.get("terminal") is False, f"{scenario}: progress event not marked non-terminal"
                    sse_terminal = _terminal_row(sse_payload)
                    sse_telemetry = _event_summary(collector.rows, sse_terminal["run_id"])
                    parity_fields = {
                        field: {"sync": sync_row.get(field), "sse": None, "equal": None}
                        for field in ("status", "code", "reason_codes", "claim_refs", "evidence_ids",
                                      "report_present", "persisted")
                    }
                    sse_row = {
                        "kind": "scenario",
                        "probe_run_id": probe_id,
                        "scenario": scenario,
                        "transport": "sse",
                        "status_code": sse_response.status_code,
                        "heartbeat_count": heartbeat_count,
                        "terminal_event_count": terminal_count,
                        "progress_event_count": len(progress),
                        **sse_terminal,
                        **_fallback_fields(sse_payload, sse_telemetry),
                        "finish_reason": sse_telemetry["finish_reason"],
                        "parse_status": sse_telemetry["parse_status"],
                        "collected_datum_count": sse_telemetry["collected_datum_count"],
                        "ledger_evidence_count": sse_telemetry["ledger_evidence_count"],
                        "selected_evidence_count": sse_telemetry["selected_evidence_count"],
                        "reasoning": sse_telemetry["reasoning"],
                        "cleanup_status": _LAST_CLEANUP["cleanup_status"],
                        "production_ready": False,
                    }
                    _assert_controlled("sse", scenario, sse_row)
                    for field, values in parity_fields.items():
                        values["sse"] = sse_row.get(field)
                        values["equal"] = values["sync"] == values["sse"]
                    sse_row["parity_fields"] = parity_fields
                    sse_row["parity_equal"] = {
                        "status": parity_fields["status"]["equal"],
                        "code": parity_fields["code"]["equal"],
                        "reason_codes": parity_fields["reason_codes"]["equal"],
                        "report_present": parity_fields["report_present"]["equal"],
                        "persisted": parity_fields["persisted"]["equal"],
                    }
                    scenario_rows.append(sse_row)

                    missing = [field for field in REQUIRED_FIELDS if field not in sse_row]
                    assert not missing, f"{scenario}: artifact row missing {missing}"
    finally:
        logger.removeHandler(collector)

    for row in scenario_rows:
        row["cleanup_status"] = _LAST_CLEANUP["cleanup_status"]
        _append(row)

    _append({
        "kind": "run_complete",
        "probe_run_id": probe_id,
        "artifact": str(ARTIFACT),
        "cleanup_status": _LAST_CLEANUP["cleanup_status"],
        "exception_type": _LAST_CLEANUP["exception_type"],
        "production_ready": False,
    })
    print(json.dumps({
        "probe_run_id": probe_id,
        "artifact": str(ARTIFACT),
        "cleanup_status": _LAST_CLEANUP["cleanup_status"],
        "production_ready": False,
    }))


if __name__ == "__main__":
    probe()
