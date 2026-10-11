"""Controlled degraded delivery tests; all data and memory writes are local fakes."""

from __future__ import annotations

import asyncio

from app.agent.persistence import persist_research
from app.graph.nodes.finalize import FinalizeAuditNode, build_degraded_report
from app.models.response import (
    ClaimEvidence,
    EvidenceItem,
    MarketIntelligence,
    ResearchResponse,
    build_response_from_state,
)


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