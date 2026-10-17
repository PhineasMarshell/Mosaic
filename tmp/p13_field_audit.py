"""P13 diagnostic: audit the harness REQUIRED_FIELDS against the P13 spec JSONL field list."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "scripts" / "p13_focused_acceptance.py"

tree = ast.parse(SRC.read_text(encoding="utf-8"))
required: list[str] = []
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == "REQUIRED_FIELDS" for t in node.targets
    ):
        required = [elt.value for elt in node.value.elts if isinstance(elt, ast.Constant)]

spec = [
    "run_id",
    "status",
    "code",
    "reason_codes",
    "finish_reason",
    "parse_status",
    "candidate_count",
    "accepted_count",
    "rejection_reasons",
    "collected_datum_count",
    "ledger_evidence_count",
    "report_evidence_count",
    "claim_count",
    "heartbeat_count",
    "terminal_event_count",
    "persisted",
    "cleanup_status",
]
print(f"REQUIRED_FIELDS={len(required)}")
print(f"spec_missing={[name for name in spec if name not in required]}")
print(f"extra={[name for name in required if name not in spec]}")
