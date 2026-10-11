"""P4 redacted real Gateway/MCP acceptance replay checks."""

from __future__ import annotations

import json
from pathlib import Path

ARTIFACT = Path(__file__).parents[1] / "memory" / "p4-real-acceptance.jsonl"


def _rows() -> list[dict]:
    return [json.loads(line) for line in ARTIFACT.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_p4_artifact_is_redacted_and_has_real_mcp_smoke():
    text = ARTIFACT.read_text(encoding="utf-8")
    assert all(marker not in text for marker in ("sk-", "Bearer ", "Authorization:", "api_key", "iiix_int_"))
    dependency = next(row for row in _rows() if row.get("kind") == "dependency_check")
    assert dependency["mcp_spawn"] == "passed"
    assert dependency["mcp_list_tools"]["tool_count"] > 0
    assert dependency["verify_upstream"]["status"] == "passed"


def test_p4_failed_gateway_results_are_zero_datum_and_never_evidence():
    for row in _rows():
        tool = row.get("tool") or {}
        if tool.get("status") in {"error", "timeout"}:
            assert tool.get("datum_count") == 0
            assert tool.get("evidence_created") is False


def test_p4_required_scenarios_and_sse_terminal_contract():
    rows = _rows()
    names = {row.get("scenario") for row in rows if row.get("kind") in {"scenario", "scenario_attempt"}}
    assert {
        "trading_day", "weekend_today", "real_unmapped_stock", "code_only",
        "abnormal_reasons_success", "abnormal_reasons_timeout", "abnormal_reasons_empty",
        "partial_refresh", "gateway_deadline", "mcp_smoke", "sse_real",
    } <= names
    sse = next(row for row in rows if row.get("scenario") == "sse_real")
    final = sse["final_response"]
    assert final["delivery_status"] in {"verified", "degraded", "blocked", "failed"}
    if final["delivery_status"] != "verified":
        assert final["persisted"] is False
    assert final["run_id"] and final["reason_codes"]