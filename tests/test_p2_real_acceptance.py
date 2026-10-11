"""P2 real-chain smoke/replay checks.

The JSONL fixture is produced by ``scripts/p2_real_acceptance.py`` (or the
same commands used for this acceptance).  These tests deliberately assert the
observed transport/audit contract without turning an unavailable dependency
into a pass.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.agent.evidence_gate import match_claims_to_evidence, run_evidence_gate
from app.graph.nodes.finalize import build_degraded_report
from app.models.evidence import Evidence
from app.models.market import NormalizedDatum, ToolResult
from app.models.response import ClaimEvidence, EvidenceItem, MarketIntelligence
from app.research.entity_check import assess_report_entities

ARTIFACT = Path(__file__).parents[1] / "memory" / "p2-real-acceptance.jsonl"


def _rows() -> list[dict]:
    assert ARTIFACT.exists(), f"missing live acceptance artifact: {ARTIFACT}"
    return [json.loads(line) for line in ARTIFACT.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_live_replay_scenarios_are_correlated_and_redacted():
    rows = _rows()
    scenarios = {row["scenario"]: row for row in rows if row.get("kind") == "scenario"}
    expected = {
        "trading_day",
        "weekend_today",
        "real_unmapped_stock",
        "code_only",
        "name_match",
        "partial_refresh_truncated",
        "gateway_deadline",
        "sse_real",
    }
    assert expected <= scenarios.keys()
    assert all(scenarios[name].get("run_id") for name in expected)

    # Run log events and scenario summaries must stay on the same run_id and
    # contain no credentials or raw free-text request payloads.
    run_ids = {row["run_id"] for row in rows if row.get("kind") in {"run_log", "scenario"} and row.get("run_id")}
    assert len(run_ids) >= len(expected)
    serialized = ARTIFACT.read_text(encoding="utf-8")
    for secret_marker in ("sk-", "api_key", "Authorization:", "Bearer "):
        assert secret_marker not in serialized


def test_live_replay_preserves_blocking_and_transport_failures():
    scenarios = {row["scenario"]: row for row in _rows() if row.get("kind") == "scenario"}
    for name in ("trading_day", "weekend_today"):
        response = scenarios[name]["response"]
        assert response["delivery_status"] in {"blocked", "failed"}
        assert response["report"] is None
    weekend_plan = next(
        row
        for row in _rows()
        if row.get("kind") == "run_log"
        and row.get("run_id") == scenarios["weekend_today"]["run_id"]
        and row.get("event") == "plan"
    )
    assert weekend_plan["requested_date"] == "2026-10-11"
    assert weekend_plan["as_of_date"] == "2026-10-09"
    assert weekend_plan["market_closed"] is True

    assert scenarios["code_only"]["tool"]["status"] == "success"
    assert scenarios["code_only"]["tool"]["normalized_datum_count"] > 0
    assert scenarios["name_match"]["tool"]["status"] == "error"
    assert scenarios["name_match"]["tool"]["normalized_datum_count"] == 0

    refresh = scenarios["partial_refresh_truncated"]["rows"]
    assert [row["phase"] for row in refresh] == ["initial_after_truncate", "refresh_after_truncate"]
    assert all(row["tool"]["partial"] is True for row in refresh)
    assert all(row["tool"]["source_datum_count"] > row["tool"]["normalized_datum_count"] for row in refresh)

    deadline = scenarios["gateway_deadline"]["tool"]
    assert deadline["status"] == "error"
    assert deadline["normalized_datum_count"] == 0
    assert "budget exhausted" in deadline["error"]

    sse = scenarios["sse_real"]["final_response"]
    assert sse["code"] == "timeout"
    assert sse["delivery_status"] == "failed"
    assert sse["persisted"] is False
    assert scenarios["sse_real"]["heartbeats"] >= 1


def test_policy_replay_keeps_unverified_and_news_only_claims_blocked():
    error_result = ToolResult(
        tool="quote",
        tool_key="quote",
        operation_id="get_market_quotes",
        arguments={"symbols": ["600519"]},
        status="error",
        normalized=[NormalizedDatum(tool="quote", metric="price", value=42, status="error")],
    )
    gate = run_evidence_gate([error_result])
    assert gate.has_evidence is False
    assert gate.valid_evidence_count == 0

    news = Evidence(
        id="news-1",
        source_tool="news_search",
        tool_key="news",
        metric="headline",
        value="公司发布公告",
        timestamp="2026-10-09",
        source="wire",
    )
    row = match_claims_to_evidence(
        {"claims": [{"claim": "该股今日上涨", "evidence_ids": ["news-1"]}]}, [news]
    )[0]
    assert row["supported"] is False
    assert "news_mention_not_price" in row["reason_codes"]

    quote = EvidenceItem(
        id="quote-001",
        source_tool="quote",
        domain="a_share",
        metric="change_pct",
        value=1.2,
        status="success",
        instrument="600000",
        instrument_name="浦发银行",
        as_of_date="2026-10-09",
        timestamp="2026-10-09",
        source="quote-feed",
        coverage="instrument",
    )
    report = MarketIntelligence(
        what_happened="浦发银行今日上涨；某公司未经核实",
        claims=[
            ClaimEvidence(claim="浦发银行今日上涨", evidence_ids=["quote-001"]),
            ClaimEvidence(claim="某公司未经核实", evidence_ids=[]),
            ClaimEvidence(claim="浦发银行收盘价为99元", evidence_ids=["forged-999"]),
        ],
    )
    out, _, codes = build_degraded_report(
        {
            "domain": "a_share",
            "requested_date": "2026-10-09",
            "planned_as_of_date": "2026-10-09",
            "as_of_date": "2026-10-09",
            "market_closed": False,
            "evidence": [quote],
        },
        report,
        {"verdict": "revise", "issues": []},
    )
    assert out is None
    assert codes == ["invalid_reference"]

    assessments = assess_report_entities(
        "胜宏科技今日上涨。",
        [
            ToolResult(
                tool="quote",
                tool_key="quote",
                arguments={"symbol": "300476"},
                status="error",
                normalized=[],
            )
        ],
    )
    assert assessments and assessments[0].existence == "unverified"
