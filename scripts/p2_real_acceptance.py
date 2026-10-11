"""Validate/replay the P2 real Gateway/LLM acceptance artifact.

The live run is kept as an explicit workspace operation so it never changes
``.env`` or the runtime SQLite database; use ``--replay`` for a fast, offline
schema check of the saved JSONL.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ARTIFACT = Path(__file__).parents[1] / "memory" / "p2-real-acceptance.jsonl"


def replay() -> int:
    if not ARTIFACT.exists():
        raise SystemExit(f"missing acceptance artifact: {ARTIFACT}")
    rows = [json.loads(line) for line in ARTIFACT.read_text(encoding="utf-8").splitlines() if line.strip()]
    scenarios = {row.get("scenario") for row in rows if row.get("kind") == "scenario"}
    required = {
        "trading_day",
        "weekend_today",
        "real_unmapped_stock",
        "code_only",
        "name_match",
        "partial_refresh_truncated",
        "gateway_deadline",
        "sse_real",
    }
    missing = sorted(required - scenarios)
    if missing:
        raise SystemExit(f"acceptance artifact missing scenarios: {missing}")
    if any(marker in ARTIFACT.read_text(encoding="utf-8") for marker in ("sk-", "Bearer ", "Authorization:")):
        raise SystemExit("acceptance artifact contains a credential marker")
    print(json.dumps({"artifact": str(ARTIFACT), "rows": len(rows), "scenarios": sorted(scenarios)}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay", action="store_true", help="validate the saved redacted JSONL")
    args = parser.parse_args()
    if args.replay:
        return replay()
    raise SystemExit(
        "Live runs are intentionally performed from the acceptance workspace with the configured real dependencies; "
        "use --replay to validate the saved artifact."
    )


if __name__ == "__main__":
    raise SystemExit(main())
