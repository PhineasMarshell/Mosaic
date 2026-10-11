"""Replay and validate the P3 real iiix/MCP acceptance evidence.

The live commands are deliberately run outside this replay helper so an
offline test cannot turn an unavailable plugin, gateway, or model into a
verified result.  This command validates the redacted JSONL contract only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ARTIFACT = Path(__file__).parents[1] / "memory" / "p3-real-acceptance.jsonl"


def load_rows() -> list[dict]:
    if not ARTIFACT.exists():
        raise SystemExit(f"missing acceptance artifact: {ARTIFACT}")
    return [json.loads(line) for line in ARTIFACT.read_text(encoding="utf-8").splitlines() if line.strip()]


def replay() -> int:
    rows = load_rows()
    text = ARTIFACT.read_text(encoding="utf-8")
    for marker in ("sk-", "Bearer ", "Authorization:", "api_key", "iiix_int_"):
        if marker in text:
            raise SystemExit(f"acceptance artifact contains credential marker: {marker}")

    dependency = next((row for row in rows if row.get("kind") == "dependency_check"), None)
    if dependency is None:
        raise SystemExit("missing dependency_check")
    required = {
        "plugin_status": "passed",
        "plugin_verify": "passed",
        "mosaic_spawn": "passed",
    }
    for key, expected in required.items():
        if dependency.get(key) != expected:
            raise SystemExit(f"{key}={dependency.get(key)!r}, expected {expected!r}")
    if dependency.get("mcp_list_tools", {}).get("tool_count", 0) <= 0:
        raise SystemExit("MCP list_tools returned no tools")
    if dependency.get("mcp_health", {}).get("status") != "passed":
        raise SystemExit("MCP health/verify_upstream did not pass")

    scenario_rows = [row for row in rows if row.get("kind") in {"scenario", "scenario_attempt"}]
    names = {row.get("scenario") for row in scenario_rows}
    required_scenarios = {
        "trading_day",
        "weekend_today",
        "real_unmapped_stock",
        "code_only",
        "partial_refresh_truncated",
        "gateway_deadline",
        "sse_real",
    }
    missing = sorted(required_scenarios - names)
    if missing:
        raise SystemExit(f"missing scenarios: {missing}")

    for row in rows:
        if row.get("kind") == "mcp_call" and row.get("status") in {"error", "timeout"}:
            if row.get("datum_count") != 0 or row.get("evidence_created"):
                raise SystemExit(f"failed MCP call produced evidence: {row.get('scenario')}")

    deadline = next(row for row in rows if row.get("scenario") == "gateway_deadline")
    deadline_tool = deadline.get("tool", {})
    if deadline_tool.get("status") != "error" or deadline_tool.get("datum_count") != 0:
        raise SystemExit("gateway deadline must be error with zero datum")

    sse = next(row for row in rows if row.get("scenario") == "sse_real")
    final_response = sse.get("final_response", {})
    if final_response.get("persisted") is not False:
        raise SystemExit("failed/timeout SSE must have persisted=false")

    print(json.dumps({
        "artifact": str(ARTIFACT),
        "rows": len(rows),
        "scenarios": sorted(names),
        "mcp": {"plugin_verify": "passed", "spawn": "passed", "tool_count": dependency["mcp_list_tools"]["tool_count"]},
    }, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay", action="store_true", help="validate the saved redacted JSONL")
    args = parser.parse_args()
    if not args.replay:
        raise SystemExit("live P3 runs are explicit workspace operations; use --replay for schema validation")
    return replay()


if __name__ == "__main__":
    raise SystemExit(main())
