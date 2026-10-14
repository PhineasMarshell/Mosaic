"""Offline P15 audit replay; writes only a new temporary DB and append-only JSONL."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent.persistence import persist_research
from app.graph.nodes.finalize import FinalizeAuditNode
from app.memory.storage import MarketMemory
from app.models.evidence import Evidence
from app.models.response import MarketIntelligence, ResearchResponse, build_response_from_state

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "memory" / "p15-acceptance.jsonl"
FACT = "贵州茅台(600519)在2026-10-09收盘价为1488元"


def _state(claims: list[dict], evidence: list[Evidence], **extra) -> dict:
    return {
        "question": "SH600519 2026-10-09收盘价", "domain": "a_share",
        "requested_date": "2026-10-09", "planned_as_of_date": "2026-10-09", "as_of_date": "2026-10-09",
        "market_closed": False, "critique": {"verdict": "pass"}, "errors": [], "results": [],
        "evidence": evidence,
        "report": MarketIntelligence(
            requested_date=extra.get("requested_date", "2026-10-09"),
            as_of_date=extra.get("as_of_date", "2026-10-09"), market_closed=extra.get("market_closed", False),
            what_happened="；".join(c["claim"] for c in claims),
            claims=claims, evidence=[item.model_dump() for item in evidence],
        ), **extra,
    }


async def main() -> None:
    probe_run_id = f"p15-{time.time_ns():x}"
    work = ROOT / ".tmp" / f"p15-acceptance-{uuid.uuid4().hex}"
    work.mkdir(parents=True)
    evidence = Evidence(
        id="p15-quote-001", source_tool="internal_iwencai_market_query", domain="a_share", metric="close",
        value=1488.0, instrument="SH600519", instrument_name="贵州茅台", as_of_date="2026-10-09",
        retrieved_at="2026-10-14T01:00:00Z", source="同花顺问财", authority="同花顺问财",
        completeness="complete", coverage="instrument",
    )
    good = {"claim": FACT, "claim_type": "fact", "evidence_ids": [evidence.id]}
    missing = {"claim": "贵州茅台收盘价为1488元", "claim_type": "fact", "evidence_ids": []}
    cases = [
        ("no_evidence_ids", _state([missing], []), "blocked", False),
        ("mixed_claims", _state([good, missing], [evidence]), "degraded", False),
        ("closed_day", _state([
            {"claim": "请求日 2026-10-10 A股休市", "claim_type": "structure", "evidence_ids": []},
            {"claim": "最近交易日 2026-10-09 行情无法确认", "claim_type": "structure", "evidence_ids": []},
        ], [], requested_date="2026-10-10", market_closed=True), "verified", True),
        ("complete_quote", _state([good], [evidence]), "verified", True),
        ("forged_and_missing", _state([
            good, missing, {"claim": "贵州茅台发布公告", "evidence_ids": ["forged-001"]},
        ], [evidence]), "blocked", False),
        ("independent_tool_failure", _state([good], [evidence], results=[
            {"status": "error", "reason_codes": ["gateway_timeout"]},
        ]), "degraded", False),
    ]
    rows: list[dict] = []
    for scenario, state, expected_delivery, expected_persisted in cases:
        run_id = f"{probe_run_id}-{scenario}"
        state["run_id"] = run_id
        out = await FinalizeAuditNode(None)(state)
        assert not out.get("errors"), (scenario, out)
        result = build_response_from_state({**state, **out}, state["question"], scenario)
        memory = MarketMemory(db_path=work / f"{scenario}.db")
        try:
            persisted = await persist_research(result, result.question, scenario, memory=memory)
            counts = {table: memory.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("research_records", "conversations", "daily_states")}
            assert result.delivery_status == expected_delivery, (scenario, result.delivery_status)
            assert persisted is expected_persisted, (scenario, persisted)
            assert set(counts.values()) == {int(expected_persisted)}, (scenario, counts)
            if "missing" in scenario or scenario in {"no_evidence_ids", "mixed_claims"}:
                assert "missing_reference" in result.reason_codes, scenario
            if scenario == "mixed_claims":
                assert [claim.evidence_ids for claim in result.report.claims] == [[evidence.id]]
            rows.append({
                "probe_run_id": probe_run_id, "run_id": run_id, "scenario": scenario,
                "mode": "offline_replay", "critic_verdict": "pass", "final_audit_status": result.final_audit_status,
                "delivery_status": result.delivery_status, "reason_codes": result.reason_codes,
                "persisted": persisted, "persistence_counts": counts,
                "ledger_evidence_count": len(state["evidence"]),
                "report_evidence_count": len(result.report.evidence) if result.report else 0,
                "claim_count": len(result.report.claims) if result.report else 0,
                "production_ready": False,
            })
        finally:
            memory.close()

    report = _state([missing], [])["report"]
    forged_verified = ResearchResponse(
        question="贵州茅台收盘价", report=report, critique={"verdict": "pass"},
        delivery_status="verified", final_audit_status="pass", run_id=f"{probe_run_id}-persistence_defense",
    )
    memory = MarketMemory(db_path=work / "persistence_defense.db")
    try:
        persisted = await persist_research(forged_verified, forged_verified.question, "p15", memory=memory)
        counts = {table: memory.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("research_records", "conversations", "daily_states")}
        assert persisted is False and set(counts.values()) == {0}
        assert "missing_reference" in forged_verified.reason_codes
        rows.append({"probe_run_id": probe_run_id, "run_id": forged_verified.run_id,
                     "scenario": "persistence_defense", "mode": "offline_replay", "persisted": False,
                     "reason_codes": forged_verified.reason_codes, "persistence_counts": counts,
                     "production_ready": False})
    finally:
        memory.close()

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    with ARTIFACT.open("a", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        stream.write(json.dumps({"kind": "run_complete", "probe_run_id": probe_run_id,
                                 "scenario_count": len(rows), "production_ready": False}) + "\n")
    print(json.dumps({"probe_run_id": probe_run_id, "scenario_count": len(rows), "artifact": str(ARTIFACT)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--mode", choices=("replay",), required=True)
    parser.add_argument("--artifact", type=Path, default=ARTIFACT)
    args = parser.parse_args()
    ARTIFACT = args.artifact
    from scripts.p16_acceptance import offline

    # Windows creates an internal loopback socketpair when constructing its
    # event loop. Build that local machinery before denying service access.
    with asyncio.Runner() as runner:
        with offline():
            runner.run(main())
