"""P0-B regressions: returned datum quality is separate from tool success."""

from app.agent.evidence_gate import match_claims_to_evidence, run_evidence_gate
from app.gateway.normalizer import normalize_tool_result
from app.models.evidence import Evidence
from app.models.market import NormalizedDatum, ToolResult
from app.research.evidence import build_evidence


def _result(status="success", *, metric="price", value=10, instrument=None, timestamp=None, tool="quote", partial=False):
    return ToolResult(tool=tool, tool_key=tool, operation_id=tool, arguments={"symbol": instrument} if instrument else {}, status=status, partial=partial, normalized=[NormalizedDatum(tool=tool, metric=metric, value=value, instrument=instrument, timestamp=timestamp, partial=partial)])


def test_error_empty_and_request_only_are_not_valid_evidence():
    error = _result("error")
    empty = ToolResult(tool="quote", tool_key="quote", arguments={"symbol": "600519"}, status="success", normalized=[])
    request_only = ToolResult(tool="quote", tool_key="quote", arguments={}, status="success", normalized=[NormalizedDatum(tool="quote", metric="symbol", value="600519")])
    gate = run_evidence_gate([error, empty, request_only])
    assert gate.has_evidence is False
    assert gate.valid_evidence_count == 0
    assert gate.invalid_evidence_count == 3
    assert "empty_normalized" in gate.reason_codes


def test_code_only_quote_has_unknown_date_and_cannot_prove_today_limit_up():
    result = _result(metric="last", value=42, instrument="600519")
    evidence = build_evidence([result], id_prefix="technical", stable_ids=True)
    assert evidence[0].instrument == "600519"
    assert evidence[0].as_of_date == "unknown"
    report = {"claims": [{"claim": "600519今日涨停", "evidence_ids": [evidence[0].id]}]}
    row = match_claims_to_evidence(report, evidence)[0]
    assert "unknown_date" in row["reason_codes"]


def test_news_mention_does_not_support_price_change():
    evidence = [Evidence(id="news-1", source_tool="news_search", tool_key="news", metric="headline", value="公司发布公告", timestamp="2026-10-09", source="wire")]
    row = match_claims_to_evidence({"claims": [{"claim": "该股今日上涨", "evidence_ids": ["news-1"]}]}, evidence)[0]
    assert "news_mention_not_price" in row["reason_codes"]


def test_partial_is_limited_and_market_level_does_not_prove_single_stock():
    evidence = [Evidence(id="sent-1", source_tool="sentiment", tool_key="sentiment", metric="up_count", value=100, timestamp="2026-10-09", source="feed", partial=True, completeness="partial", coverage="market")]
    rows = match_claims_to_evidence({"claims": [{"claim": "600519今日上涨", "evidence_ids": ["sent-1"]}]}, evidence)
    assert "partial_only" in rows[0]["reason_codes"]
    assert "market_level_for_single_instrument" in rows[0]["reason_codes"]


def test_market_level_quote_cannot_prove_named_stock_move():
    evidence = [Evidence(id="m-1", source_tool="quote", metric="change_pct", value=1.2, timestamp="2026-10-09", source="quote", coverage="market")]
    row = match_claims_to_evidence({"claims": [{"claim": "贵州茅台上涨", "evidence_ids": ["m-1"]}]}, evidence)[0]
    assert "market_level_for_single_instrument" in row["reason_codes"]


def test_cross_instrument_and_stale_date_are_reported():
    evidence = [Evidence(id="q-1", source_tool="quote", tool_key="quote", metric="price", value=10, instrument="000001", timestamp="2026-10-08", source="quote")]
    gate = run_evidence_gate([_result(instrument="000001", timestamp="2026-10-08")], requested_date="2026-10-09")
    assert gate.stale_evidence
    row = match_claims_to_evidence({"claims": [{"claim": "600519上涨", "evidence_ids": ["q-1"]}]}, evidence)[0]
    assert "unmatched_instrument" in row["reason_codes"]


def test_instrument_name_mismatch_is_not_silently_accepted():
    evidence = [Evidence(id="q-2", source_tool="quote", metric="price", value=10, instrument="600519", instrument_name="贵州茅台", timestamp="2026-10-09", source="quote")]
    row = match_claims_to_evidence({"claims": [{"claim": "五粮液上涨", "evidence_ids": ["q-2"]}]}, evidence)[0]
    assert "unmatched_instrument" in row["reason_codes"]


def test_name_only_claim_requires_a_name_mapping_for_code_only_datum():
    evidence = [Evidence(id="q-3", source_tool="quote", metric="price", value=10, instrument="600519", timestamp="2026-10-09", source="quote", coverage="instrument")]
    row = match_claims_to_evidence({"claims": [{"claim": "五粮液上涨", "evidence_ids": ["q-3"]}]}, evidence)[0]
    assert "instrument_name_unknown" in row["reason_codes"]
    assert row["supported"] is False


def test_theme_and_index_terms_are_not_treated_as_single_stock_names():
    evidence = [Evidence(id="m-2", source_tool="quote", metric="change_pct", value=1.2, timestamp="2026-10-09", source="quote", coverage="market")]
    for claim in ("商业航天上涨", "沪深300上涨"):
        row = match_claims_to_evidence({"claims": [{"claim": claim, "evidence_ids": ["m-2"]}]}, evidence)[0]
        assert "market_level_for_single_instrument" not in row["reason_codes"]


def test_request_path_metadata_and_error_datums_are_not_evidence():
    result = ToolResult(
        tool="quote",
        tool_key="quote",
        arguments={"symbol": "600519"},
        status="success",
        normalized=[
            NormalizedDatum(tool="quote", metric="quote.symbol", value="600519"),
            NormalizedDatum(tool="quote", metric="price", value=42, status="error"),
        ],
    )
    gate = run_evidence_gate([result])
    assert gate.has_evidence is False
    assert gate.valid_evidence_count == 0
    assert gate.invalid_evidence_count == 2
    assert "invalid_datum" in gate.reason_codes


def test_as_of_and_provenance_fields_survive_evidence_conversion():
    result = ToolResult(
        tool="quote",
        tool_key="quote",
        arguments={"symbol": "600519"},
        status="success",
        normalized=[
            NormalizedDatum(
                tool="quote",
                metric="price",
                value=42,
                instrument="600519",
                as_of_date="2026-10-10",
                retrieved_at="2026-10-10T09:00:00Z",
                authority="exchange",
                completeness="complete",
                instrument_scope="instrument",
            )
        ],
    )
    evidence = build_evidence([result], id_prefix="quote")
    assert evidence[0].as_of_date == "2026-10-10"
    assert evidence[0].retrieved_at == "2026-10-10T09:00:00Z"
    assert evidence[0].authority == "exchange"
    assert evidence[0].completeness == "complete"
    assert evidence[0].coverage == "instrument"


def test_batched_quotes_keep_each_returned_instrument():
    result = normalize_tool_result(
        "get_market_quotes",
        {"symbols": ["600519", "000001"]},
        {"data": [{"symbol": "600519", "last": 1800}, {"symbol": "000001", "last": 10}], "timestamp": "2026-10-09", "source": "feed"},
    )
    prices = [datum for datum in result.normalized if datum.metric.endswith(".last")]
    assert [datum.instrument for datum in prices] == ["600519", "000001"]
    gate = run_evidence_gate([result])
    assert gate.unmatched_instruments == []
    evidence = build_evidence([result], id_prefix="quote")
    other_price = next(item for item in evidence if item.metric == "data[1].last")
    row = match_claims_to_evidence({"claims": [{"claim": "600519价格为10", "evidence_ids": [other_price.id]}]}, evidence)[0]
    assert "unmatched_instrument" in row["reason_codes"]


def test_unattributed_batched_quote_cannot_prove_a_single_stock():
    result = ToolResult(
        tool="get_market_quotes",
        tool_key="quote",
        arguments={"symbols": ["600519", "000001"]},
        status="success",
        normalized=[NormalizedDatum(tool="get_market_quotes", metric="last", value=10, timestamp="2026-10-09", source="feed")],
    )
    gate = run_evidence_gate([result])
    assert gate.valid_evidence_count == 0
    assert gate.unmatched_instruments
    evidence = build_evidence([result], id_prefix="quote")
    assert evidence[0].instrument is None


def test_market_predicate_requires_the_corresponding_datum():
    def reasons(claim, metric, value):
        item = Evidence(id="q", source_tool="quote", metric=metric, value=value, instrument="600519", timestamp="2026-10-09", source="feed", coverage="instrument")
        return match_claims_to_evidence({"claims": [{"claim": claim, "evidence_ids": ["q"]}]}, [item])[0]["reason_codes"]

    assert "missing_limit_up_status" in reasons("600519今日涨停", "price", 10)
    assert "missing_direction_metric" in reasons("600519上涨", "price", 10)
    assert "contradictory_direction" in reasons("600519上涨", "change_pct", -4)
    assert "numeric_mismatch" in reasons("600519上涨10%", "change_pct", 5)
    assert "numeric_mismatch" in reasons("600519价格20元", "price", 10)
    assert "missing_limit_up_status" in reasons("600519今日涨停", "name", "某公司")
    assert "missing_limit_up_status" in reasons("600519今日涨停", "涨停家数", 70)
    assert "missing_limit_up_status" in reasons("600519今日涨停", "limit_up", 70)
    assert "missing_direction_metric" in reasons("600519上涨", "exchange_rate", 1.2)
    assert reasons("600519上涨", "change_pct", 4) == []


def test_named_theme_cannot_borrow_a_stock_or_generic_market_move():
    stock = Evidence(id="s", source_tool="quote", metric="change_pct", value=3, instrument="600519", timestamp="2026-10-09", source="feed", coverage="instrument")
    market = Evidence(id="m", source_tool="market", metric="change_pct", value=3, timestamp="2026-10-09", source="feed", coverage="market")
    for item in (stock, market):
        row = match_claims_to_evidence({"claims": [{"claim": "商业航天上涨", "evidence_ids": [item.id]}]}, [item])[0]
        assert "unmatched_subject" in row["reason_codes"]


def test_verified_name_code_mapping_can_support_code_only_quote():
    evidence = [Evidence(id="q", source_tool="quote", metric="change_pct", value=2, instrument="600519", timestamp="2026-10-09", source="feed", coverage="instrument")]
    report = {"entity_code_matches": {"贵州茅台": "600519"}, "claims": [{"claim": "贵州茅台上涨", "evidence_ids": ["q"]}]}
    row = match_claims_to_evidence(report, evidence)[0]
    assert row["supported"] is True


def test_claim_date_must_match_evidence_as_of_date():
    evidence = [Evidence(id="q", source_tool="quote", metric="change_pct", value=2, instrument="600519", as_of_date="2026-10-09", source="feed")]
    row = match_claims_to_evidence({"claims": [{"claim": "600519在2026-10-10上涨", "evidence_ids": ["q"]}]}, evidence)[0]
    assert "stale_date" in row["reason_codes"]


def test_today_claim_uses_explicit_requested_date():
    evidence = [Evidence(id="q", source_tool="quote", metric="change_pct", value=2, instrument="600519", as_of_date="2026-10-09", source="feed")]
    row = match_claims_to_evidence({"claims": [{"claim": "600519今日上涨", "evidence_ids": ["q"]}]}, evidence, expected_date="2026-10-10")[0]
    assert "stale_date" in row["reason_codes"]


def test_news_context_does_not_invalidate_matching_quote():
    quote = Evidence(id="q", source_tool="quote", metric="change_pct", value=3, instrument="600519", timestamp="2026-10-09", source="feed")
    news = Evidence(id="n", source_tool="news_search", metric="headline", value="公司公告", timestamp="2026-10-09", source="wire")
    claim = {"claims": [{"claim": "600519今日上涨", "evidence_ids": ["q", "n"]}]}
    row = match_claims_to_evidence(claim, [quote, news])[0]
    assert row["supported"] is True
    assert row["reason_codes"] == []


def test_invalid_reference_still_blocks_when_other_evidence_is_valid():
    quote = Evidence(id="q", source_tool="quote", metric="change_pct", value=3, instrument="600519", timestamp="2026-10-09", source="feed")
    claim = {"claims": [{"claim": "600519今日上涨", "evidence_ids": ["q", "forged"]}]}
    row = match_claims_to_evidence(claim, [quote])[0]
    assert row["supported"] is False
    assert "invalid_reference" in row["reason_codes"]


def test_returned_limit_up_status_is_not_filtered_as_tool_status():
    result = normalize_tool_result(
        "list_limit_up_stocks",
        {},
        {"data": [{"code": "001389", "name": "广合科技", "status": "涨停"}], "timestamp": "2026-10-09", "source": "feed"},
    )
    status = next(datum for datum in result.normalized if datum.metric == "data[0].status")
    assert status.instrument == "001389"
    gate = run_evidence_gate([result])
    assert gate.has_evidence is True
    evidence = build_evidence([result], id_prefix="sentiment")
    status_evidence = next(item for item in evidence if item.metric == "data[0].status")
    claim = {"claims": [{"claim": "广合科技今日涨停", "evidence_ids": [status_evidence.id]}]}
    row = match_claims_to_evidence(claim, evidence)[0]
    assert row["supported"] is True


def test_directly_conflicting_quotes_do_not_pass_on_one_favorable_citation():
    positive = Evidence(id="up", source_tool="quote", metric="change_pct", value=3, instrument="600519", timestamp="2026-10-09", source="feed")
    negative = Evidence(id="down", source_tool="quote", metric="change_pct", value=-3, instrument="600519", timestamp="2026-10-09", source="feed")
    row = match_claims_to_evidence({"claims": [{"claim": "600519上涨", "evidence_ids": ["up", "down"]}]}, [positive, negative])[0]
    assert "conflicting_evidence" in row["reason_codes"]
    assert row["supported"] is False


def test_conflicting_explicit_prices_are_flagged():
    first = Evidence(id="a", source_tool="quote", metric="price", value=20, instrument="600519", timestamp="2026-10-09", source="feed")
    second = Evidence(id="b", source_tool="quote", metric="price", value=10, instrument="600519", timestamp="2026-10-09", source="feed")
    row = match_claims_to_evidence({"claims": [{"claim": "600519价格20元", "evidence_ids": ["a", "b"]}]}, [first, second])[0]
    assert "conflicting_evidence" in row["reason_codes"]
