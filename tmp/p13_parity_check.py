"""Read-only: print parity_fields for the newest P13 run per scenario."""

import json
import sys


def main(path):
    rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    runs = [row.get("probe_run_id") for row in rows if row.get("kind") == "scenario"]
    newest = runs[-1]
    for row in rows:
        if row.get("probe_run_id") != newest or row.get("transport") != "sse":
            continue
        print("=" * 70)
        print("scenario:", row.get("scenario"), "heartbeat:", row.get("heartbeat_count"))
        for field, values in (row.get("parity_fields") or {}).items():
            print(f"  {field}: equal={values.get('equal')}")
            if not values.get("equal"):
                print("    sync:", json.dumps(values.get("sync"), ensure_ascii=False)[:400])
                print("    sse :", json.dumps(values.get("sse"), ensure_ascii=False)[:400])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "memory/p13-focused-acceptance.jsonl")
