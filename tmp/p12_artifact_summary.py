"""Read-only: summarise the newest P12 focused-acceptance run's rows."""

import json
import sys

FIELDS = [
    "scenario", "transport", "status_code", "run_id", "delivery_status",
    "final_audit_status", "reason_codes", "persisted", "blocked_reason",
    "claim_count", "evidence_count", "collected_datum_count",
    "ledger_evidence_count", "report_evidence_count", "finish_reason",
    "parse_status", "candidate_count", "accepted_count", "rejected_count",
    "rejection_reasons", "heartbeat_count", "terminal_event_count",
    "parity_fields", "elapsed_ms", "cleanup_status",
]


def main(path):
    rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    runs = [row.get("probe_run_id") for row in rows if row.get("kind") == "scenario"]
    newest = runs[-1]
    print(f"artifact rows={len(rows)} newest_probe={newest}")
    for row in rows:
        if row.get("probe_run_id") != newest:
            continue
        print("-" * 70)
        for field in FIELDS:
            if field in row:
                print(f"  {field}: {json.dumps(row[field], ensure_ascii=False)[:600]}")
    complete = [row for row in rows if row.get("kind") == "run_complete"]
    if complete:
        print("run_complete:", json.dumps(complete[-1], ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "memory/p12-focused-acceptance.jsonl")
