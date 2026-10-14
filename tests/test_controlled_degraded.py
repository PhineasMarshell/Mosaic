"""Controlled degraded delivery tests; all data and memory writes are local fakes."""

from __future__ import annotations

import asyncio

from app.agent.persistence import persist_research
from app.graph.nodes.finalize import FinalizeAuditNode, build_degraded_report
from app.models.evidence import Evidence
from app.models.market import NormalizedDatum, ToolResult
from app.models.response import (
    ClaimEvidence,
    EvidenceItem,
    MarketIntelligence,
    ResearchResponse,
    build_response_from_state,
)
from app.research.evidence import build_evidence
from app.research.reasoning import build_evidence_only_fallback


def _evidence(**overrides):
    values = {
        "id": "quote-001",
        "source_tool": "quote",
        "domain": "a_share",
        "metric": "change_pct",
        "value": 1.2,
        "status": "success",
        "instrument": "600000",
        "instrument_name": "浦发银行",
        "as_of_date": "2026-10-09",
        "timestamp": "2026-10-09",
        "source": "quote-feed",
        "coverage": "instrument",
    }
    values.update(overrides)
    return EvidenceItem(**values)


def _state(report, evidence, **overrides):
    state = {
        "domain": "a_share",
        "requested_date": "2026-10-09",
        "planned_as_of_date": "2026-10-09",
        "as_of_date": "2026-10-09",
        "market_closed": False,
        "evidence": evidence,
        "report": report,
    }
    state.update(overrides)
    return state


def _fallback_evidence(**overrides):
    values = {
        "id": "fallback-001",
        "source_tool": "quote",
        "metric": "price",
        "value": 1800,
        "instrument": "SH600519",
        "as_of_date": "2026-10-09",
        "source": "feed",
        "retrieved_at": "2026-10-10T00:00:00Z",
        "coverage": "instrument",
        "completeness": "complete",
    }
    values.update(overrides)
    return Evidence(**values)


def test_evidence_only_fallback_requires_explicit_complete_provenance():
    for overrides in (
        {"completeness": "unknown"},
        {"coverage": "unknown"},
        {"source": None},
        {"retrieved_at": None},
        {"partial": True},
        {"as_of_date": "2026-10-08"},
        {"instrument": "SZ000001"},
    ):
        assert build_evidence_only_fallback(
            [_fallback_evidence(**overrides)],
            requested_date="2026-10-09",
            target_instrument="SH600519",
        ) is None


def _mixed_report():
    return MarketIntelligence(
        requested_date="2026-10-09",
        as_of_date="2026-10-09",
        market_closed=False,
        what_happened="浦发银行今日上涨；某公司发布了未经核实的政策消息",
        claims=[
            ClaimEvidence(claim="浦发银行今日上涨", evidence_ids=["quote-001"]),
            ClaimEvidence(claim="某公司发布了未经核实的政策消息", evidence_ids=[]),
        ],
    )


def test_mixed_claims_are_filtered_into_degraded_report():
    report, gaps, codes = build_degraded_report(
        _state(_mixed_report(), [_evidence()]),
        _mixed_report(),
        {"verdict": "revise", "issues": []},
    )

    assert report is not None
    assert report.partial is True
    assert report.claims[0].claim == "浦发银行今日上涨"
    assert "政策消息" not in report.what_happened
    assert report.requested_date == "2026-10-09"
    assert report.as_of_date == "2026-10-09"
    assert report.source == ["quote-feed"]
    assert report.coverage == ["instrument"]
    assert gaps and "删除" in gaps[0]
    assert "degraded_partial_evidence" in codes


def test_closed_day_only_is_not_an_empty_degraded_report():
    report = MarketIntelligence(
        requested_date="2026-10-11",
        market_closed=True,
        what_happened="请求日休市；暂无可核验行情",
        claims=[],
    )
    out, _, codes = build_degraded_report(
        _state(report, [], requested_date="2026-10-11", market_closed=True, as_of_date=None, planned_as_of_date="2026-10-10"),
        report,
        {"verdict": "revise", "issues": []},
    )
    assert out is None
    assert codes == ["no_verified_claim"]


def test_all_claims_without_evidence_remain_blocked():
    report = MarketIntelligence(claims=[ClaimEvidence(claim="市场全面反转", evidence_ids=[])])
    out, _, codes = build_degraded_report(_state(report, []), report, {"verdict": "revise", "issues": []})
    assert out is None
    assert codes == ["no_verified_claim"]


def test_forged_reference_blocks_even_beside_real_claim():
    report = _mixed_report()
    report.claims.append(ClaimEvidence(claim="浦发银行收盘价为 99 元", evidence_ids=["E999"]))
    out, _, codes = build_degraded_report(_state(report, [_evidence()]), report, {"verdict": "revise", "issues": []})
    assert out is None
    assert codes == ["invalid_reference"]


def test_partial_strong_conclusion_remains_blocked():
    report = MarketIntelligence(
        claims=[ClaimEvidence(claim="涨停池形成最强主线并全面反转", evidence_ids=["quote-001"])],
    )
    partial = _evidence(partial=True, status="partial", coverage="market")
    out, _, codes = build_degraded_report(_state(report, [partial]), report, {"verdict": "revise", "issues": []})
    assert out is None
    assert codes == ["no_verified_claim"] or "partial_only" in codes


def test_degraded_persistence_is_always_false_without_writes():
    class NoWriteMemory:
        def save_research(self, *_args, **_kwargs):
            raise AssertionError("degraded result entered persistence")

    result = ResearchResponse(
        question="今天A股发生了什么",
        report=MarketIntelligence(what_happened="浦发银行今日上涨", partial=True),
        critique={"verdict": "revise"},
        final_audit_status="revise_exhausted",
        delivery_status="degraded",
        delivery_reason="degraded_partial_evidence",
    )
    assert asyncio.run(persist_research(result, result.question, None, memory=NoWriteMemory())) is False
    assert result.persisted is False


def test_unrelated_tool_timeout_does_not_block_verified_fact_degraded():
    report = _mixed_report()
    out, _, codes = build_degraded_report(
        _state(report, [_evidence()], errors=["optional sentiment tool timeout"]),
        report,
        {"verdict": "revise", "issues": []},
    )
    assert out is not None
    assert out.claims[0].evidence_ids == ["quote-001"]
    assert "degraded_partial_evidence" in codes


def test_finalize_keeps_evidence_only_fallback_report():
    evidence = Evidence(
        id="quote-fallback", source_tool="quote", metric="price", value=1800,
        instrument="SH600519", instrument_name="贵州茅台", as_of_date="2026-10-09",
        source="feed", authority="exchange", retrieved_at="2026-10-10T00:00:00Z",
            completeness="complete", coverage="instrument",
    )
    report = build_evidence_only_fallback(
        [evidence], requested_date="2026-10-09", target_instrument="600519"
    )
    state = _state(
        report, [evidence], reason_codes=["evidence_only_fallback", "llm_output_error"],
        critique={"verdict": "research_more", "issues": []}, errors=["reasoning output error"],
    )
    out = asyncio.run(FinalizeAuditNode(None)(state))
    assert out["delivery_status"] == "degraded"
    assert out["report"] is not None
    assert out["report"].claims[0].evidence_ids == ["quote-fallback"]


def test_evidence_only_fallback_requires_same_date_and_is_fact_only():
    same = Evidence(
        id="quote-same", source_tool="quote", metric="price", value=1800,
        instrument="SH600519", instrument_name="贵州茅台", as_of_date="2026-10-09",
        source="feed", authority="exchange", retrieved_at="2026-10-10T00:00:00Z",
        completeness="complete", coverage="instrument",
    )
    old = same.model_copy(update={"id": "quote-old", "as_of_date": "2026-10-08"})
    report = build_evidence_only_fallback(
        [same, old], requested_date="2026-10-09", target_instrument="SH600519"
    )
    assert report is not None
    assert report.partial is True
    assert len(report.claims) == 1
    assert report.claims[0].evidence_ids == ["quote-same"]
    assert report.why == [] and report.risks == []
    assert build_evidence_only_fallback(
        [old], requested_date="2026-10-09", target_instrument="SH600519"
    ) is None


def test_finalize_emits_structured_degraded_reason_and_keeps_report():
    report = _mixed_report()
    state = _state(
        report,
        [_evidence()],
        critique={"verdict": "revise", "reason": "缺少外部事实证据", "issues": []},
        errors=[],
    )
    out = asyncio.run(FinalizeAuditNode(None)(state))
    assert out["delivery_status"] == "degraded"
    assert out["final_audit_status"] == "revise_exhausted"
    assert out["report"] is not None
    assert out["reason_codes"]
    assert out["blocked_reason"] == ""


def test_a_share_announcement_media_evidence_can_be_degraded():
    evidence = Evidence(
        id="announcement-001",
        source_tool="internal_iwencai_announcement_search",
        domain="media",
        metric="announcement_1",
        value={"title": "贵州茅台公告"},
        instrument="600519",
        instrument_name="贵州茅台",
        as_of_date="2026-10-09",
        retrieved_at="2026-10-09T10:00:00Z",
        source="同花顺问财",
        authority="同花顺问财",
        completeness="complete",
        coverage="instrument",
    )
    report = MarketIntelligence(
        requested_date="2026-10-09",
        as_of_date="2026-10-09",
        claims=[ClaimEvidence(claim="贵州茅台在2026-10-09发布公告", evidence_ids=[evidence.id])],
    )
    out, _, codes = build_degraded_report(_state(report, [evidence]), report, {"verdict": "revise", "issues": []})
    assert out is not None
    assert out.claims[0].evidence_ids == [evidence.id]
    assert "degraded_partial_evidence" in codes


def test_cross_market_media_evidence_still_fails_a_share_degraded():
    evidence = Evidence(
        id="hk-announcement-001",
        source_tool="internal_iwencai_announcement_search",
        domain="media",
        metric="announcement_1",
        value={"title": "腾讯公告"},
        instrument="00700",
        instrument_name="腾讯控股",
        as_of_date="2026-10-09",
        retrieved_at="2026-10-09T10:00:00Z",
        source="同花顺问财",
        authority="同花顺问财",
        completeness="complete",
        coverage="instrument",
    )
    report = MarketIntelligence(
        requested_date="2026-10-09",
        as_of_date="2026-10-09",
        claims=[ClaimEvidence(claim="腾讯控股在2026-10-09发布公告", evidence_ids=[evidence.id])],
    )
    out, _, codes = build_degraded_report(_state(report, [evidence]), report, {"verdict": "revise", "issues": []})
    assert out is None
    assert codes == ["domain_mismatch"]


def test_partial_result_does_not_pollute_complete_datum_evidence():
    result = ToolResult(
        tool="quote",
        tool_key="quote",
        operation_id="quote",
        arguments={"symbol": "600519"},
        status="partial",
        partial=True,
        normalized=[
            NormalizedDatum(
                tool="quote", metric="change_pct", value=2.0, instrument="600519",
                as_of_date="2026-10-09", retrieved_at="2026-10-09T10:00:00Z",
                source="feed", authority="exchange", completeness="complete",
                instrument_scope="instrument",
            ),
            NormalizedDatum(
                tool="quote", metric="price", value=1800.0, instrument="600519",
                source="feed", authority="exchange", completeness="unknown",
                instrument_scope="instrument", partial=True,
            ),
        ],
    )
    evidence = build_evidence([result], id_prefix="quote")
    dated = next(item for item in evidence if item.metric == "change_pct")
    missing_date = next(item for item in evidence if item.metric == "price")
    assert dated.partial is False
    assert dated.result_partial is True
    assert dated.completeness == "complete"
    assert missing_date.partial is True
    report = MarketIntelligence(
        requested_date="2026-10-09", as_of_date="2026-10-09",
        claims=[ClaimEvidence(claim="600519在2026-10-09上涨2%", evidence_ids=[dated.id])],
    )
    out, _, _ = build_degraded_report(_state(report, evidence), report, {"verdict": "revise", "issues": []})
    assert out is not None
    assert out.claims[0].evidence_ids == [dated.id]


def test_sync_and_sse_shared_builder_exposes_identical_degraded_fields():
    report = _mixed_report()
    state = _state(
        report,
        [_evidence()],
        final_audit_status="revise_exhausted",
        delivery_status="degraded",
        delivery_reason="degraded_partial_evidence",
        unresolved_issues=["missing external fact"],
        reason_codes=["degraded_partial_evidence"],
        persisted=False,
        run_id="run-degraded",
        critique={"verdict": "revise", "issues": []},
    )
    sync = build_response_from_state(state, "今天A股发生了什么")
    sse = build_response_from_state(state, "今天A股发生了什么")
    fields = ("delivery_status", "final_audit_status", "delivery_reason", "unresolved_issues", "persisted")
    assert [getattr(sync, field) for field in fields] == [getattr(sse, field) for field in fields]
    assert sync.delivery_status == "degraded"
    assert sync.persisted is False
