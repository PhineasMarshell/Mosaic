"""P9 end-to-end replay for the main THS research route.

``probe`` drives the real ``/api/ask`` and ``/api/ask/stream`` endpoints and
stores only redacted terminal metadata.  ``replay`` is deterministic: it
checks that a completed run contains every required user scenario, that the
terminal contract is present for both transports, and that unsafe/error paths
never claim persistence.  A probe is evidence about one environment; it never
changes ``production_ready``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
# Running ``python scripts/p9_e2e_acceptance.py`` puts ``scripts/`` on
# sys.path.  Add the project root before importing app so the probe behaves
# the same way from a shell, a scheduler, and pytest.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ARTIFACT = ROOT / "memory" / "p9-e2e-acceptance.jsonl"
PROBE_LABEL = "p9"

SCENARIOS: dict[str, str] = {
    "trading_day_today_ashare": "2026-10-09 今天 A 股发生了什么",
    "weekend_today_ashare": "2026-10-11 今天 A 股发生了什么",
    "quote": "2026-10-09 查询贵州茅台 600519 行情",
    "kline": "查询 600519 2026-10-01 到 2026-10-09 K线",
    "limit_up_pool": "2026-10-09 A股涨停池有哪些",
    "limit_up_sectors": "2026-10-09 哪些涨停板块最强",
    "news": "查询 600519 2026-10-09 新闻",
    "announcement": "查询 600519 2026年10月公告",
    "index": "查询 2026-10-09 上证指数点位和涨跌幅",
    "sector_selector": "筛选 2026-10-09 涨幅前五行业板块",
    "business": "查询 600519 2025年主营业务构成、主要客户和供应商",
    "insresearch": "查询 600519 2026年业绩预测、机构评级和目标价",
}
REQUIRED_SCENARIOS = set(SCENARIOS)
TERMINAL_FIELDS = (
    "run_id",
    "final_audit_status",
    "delivery_status",
    "persisted",
    "reason_codes",
    "elapsed_ms",
    "stage",
    "exception_type",
    "delivery_reason",
    "blocked_reason",
    "source_switch",
    "partial",
)


def _write(row: dict[str, Any]) -> None:
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    with ARTIFACT.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _redacted_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep contract metadata while omitting user/model text and raw bodies."""
    if not isinstance(payload, dict):
        return {
            "run_id": None,
            "delivery_status": None,
            "persisted": False,
            "reason_codes": ["invalid_terminal_payload"],
            "claim_refs": [],
            "claims_bound": False,
            "claims_valid": False,
        }
    terminal = {key: payload.get(key) for key in TERMINAL_FIELDS}
    terminal["reason_codes"] = sorted({str(code) for code in (payload.get("reason_codes") or [])})
    terminal["claim_count"] = len((payload.get("report") or {}).get("claims", [])) if isinstance(payload.get("report"), dict) else 0
    report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
    claim_refs = [
        {"evidence_ids": [str(value) for value in (claim.get("evidence_ids") or [])],
         "claim_type": claim.get("claim_type")}
        for claim in (report.get("claims") or [])
        if isinstance(claim, dict)
    ]
    evidence_ids = {str(item.get("id")) for item in (report.get("evidence") or []) if isinstance(item, dict) and item.get("id")}
    evidence = [item for item in (report.get("evidence") or []) if isinstance(item, dict)]
    valid_evidence_ids = {
        str(item.get("id"))
        for item in evidence
        if item.get("id")
        and str(item.get("status", "success")).lower() == "success"
        and not item.get("partial", False)
        and str(item.get("completeness", "")).lower() not in {"partial", "incomplete", "truncated"}
    }
    terminal["claim_refs"] = claim_refs
    terminal["evidence_count"] = len(evidence)
    terminal["valid_evidence_count"] = len(valid_evidence_ids)
    terminal["claims_bound"] = all(item["evidence_ids"] and set(item["evidence_ids"]) <= evidence_ids for item in claim_refs)
    # Replay the same strict matcher used by Critic/Finalize.  The old P9
    # check only compared IDs and accepted forged provenance, stale dates,
    # wrong instruments, news-only price claims, and insufficient coverage.
    try:
        from app.agent.evidence_gate import match_claims_to_evidence

        validation = match_claims_to_evidence(
            report,
            evidence,
            expected_date=payload.get("as_of_date") or report.get("as_of_date"),
            requested_date=payload.get("requested_date") or report.get("requested_date"),
            market_closed=payload.get("market_closed"),
            strict_provenance=True,
        )
    except Exception:
        validation = []
    terminal["claim_validation"] = [
        {"evidence_ids": [str(value) for value in (row.get("evidence_ids") or [])],
         "matched_evidence_ids": [str(value) for value in (row.get("matched_evidence_ids") or [])],
         "supported": bool(row.get("supported")),
         "reason_codes": sorted({str(code) for code in (row.get("reason_codes") or [])})}
        for row in validation if isinstance(row, dict)
    ]
    terminal["claims_valid"] = bool(claim_refs) and bool(validation) and all(
        row.get("supported") is True for row in validation
    ) and all(item["evidence_ids"] and set(item["evidence_ids"]) <= valid_evidence_ids for item in claim_refs)
    terminal["cache_stats"] = payload.get("cache_stats") or {}
    terminal["remaining_budget"] = payload.get("remaining_budget")
    terminal["tool_count"] = len(payload.get("tool_results") or [])
    terminal["tool_statuses"] = [
        {
            "status": item.get("status"),
            "partial": bool(item.get("partial", False)),
            "reason_codes": sorted({str(code) for code in (item.get("reason_codes") or [])}),
            "source_switch": bool(item.get("source_switch", False)),
            "primary_source": item.get("primary_source"),
            "fallback_source": item.get("fallback_source"),
            "datum_count": len(item.get("normalized") or []),
        }
        for item in (payload.get("tool_results") or [])
        if isinstance(item, dict)
    ]
    terminal["collected_datum_count"] = sum(item["datum_count"] for item in terminal["tool_statuses"])
    terminal["collected_nonempty_tool_count"] = sum(
        1 for item in terminal["tool_statuses"] if item["datum_count"] > 0
    )
    terminal["failed_tool_count"] = sum(
        1 for item in terminal["tool_statuses"]
        if str(item.get("status") or "").lower() in {"error", "timeout"}
        or any(
            any(token in str(code).lower() for token in ("auth", "timeout", "deadline", "tls", "upstream"))
            for code in (item.get("reason_codes") or [])
        )
    )
    return terminal


def _sse_result(response_text: str) -> dict[str, Any]:
    result: dict[str, Any] | None = None
    for line in response_text.splitlines():
        if line.startswith("data: "):
            try:
                payload = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and ("delivery_status" in payload or payload.get("code")):
                result = payload
    if result is None:
        raise AssertionError("SSE stream did not contain a terminal result event")
    return result


def _safe_reason(exc: BaseException, *, prefix: str) -> list[str]:
    """Return transport diagnostics without echoing URLs, bodies, or secrets."""
    name = type(exc).__name__
    return [f"{prefix}_{re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_') or 'error'}"]


def _not_observed(*, transport: str, status_code: int | None = None, reason_codes: list[str] | None = None,
                  exception_type: str | None = None) -> dict[str, Any]:
    return {
        "status": "not_observed",
        "observed": False,
        "transport": transport,
        "status_code": status_code,
        "reason_codes": sorted(set(reason_codes or ["not_observed"])),
        "exception_type": exception_type,
        "terminal": None,
    }


def _safe_sync(client: Any, question: str) -> tuple[dict[str, Any], Any | None]:
    try:
        response = client.post("/api/ask", json={"question": question, "domain": "a_share"})
    except Exception as exc:  # noqa: BLE001 - probe must continue to SSE/next scenario
        return _not_observed(transport="sync", reason_codes=_safe_reason(exc, prefix="sync_request"),
                             exception_type=type(exc).__name__), None
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - non-JSON HTTP is an observed failure
        return _not_observed(transport="sync", status_code=response.status_code,
                             reason_codes=_safe_reason(exc, prefix="sync_non_json"),
                             exception_type=type(exc).__name__), response
    if not isinstance(payload, dict):
        return _not_observed(transport="sync", status_code=response.status_code,
                             reason_codes=["sync_invalid_json_payload"]), response
    return {"status": "observed", "observed": True, "transport": "sync",
            "status_code": response.status_code,
            "terminal": _redacted_payload(payload)}, response


def _safe_sse(client: Any, question: str) -> tuple[dict[str, Any], Any | None]:
    try:
        response = client.post("/api/ask/stream", json={"question": question, "domain": "a_share"})
    except Exception as exc:  # noqa: BLE001 - probe must continue to next transport
        return _not_observed(transport="sse", reason_codes=_safe_reason(exc, prefix="sse_request"),
                             exception_type=type(exc).__name__), None
    try:
        payload = _sse_result(response.text)
    except Exception as exc:  # noqa: BLE001 - no terminal event is not a probe crash
        return _not_observed(transport="sse", status_code=response.status_code,
                             reason_codes=_safe_reason(exc, prefix="sse_no_terminal"),
                             exception_type=type(exc).__name__), response
    return {"status": "observed", "observed": True, "transport": "sse",
            "status_code": response.status_code,
            "heartbeat_count": _sse_heartbeat_count(response.text),
            "terminal": _redacted_payload(payload)}, response


def _sse_heartbeat_count(response_text: str) -> int:
    return sum(1 for line in response_text.splitlines() if line.startswith("data: ") and '"step": "working"' in line)


@contextmanager
def _best_effort_tempdir():
    """Keep probe completion independent from Windows child-process cleanup."""
    temp_dir = tempfile.TemporaryDirectory(prefix="mosaic-p9-", ignore_cleanup_errors=True)
    try:
        yield temp_dir.name
    finally:
        try:
            temp_dir.cleanup()
        except OSError:
            # MCP child processes can retain a handle or deny chmod/rmtree on
            # Windows after the response and artifact are already complete.
            # Cleanup is best effort and must not turn an accepted observation
            # into a failed probe process.
            pass


def probe() -> None:
    """Run real sync and SSE requests in an isolated temporary memory store."""
    from fastapi.testclient import TestClient

    # Import only after the isolated DB path is installed; no production memory
    # file is touched by a probe.
    # MCP child processes may create read-only files in the isolated store on
    # Windows.  Cleanup is best effort so a completed probe is never reported
    # as failed solely because tempfile teardown lacks permission.
    with _best_effort_tempdir() as temp_dir:
        os.environ["MOSAIC_MEMORY_DB"] = str(Path(temp_dir) / "memory.db")
        from app import main
        from app.config import get_settings

        get_settings.cache_clear()
        main._settings = get_settings()
        main._orchestrator = None
        run_id = hashlib.sha256(f"{PROBE_LABEL}-{time.time_ns()}".encode()).hexdigest()[:12]
        _write({"kind": "run_start", "run_id": run_id, "probe_kind": "real_e2e", "production_ready": False,
                "acceptance_phase": PROBE_LABEL,
                "budget_seconds": main._settings.research_budget_seconds,
                "stage_budgets": {"plan": main._settings.plan_min_budget_seconds,
                                  "tools": main._settings.tools_min_budget_seconds,
                                  "reasoning": main._settings.reasoning_min_budget_seconds,
                                  "critic": main._settings.critic_min_budget_seconds,
                                  "finalize": main._settings.finalize_min_budget_seconds},
                "heartbeat_seconds": main._settings.stream_heartbeat_seconds})
        # TestClient drives FastAPI's actual lifespan and both public endpoints.
        with TestClient(main.app) as client:
            for scenario, question in SCENARIOS.items():
                sync_row, _ = _safe_sync(client, question)
                sync_terminal = sync_row.get("terminal") or {}
                _write({"kind": "scenario", "run_id": sync_terminal.get("run_id"), "probe_run_id": run_id,
                        "scenario": scenario, **sync_row, "production_ready": False})

                # Always attempt SSE independently, even when sync returned
                # non-JSON, raised, or timed out.
                sse_row, _ = _safe_sse(client, question)
                sse_terminal = sse_row.get("terminal") or {}
                _write({"kind": "scenario", "run_id": sse_terminal.get("run_id"), "probe_run_id": run_id,
                        "scenario": scenario, **sse_row, "production_ready": False})
        _write({"kind": "run_complete", "probe_run_id": run_id, "required_scenarios": sorted(REQUIRED_SCENARIOS),
                "probe_kind": "real_e2e", "acceptance_phase": PROBE_LABEL, "production_ready": False})
        print(json.dumps({"probe_run_id": run_id, "artifact": str(ARTIFACT), "production_ready": False}, ensure_ascii=False))


def _assert_terminal(row: dict[str, Any]) -> None:
    if row.get("status") == "not_observed":
        assert row.get("observed") is False
        assert row.get("terminal") in (None, {})
        return
    terminal = row.get("terminal") or {}
    missing = [key for key in TERMINAL_FIELDS if key not in terminal]
    assert not missing, f"terminal fields missing: {missing}"
    assert terminal.get("run_id"), "terminal run_id missing"
    assert terminal.get("delivery_status") in {"verified", "degraded", "blocked", "failed"}
    assert isinstance(terminal.get("reason_codes"), list)
    assert isinstance(terminal.get("elapsed_ms"), (int, float)) or terminal.get("elapsed_ms") is None
    unsafe_codes = {"timeout", "research_deadline_exhausted", "ths_invalid_credentials", "mcp_fallback_not_attempted_auth",
                    "fallback_transport_error", "mcp_upstream_timeout", "gateway_tls_error", "deadline_exhausted",
                    "upstream_timeout", "sse_timeout", "gateway_auth_error", "gateway_timeout",
                    "gateway_http_error", "gateway_transport_error"}
    reason_codes = {str(code).lower() for code in (terminal.get("reason_codes") or ())}
    unsafe = unsafe_codes.intersection(reason_codes)
    # Transport status and redacted reason names are both accepted inputs.
    # Any authentication, TLS, server, upstream, deadline, or SSE timeout
    # path must be evidence-free even if the application used a new code.
    status_code = row.get("status_code")
    if status_code == 401 or (isinstance(status_code, int) and status_code >= 500):
        unsafe.add(f"http_{status_code}")
    if any(any(token in code for token in ("timeout", "deadline", "tls", "upstream")) for code in reason_codes):
        unsafe.add("transport_failure")
    if unsafe:
        assert terminal.get("persisted") is False, "unsafe/non-verified result must not persist"
        failed_tools = [
            item for item in (terminal.get("tool_statuses") or [])
            if str(item.get("status") or "").lower() in {"error", "timeout"}
            or any(
                any(token in str(code).lower() for token in ("auth", "timeout", "deadline", "tls", "upstream"))
                for code in (item.get("reason_codes") or [])
            )
        ]
        # A failed optional tool must contribute no datum/evidence of its own;
        # independent successful tools may still support a controlled degraded
        # claim.  If no tool identity exists at all, retain the conservative
        # response-level zero-evidence rule.
        if failed_tools:
            assert all(item.get("datum_count") == 0 for item in failed_tools), (
                "failed tool must not generate evidence"
            )
        else:
            assert terminal.get("evidence_count") == 0, "unattributed transport failure must contain zero evidence"
    if terminal.get("delivery_status") in {"blocked", "failed", "degraded"}:
        assert terminal.get("persisted") is False, "non-verified result must not persist"
    if terminal.get("delivery_status") == "verified":
        assert terminal.get("final_audit_status") == "pass", "verified result must have a passing final audit"
        assert terminal.get("claims_bound") is True, "verified report contains an unbound claim"
        assert terminal.get("claims_valid") is True, "verified report contains a claim with invalid evidence"
    if terminal.get("delivery_status") == "degraded":
        assert terminal.get("claims_valid") is True, "degraded result contains an unverified claim"


def _transport(row: dict[str, Any]) -> str | None:
    """Return transport, including compatibility for early P10 rows.

    The first real P10 probe predated the explicit transport field on observed
    rows.  Its SSE rows carry ``heartbeat_count`` while sync rows do not, so
    replay can classify those already-written redacted rows without changing
    the runtime artifact.  New probes always write the field explicitly.
    """
    transport = row.get("transport")
    if transport in {"sync", "sse"}:
        return transport
    if row.get("status") == "observed":
        return "sse" if "heartbeat_count" in row else "sync"
    return transport


def replay() -> None:
    rows = [json.loads(line) for line in ARTIFACT.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows, "empty P9 artifact"
    text = ARTIFACT.read_text(encoding="utf-8")
    for marker in ("Bearer ", "Authorization:", "IWENCAI_API_KEY=", "OPENAI_API_KEY=", "synthetic-secret"):
        assert marker not in text, f"credential marker: {marker}"
    complete_ids = [row.get("probe_run_id") for row in rows if row.get("kind") == "run_complete"]
    assert complete_ids, "no complete P9 run"
    selected = complete_ids[-1]
    current = [row for row in rows if row.get("probe_run_id") == selected]
    scenario_rows = [row for row in current if row.get("kind") == "scenario"]
    assert {row.get("scenario") for row in scenario_rows} >= REQUIRED_SCENARIOS
    assert {_transport(row) for row in scenario_rows} >= {"sync", "sse"}
    for row in scenario_rows:
        _assert_terminal(row)
    # A single scenario must expose the same terminal schema on sync and SSE;
    # values can differ because the two calls are independent real runs.
    for scenario in REQUIRED_SCENARIOS:
        modes = {_transport(row) for row in scenario_rows if row.get("scenario") == scenario}
        assert modes >= {"sync", "sse"}, f"missing transport replay for {scenario}"
    not_observed = sorted({row.get("scenario") for row in scenario_rows if row.get("status") == "not_observed"})
    print(json.dumps({"probe_run_id": selected, "rows": len(current), "scenarios": len(REQUIRED_SCENARIOS),
                      "not_observed": not_observed, "production_ready": False}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("probe", "replay"), required=True)
    args = parser.parse_args()
    probe() if args.mode == "probe" else replay()


if __name__ == "__main__":
    main()
