"""Replay the redacted P4 Gateway/budget/SSE acceptance evidence.

Live runs are intentionally explicit workspace operations.  ``--replay`` only
checks the saved JSONL contract and never turns an unavailable Gateway/LLM into
success.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ARTIFACT = Path(__file__).parents[1] / "memory" / "p4-real-acceptance.jsonl"


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

    names = {row.get("scenario") for row in rows if row.get("kind") in {"scenario", "scenario_attempt"}}
    required = {
        "trading_day", "weekend_today", "real_unmapped_stock", "code_only",
        "abnormal_reasons_success", "abnormal_reasons_timeout", "abnormal_reasons_empty",
        "partial_refresh", "gateway_deadline", "mcp_smoke", "sse_real",
    }
    missing = sorted(required - names)
    if missing:
        raise SystemExit(f"acceptance artifact missing scenarios: {missing}")

    for row in rows:
        tool = row.get("tool") or {}
        if tool.get("status") in {"error", "timeout"}:
            if tool.get("datum_count") != 0 or tool.get("evidence_created"):
                raise SystemExit(f"failed tool produced evidence: {row.get('scenario')}")
        if row.get("kind") == "scenario" and row.get("scenario") == "sse_real":
            final = row.get("final_response") or {}
            if final.get("delivery_status") not in {"verified", "degraded", "blocked", "failed"}:
                raise SystemExit("SSE final response has no explicit terminal status")
            if final.get("persisted") is not False and final.get("delivery_status") != "verified":
                raise SystemExit("non-verified SSE result must have persisted=false")

    print(json.dumps({"artifact": str(ARTIFACT), "rows": len(rows), "scenarios": sorted(names)}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay", action="store_true")
    args = parser.parse_args()
    if not args.replay:
        raise SystemExit("Run live P4 scenarios explicitly, then use --replay to validate the redacted artifact.")
    return replay()


if __name__ == "__main__":
    raise SystemExit(main())