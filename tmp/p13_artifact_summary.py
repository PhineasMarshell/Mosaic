"""Read-only: summarise the newest P13 focused-acceptance run's rows."""

import json
import sys

FIELDS = [
    "scenario", "transport", "status_code", "run_id", "status", "code",
    "delivery_status", "final_audit_status", "reason_codes", "persisted",
    "report_present", "claim_count", "report_evidence_count", "evidence_ids",
    "fallback_applied", "fallback_enabled", "fallback_reason",
    "candidate_count", "accepted_count", "rejected_count", "rejection_reasons",
    "finish_reason", "parse_status", "collected_datum_count",
    "ledger_evidence_count", "selected_evidence_count", "heartbeat_count",
    "terminal_event_count", "progress_event_count", "cleanup_status",
    "parity_equal",
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
                print(f"  {field}: {json.dumps(row[field], ensure_ascii=False)}")
    complete = [row for row in rows if row.get("kind") == "run_complete"][-1]
    print("-" * 70)
    print("run_complete:", json.dumps(complete, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "memory/p13-focused-acceptance.jsonl")
