"""P0-A entity regressions.

The registry loader is mocked only in its success/failure test. All other
checks use constructed tool responses and therefore do not exercise a live
gateway or exchange data source.
"""

from __future__ import annotations

from app.graph.nodes.critic import _code_level_issues
from app.models.evidence import Evidence
from app.models.market import NormalizedDatum, ToolResult
from app.research.entity_check import (
    assess_report_entities,
    check_report_entities,
    evidence_entity_names,
    local_entity_index,
)
from app.research.entity_registry import (
    RegistrySnapshot,
    fetch_full_a_share_registry,
    get_full_a_share_registry,
    snapshot_from_mapping,
)


def _pool_result(*datums: NormalizedDatum) -> ToolResult:
    return ToolResult(
        tool="list_limit_up_stocks",
        tool_key="limit_up_pool",
        arguments={"date": "2026-10-09"},
        status="success",
        normalized=list(datums),
    )


def test_successful_limit_up_pool_verifies_guanghe_technology():
    result = _pool_result(
        NormalizedDatum(
            tool="list_limit_up_stocks",
            metric="data[0].name",
            value="广合科技",
            instrument="SZ001389",
            domain="a_share",
        ),
        NormalizedDatum(
            tool="list_limit_up_stocks",
            metric="data[0].code",
            value="001389",
            instrument="SZ001389",
            domain="a_share",
        ),
    )

    unverified, _ = check_report_entities("广合科技今日涨停。", [result])
    assessment = assess_report_entities("广合科技今日涨停。", [result])[0]

    assert "广合科技" not in unverified
    assert "广合科技" in evidence_entity_names([result])
    assert assessment.existence == "verified"
    assert assessment.code_match == "verified"
    assert assessment.market_evidence


def test_successful_quote_code_name_pair_verifies_shenghong():
    evidence = [
        Evidence(
            id="quote-001",
            source_tool="get_market_quotes",
            metric="name",
            value="胜宏科技",
            instrument="SZ300476",
            domain="a_share",
            status="success",
        )
    ]

    unverified, _ = check_report_entities("胜宏科技今日上涨。", evidence)

    assert unverified == []


def test_error_quote_instrument_cannot_verify_entity():
    evidence = [
        Evidence(
            id="quote-err",
            source_tool="get_market_quotes",
            metric="tool_status",
            value="Failed",
            instrument="SZ300476",
            domain="a_share",
            status="error",
        )
    ]

    unverified, _ = check_report_entities("胜宏科技今日上涨。", evidence)

    assert "胜宏科技" in unverified


def test_sector_theme_is_not_company_entity():
    result = ToolResult(
        tool="list_limit_up_sectors",
        tool_key="limit_up_sectors",
        arguments={"date": "2026-10-09"},
        status="success",
        normalized=[
            NormalizedDatum(
                tool="list_limit_up_sectors",
                metric="data[0].sector",
                value="商业航天",
                domain="a_share",
            )
        ],
    )

    unverified, _ = check_report_entities("商业航天今日走强。", [result])

    assert "商业航天" not in unverified
    assert check_report_entities("人工智能今日走强。", [result])[0] == []


def test_a_share_company_hints_exclude_indices_and_other_markets():
    hints = local_entity_index()

    assert "宁德时代" in hints
    assert all(name not in hints for name in ("沪深300", "比特币", "黄金", "腾讯控股"))
    assert check_report_entities("沪深300今日上涨。", [])[0] == []


def test_no_suffix_hanwuji_is_verified_only_by_actual_evidence():
    result = _pool_result(
        NormalizedDatum(
            tool="list_limit_up_stocks",
            metric="data[0].name",
            value="寒武纪",
            instrument="SH688256",
            domain="a_share",
        ),
        NormalizedDatum(
            tool="list_limit_up_stocks",
            metric="data[0].code",
            value="688256",
            instrument="SH688256",
            domain="a_share",
        ),
    )

    unverified, _ = check_report_entities("寒武纪今日涨停。", [result])

    assert "寒武纪" not in unverified


def test_unverified_qualification_has_no_code_issue():
    report = {
        "what_happened": "行云科技尚未验证，无法确认其行情。",
        "unverified_entities": ["行云科技"],
    }

    assert _code_level_issues(report) == []


def test_unverified_positive_market_claim_requires_evidence_not_invalid_entity():
    report = {
        "what_happened": "行云科技今日领涨。",
        "unverified_entities": ["行云科技"],
    }

    issues = _code_level_issues(report)

    assert issues
    assert all(issue.kind != "invalid_entity" for issue in issues)
    assert any(issue.kind == "missing_evidence" and issue.action == "research_more" for issue in issues)


def test_verified_name_without_market_datum_still_requires_quote_evidence():
    registry = snapshot_from_mapping(
        {"300476": "胜宏科技"}, source="akshare.stock_info_a_code_name", as_of="2026-10-09"
    )
    assessment = assess_report_entities("胜宏科技今日领涨。", registry=registry)[0]
    report = {
        "what_happened": "胜宏科技今日领涨。",
        "unverified_entities": [],
        "entities_without_market_evidence": [assessment.name] if not assessment.market_evidence else [],
    }

    issues = _code_level_issues(report)

    assert assessment.existence == "verified"
    assert any(issue.kind == "missing_evidence" for issue in issues)
    assert not any(issue.kind == "invalid_entity" for issue in issues)


def test_qualification_does_not_excuse_later_claim_in_same_sentence():
    issues = _code_level_issues({
        "what_happened": "行云科技尚未验证，但行云科技今日领涨。",
        "unverified_entities": ["行云科技"],
    })

    assert any(issue.kind == "missing_evidence" for issue in issues)


def test_authoritative_name_code_conflict_is_invalid_entity():
    registry = snapshot_from_mapping(
        {"300476": "胜宏科技"},
        source="exchange.security_master",
        as_of="2026-10-09",
        authority="authoritative",
    )
    evidence = [
        Evidence(
            id="quote-001",
            source_tool="get_market_quotes",
            metric="name",
            value="广合科技",
            instrument="SZ300476",
            domain="a_share",
            status="success",
        )
    ]
    assessment = assess_report_entities("广合科技今日上涨。", evidence, registry=registry)[0]
    assert assessment.code_match == "contradicted"
    report = {
        "what_happened": "广合科技今日上涨。",
        "unverified_entities": [],
        "entity_conflicts": [
            {
                "name": assessment.name,
                "code": "SZ300476",
                "conflict": assessment.conflict,
                "source": registry.source,
                "as_of": registry.as_of,
                "authority": registry.authority,
            }
        ],
    }

    issues = _code_level_issues(report)

    assert any(issue.kind == "invalid_entity" for issue in issues)
    assert any(registry.source in issue.rationale for issue in issues)


def test_reference_registry_discrepancy_is_not_authoritative_conflict():
    registry = snapshot_from_mapping(
        {"300476": "胜宏科技"}, source="akshare.stock_info_a_code_name", as_of="2026-10-09"
    )
    evidence = [
        Evidence(
            id="quote-001", source_tool="get_market_quotes", metric="name",
            value="广合科技", instrument="SZ300476", status="success",
        )
    ]

    assessment = assess_report_entities("广合科技今日上涨。", evidence, registry=registry)[0]

    assert assessment.existence == "verified"
    assert assessment.code_match == "unverified"
    assert assessment.conflict is None


def test_code_only_quote_uses_full_registry_without_claiming_market_event():
    registry = snapshot_from_mapping(
        {"300476": "胜宏科技"}, source="akshare.stock_info_a_code_name", as_of="2026-10-09"
    )
    evidence = [
        Evidence(
            id="quote-001", source_tool="get_market_quotes", metric="close", value=100.0,
            instrument="SZ300476", domain="a_share", status="success",
        )
    ]

    assessment = assess_report_entities("胜宏科技今日上涨。", evidence, registry=registry)[0]

    assert assessment.existence == "verified"
    assert assessment.code_match == "verified"
    assert assessment.market_evidence
    assert assessment.source == "get_market_quotes"


def test_unavailable_registry_does_not_treat_common_map_as_full_universe():
    unavailable = RegistrySnapshot(source="akshare.stock_info_a_code_name", available=False)

    assessments = assess_report_entities("寒武纪与宁德时代今日上涨。", registry=unavailable)

    assert {item.name for item in assessments} == {"寒武纪", "宁德时代"}
    assert all(item.existence == "unverified" for item in assessments)


def test_unmatched_code_does_not_verify_name():
    registry = snapshot_from_mapping(
        {"300476": "胜宏科技"}, source="akshare.stock_info_a_code_name", as_of="2026-10-09"
    )
    evidence = [
        Evidence(
            id="quote-001", source_tool="get_market_quotes", metric="close", value=100.0,
            instrument="SZ001389", status="success",
        )
    ]

    assessment = assess_report_entities("胜宏科技今日上涨。", evidence, registry=registry)[0]

    assert assessment.existence == "verified"
    assert assessment.code_match == "unverified"
    assert not assessment.market_evidence


def test_news_text_mention_does_not_bind_code_to_name():
    evidence = [
        Evidence(
            id="news-001", source_tool="telegraph", metric="title",
            value="胜宏科技被新闻提及", instrument="SZ300476", status="success",
        )
    ]

    assessment = assess_report_entities("胜宏科技今日上涨。", evidence)[0]

    assert assessment.existence == "mentioned_only"
    assert assessment.code_match == "unverified"


async def test_full_registry_loader_preserves_source_date_and_unavailable_state(monkeypatch):
    from app.research.news_sources import akshare_sources

    async def available(*, timeout):
        return {"300476": "胜宏科技"}

    monkeypatch.setattr(akshare_sources, "fetch_code_name_map", available)
    snapshot = await fetch_full_a_share_registry(timeout=0.1)
    assert snapshot.mapping == {"300476": "胜宏科技"}
    assert snapshot.source == "akshare.stock_info_a_code_name"
    assert snapshot.as_of and snapshot.as_of_basis == "retrieval_date"
    assert snapshot.market == "a_share" and snapshot.listing_status == "unknown"
    assert snapshot.authority == "reference"

    async def unavailable(*, timeout):
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr(akshare_sources, "fetch_code_name_map", unavailable)
    failed = await fetch_full_a_share_registry(timeout=0.1)
    assert failed.available is False
    assert failed.mapping == {}
    assert failed.as_of is None
    assert failed.authority == "unavailable"


async def test_registry_default_timeout_allows_normal_initial_load(monkeypatch):
    from app.cache import Cache
    from app.research import entity_registry

    called = []

    async def load(*, timeout):
        called.append(timeout)
        return snapshot_from_mapping({"300476": "胜宏科技"}, source="test")

    monkeypatch.setattr(entity_registry, "market_cache", Cache())
    monkeypatch.setattr(entity_registry, "fetch_full_a_share_registry", load)

    assert (await get_full_a_share_registry()).available
    assert called == [15.0]
