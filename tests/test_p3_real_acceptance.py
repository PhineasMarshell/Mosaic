"""P3 real iiix/MCP acceptance replay checks."""

from __future__ import annotations

import json
from pathlib import Path

ARTIFACT = Path(__file__).parents[1] / "memory" / "p3-real-acceptance.jsonl"


def _rows() -> list[dict]:
    assert ARTIFACT.exists(), f"missing P3 artifact: {ARTIFACT}"
    return [json.loads(line) for line in ARTIFACT.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_p3_plugin_verify_spawn_and_list_tools_are_real_passes():
    dependency = next(row for row in _rows() if row.get("kind") == "dependency_check")
    assert dependency["plugin_status"] == "passed"
    assert dependency["plugin_verify"] == "passed"
    assert dependency["mosaic_spawn"] == "passed"
    assert dependency["mcp_list_tools"]["tool_count"] == 40
    assert dependency["mcp_health"]["status"] == "passed"


def test_p3_failed_calls_never_create_evidence():
    for row in _rows():
        if row.get("kind") == "mcp_call" and row.get("status") in {"error", "timeout"}:
            assert row["datum_count"] == 0
            assert row["evidence_created"] is False


def test_p3_required_scenarios_keep_terminal_and_policy_metadata():
    rows = [row for row in _rows() if row.get("kind") in {"scenario", "scenario_attempt"}]
    names = {row["scenario"] for row in rows}
    assert {"trading_day", "weekend_today", "real_unmapped_stock", "code_only", "partial_refresh_truncated", "gateway_deadline", "sse_real"} <= names
    deadline = next(row for row in rows if row["scenario"] == "gateway_deadline")
    assert deadline["tool"]["status"] == "error"
    assert deadline["tool"]["datum_count"] == 0
    sse = next(row for row in rows if row["scenario"] == "sse_real")
    assert sse["final_response"]["persisted"] is False
    weekend = next(row for row in rows if row["scenario"] == "weekend_today" and row.get("kind") == "scenario")
    assert weekend["response"]["market_closed"] is True
    assert weekend["response"]["as_of_date"] == "2026-10-09"


def test_p3_artifact_is_redacted():
    text = ARTIFACT.read_text(encoding="utf-8")
    for marker in ("sk-", "Bearer ", "Authorization:", "api_key", "iiix_int_"):
        assert marker not in text
