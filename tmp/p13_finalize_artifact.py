"""Read-only on the source: copy the newest P13 probe run into a clean artifact."""

import json
import pathlib
import sys

SOURCE = pathlib.Path("memory/p13-focused-acceptance.jsonl")
TARGET = pathlib.Path("memory/p13-focused-acceptance-final.jsonl")


def main(probe: str | None = None):
    rows = [json.loads(line) for line in SOURCE.read_text(encoding="utf-8").splitlines() if line.strip()]
    if probe is None:
        probes = [row.get("probe_run_id") for row in rows if row.get("kind") == "scenario"]
        probe = probes[-1]
    kept = [row for row in rows if row.get("probe_run_id") == probe]
    TARGET.write_text(
        "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in kept),
        encoding="utf-8",
    )
    print(json.dumps({"probe_run_id": probe, "rows": len(kept), "target": str(TARGET)}))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
