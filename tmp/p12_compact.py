"""P13 diagnostic: print compact fields of the newest P12 probe rows (no raw dumps)."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "memory" / "p12-focused-acceptance.jsonl"

rows = []
for line in ARTIFACT.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        rows.append(json.loads(line))
    except json.JSONDecodeError:
        continue

scenarios = [row for row in rows if row.get("kind") == "scenario"]
if not scenarios:
    raise SystemExit("no scenario rows")
newest = scenarios[-1].get("probe_run_id")
mine = [row for row in scenarios if row.get("probe_run_id") == newest]

print(f"artifact={ARTIFACT.name} total_rows={len(rows)} scenario_rows={len(scenarios)} newest_probe={newest}")
for row in mine:
    codes = row.get("reason_codes") or []
    print(
        f"- {row.get('scenario')}/{row.get('transport')} run_id={row.get('run_id')} "
        f"status={row.get('delivery_status')} code={row.get('code')} audit={row.get('final_audit_status')} "
        f"persisted={row.get('persisted')} report={row.get('report_present')} claims={row.get('claim_count')} "
        f"report_evidence={row.get('report_evidence_count')} hb={row.get('heartbeat_count')} "
        f"terminal={row.get('terminal_event_count')} cleanup={row.get('cleanup_status')}"
    )
    print(f"    reason_codes={codes}")

parity = [row for row in mine if row.get("parity_fields")]
for row in parity:
    fields = row["parity_fields"]
    unequal = []
    for key, value in fields.items():
        if not isinstance(value, dict):
            continue
        sync_value = value.get("sync")
        sse_value = value.get("sse")
        unequal.append((key, sync_value == sse_value))
    same = [key for key, ok in unequal if ok]
    diff = [key for key, ok in unequal if not ok]
    print(f"parity {row.get('scenario')}: equal={same}")
    print(f"parity {row.get('scenario')}: differ={diff}")
print(f"run_complete={json.dumps(rows[-1], ensure_ascii=False)}")
