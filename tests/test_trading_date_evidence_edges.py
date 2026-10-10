"""Date provenance edges across normalization, evidence gating, and claims."""

import pytest

from app.agent.evidence_gate import match_claims_to_evidence, run_evidence_gate
from app.config import Settings
from app.gateway.normalizer import normalize_tool_result
from app.graph.nodes.gate import GateNode
from app.models.evidence import Evidence
from app.models.market import NormalizedDatum, ToolResult


def _quote_result(*datums: NormalizedDatum) -> ToolResult:
    return ToolResult(
        tool="quote",
        tool_key="quote",
        arguments={"symbol": "600519"},
        status="success",
        normalized=list(datums),
    )


def _quote_datum(as_of_date: str | None) -> NormalizedDatum:
    return NormalizedDatum(
        tool="quote", metric="change_pct", value=2, instrument="600519",
        as_of_date=as_of_date, source="exchange",
    )


@pytest.mark.asyncio
async def test_gate_promotes_only_valid_market_datum_dates():
    result = _quote_result(
        _quote_datum("2026-10-09"),
        NormalizedDatum(tool="quote", metric="date", value="2026-10-10", as_of_date="2026-10-10"),
    )
    output = await GateNode(Settings())({
        "results": [result], "evidence": [],
        "requested_date": "2026-10-10", "planned_as_of_date": "2026-10-09",
    })
    assert output["gate"].valid_evidence_count == 1
    assert output["as_of_date"] == "2026-10-09"


@pytest.mark.asyncio
async def test_gate_preserves_stale_observation_but_not_unknown_date():
    stale = await GateNode(Settings())({
        "results": [_quote_result(_quote_datum("2026-10-08"))], "evidence": [],
        "requested_date": "2026-10-10", "planned_as_of_date": "2026-10-09",
    })
    assert stale["gate"].stale_evidence
    assert stale["gate"].valid_evidence_count == 0
    assert stale["as_of_date"] == "2026-10-08"

    unknown = await GateNode(Settings())({
        "results": [_quote_result(_quote_datum("unknown"))], "evidence": [],
        "requested_date": "2026-10-10", "planned_as_of_date": "2026-10-09",
    })
    assert unknown["gate"].valid_evidence_count == 1
    assert unknown["gate"].stale_evidence == []
    assert unknown["gate"].evidence_quality[0]["reason_codes"] == ["unknown_date"]
    assert unknown["as_of_date"] is None


@pytest.mark.asyncio
async def test_weekend_news_date_does_not_replace_market_observation_date():
    news = ToolResult(
        tool="news_search", tool_key="news_search", arguments={"query": "A股"},
        status="success", normalized=[NormalizedDatum(
            tool="news_search", metric="headline", value="周末公告",
            as_of_date="2026-10-10", source="wire",
        )],
    )
    state = {
        "results": [_quote_result(_quote_datum("2026-10-09")), news],
        "evidence": [], "requested_date": "2026-10-10",
        "planned_as_of_date": "2026-10-09",
    }
    output = await GateNode(Settings())(state)
    assert output["gate"].valid_evidence_count == 2
    assert output["gate"].stale_evidence == []
    assert output["as_of_date"] == "2026-10-09"
    news_quality = next(row for row in output["gate"].evidence_quality if row["date_scope"] == "event")
    assert news_quality["as_of_date"] == "2026-10-10"

    state["results"] = [news]
    news_only = await GateNode(Settings())(state)
    assert news_only["gate"].valid_evidence_count == 1
    assert news_only["as_of_date"] is None


def test_quote_rows_keep_their_own_observation_dates():
    result = normalize_tool_result(
        "get_market_quotes", {"symbols": ["600519", "000001"]},
        {"timestamp": "2026-10-10T09:00:00", "data": [
            {"symbol": "600519", "date": "2026-10-09", "change_pct": 2},
            {"symbol": "000001", "交易日期": "2026-10-08", "change_pct": -1},
        ]},
    )
    changes = [datum for datum in result.normalized if datum.metric.endswith(".change_pct")]
    assert [(datum.instrument, datum.as_of_date) for datum in changes] == [
        ("600519", "2026-10-09"), ("000001", "2026-10-08"),
    ]
    gate = run_evidence_gate([result], requested_date="2026-10-09")
    assert gate.valid_evidence_count == 1
    assert gate.stale_evidence[0]["as_of_date"] == "2026-10-08"


def test_closed_day_current_market_claim_cannot_borrow_previous_session():
    evidence = [Evidence(
        id="q-1", source_tool="quote", metric="change_pct", value=2,
        instrument="600519", as_of_date="2026-10-09", source="exchange",
        coverage="instrument",
    )]

    def assess(claim: str):
        return match_claims_to_evidence(
            {"claims": [{"claim": claim, "evidence_ids": ["q-1"]}]}, evidence,
            expected_date="2026-10-09", requested_date="2026-10-10", market_closed=True,
        )[0]

    today = assess("600519今天上涨")
    assert today["supported"] is False
    assert "closed_day_current_market_claim" in today["reason_codes"]
    assert assess("600519最近交易日上涨")["supported"] is True


def test_explicit_older_market_claim_is_historical_not_current():
    evidence = [Evidence(
        id="q-1", source_tool="quote", metric="change_pct", value=2,
        instrument="600519", as_of_date="2026-10-08", source="exchange",
    )]
    for claim in ("600519在2026-10-08上涨", "600519在2026年10月8日上涨", "600519在10月8日上涨"):
        row = match_claims_to_evidence(
            {"claims": [{"claim": claim, "evidence_ids": ["q-1"]}]}, evidence,
            expected_date="2026-10-09", requested_date="2026-10-10", market_closed=True,
        )[0]
        assert row["supported"] is True

    mismatch = match_claims_to_evidence(
        {"claims": [{"claim": "600519在10月8日上涨", "evidence_ids": ["q-1"]}]},
        [evidence[0].model_copy(update={"as_of_date": "2026-10-09"})],
        expected_date="2026-10-09", requested_date="2026-10-10", market_closed=True,
    )[0]
    assert "stale_date" in mismatch["reason_codes"]


def test_weekend_news_uses_request_date_not_market_session_date():
    evidence = [Evidence(
        id="n-1", source_tool="news_search", metric="headline", value="公告",
        as_of_date="2026-10-10", source="wire",
    )]
    row = match_claims_to_evidence(
        {"claims": [{"claim": "今天发布公告", "evidence_ids": ["n-1"]}]}, evidence,
        expected_date="2026-10-09", requested_date="2026-10-10", market_closed=True,
    )[0]
    assert row["supported"] is True
