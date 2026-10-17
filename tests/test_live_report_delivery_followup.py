"""Offline regressions for the 2026-10-15 live report delivery follow-up."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.config import Settings
from app.gateway.controlled_fallback import (
    ControlledFallbackRouter,
    fallback_provenance_failures,
)
from app.gateway.normalizer import normalize_tool_result
from app.gateway.source_routing import skill_route_for
from app.graph.builder import critic_route_outcome
from app.graph.nodes.critic import CriticNode
from app.graph.nodes.finalize import FinalizeAuditNode
from app.graph.nodes.reasoning import ReasoningNode, _evidence_fallback_update, _select_evidence
from app.models.evidence import Evidence
from app.models.market import ToolResult
from app.research.reasoning import (
    MARKET_EVIDENCE_ONLY_FALLBACK_TITLE,
    build_market_evidence_only_fallback,
    plan_market_evidence_fallback,
)

DATE = "2026-10-14"


def _error_result() -> ToolResult:
    return normalize_tool_result("list_limit_up_sectors", {}, None, error="timeout")


def _sector_result(raw: dict) -> ToolResult:
    return normalize_tool_result("list_limit_up_sectors", {}, raw)


def test_limit_up_sector_query_keeps_user_corrected_terms():
    route = skill_route_for("list_limit_up_sectors", {})
    assert route is not None
    query = route[2]["query"]
    assert all(term in query for term in ("行业板块", "概念板块", "涨停数量排名"))
    assert "涨停板块 涨停题材" not in query


def test_query2data_empty_datas_is_error_without_evidence():
    result = normalize_tool_result("list_limit_up_sectors", {}, {"code_count": 0, "datas": []})
    assert result.status == "error"
    assert result.reason_codes == ["empty_normalized"]
    assert result.normalized == []


def test_query2data_rows_survive_structural_code_count():
    rows = [{"行业板块": f"板块{i}", "涨停家数": i + 1, "date": DATE} for i in range(10)]
    result = normalize_tool_result("list_limit_up_sectors", {}, {"code_count": 2030, "datas": rows})
    assert result.status == "success"
    assert len([d for d in result.normalized if not d.structural_metadata]) >= 20
    assert any(d.metric == "code_count" and d.structural_metadata for d in result.normalized)


def test_mcp_root_provider_rows_are_adopted_with_auditable_provenance():
    primary = _error_result()
    fallback = _sector_result({
        "source_used": "ashare-master",
        "count": 10,
        "rows": [{"板块": "机器人", "涨停家数": 8, "date": DATE}],
    })
    result = asyncio.run(ControlledFallbackRouter().call(
        run_id="fixture-root-provider", capability="limit_up_sectors", arguments={},
        primary_source="ths_skill", primary_transport="http_api", primary=lambda: _async(primary),
        fallback_source="mcp", fallback_transport="mcp", fallback=lambda: _async(fallback),
    ))
    assert result.source_switch is True
    business = [d for d in result.normalized if not d.structural_metadata]
    assert business
    assert all(d.authority == "ashare-master" and d.retrieved_at and d.as_of_date == DATE for d in business)


def test_mcp_nested_metadata_provider_is_adopted():
    result = _sector_result({
        "metadata": {"provider": "ashare-master"},
        "rows": [{"板块": "机器人", "涨停家数": 8, "date": DATE}],
    })
    assert result.authority == "ashare-master"
    assert all(d.authority == "ashare-master" for d in result.normalized if not d.structural_metadata)


def test_mcp_nested_result_source_used_is_adopted():
    result = _sector_result({
        "result": {"source_used": "ashare-master"},
        "rows": [{"板块": "机器人", "涨停家数": 8, "date": DATE}],
    })
    assert result.authority == "ashare-master"


def test_mcp_transport_without_provider_is_rejected_with_authority_gap():
    primary = _error_result()
    fallback = _sector_result({
        "transport": "mcp",
        "rows": [{"板块": "机器人", "涨停家数": 8, "date": DATE}],
    })
    codes = fallback_provenance_failures(primary, fallback, "limit_up_sectors", {})
    assert "fallback_missing_authority" in codes
    assert "fallback_missing_source" in codes
    assert "mcp" not in {d.authority for d in fallback.normalized}


def test_mcp_envelope_date_is_adopted_via_trusted_lineage():
    """契约保证同一快照的信封日期可经受信 lineage 传播到全批行（替代旧逐行规则）。

    旧修复单要求每条原始行重复携带日期；语义补标注后，适配器产出的受信
    ``row_date_lineage``（date_annotation=envelope）允许全批继承同一观察日。
    """
    primary = _error_result()
    fallback = _sector_result({
        "source_used": "ashare-master",
        "date": DATE,
        "rows": [{"板块": "机器人", "涨停家数": 8}],
    })
    codes = fallback_provenance_failures(primary, fallback, "limit_up_sectors", {})
    assert "fallback_missing_date" not in codes
    business = [d for d in fallback.normalized if not d.structural_metadata]
    assert business
    assert all(d.as_of_date == DATE for d in business)
    # 行级 lineage 必须标记为受信信封传播，而不是 observed 或自标注。
    lineage = (fallback.date_annotation or {}).get("row_date_lineage", {})
    assert lineage and all(
        info.get("trusted") and info.get("date_annotation") == "envelope"
        for info in lineage.values()
    )


def test_mcp_no_envelope_or_row_date_is_rejected():
    primary = _error_result()
    fallback = _sector_result({
        "source_used": "ashare-master",
        "rows": [{"板块": "机器人", "涨停家数": 8}],
    })
    codes = fallback_provenance_failures(primary, fallback, "limit_up_sectors", {})
    assert "fallback_missing_date" in codes
    assert all(not d.as_of_date for d in fallback.normalized if not d.structural_metadata)


def test_top_level_count_only_is_structural_and_rejected():
    primary = _error_result()
    fallback = _sector_result({"source_used": "ashare-master", "count": 2030})
    assert fallback.normalized == []
    assert "fallback_structural_metadata_only" in fallback_provenance_failures(
        primary, fallback, "limit_up_sectors", {}
    )


def test_market_reasoning_fallback_builds_report_without_target_instrument():
    evidence = [Evidence(
        id="quote-001", source_tool="get_market_quotes", tool_key="quote", operation_id="get_market_quotes",
        domain="a_share", metric="data[0].price", value=3210.5, instrument="SH000001",
        as_of_date=DATE, timestamp=DATE, retrieved_at=f"{DATE}T08:00:00+00:00",
        source="tencent", authority="tencent", completeness="complete", coverage="instrument",
    )]
    decision = plan_market_evidence_fallback(
        evidence, requested_date="2026-10-15", planned_as_of_date=DATE, as_of_date=None,
    )
    report = build_market_evidence_only_fallback(
        evidence, requested_date="2026-10-15", planned_as_of_date=DATE,
        as_of_date=None, decision=decision,
    )
    assert decision.enabled and decision.accepted_count == 1
    assert report is not None and report.title == MARKET_EVIDENCE_ONLY_FALLBACK_TITLE
    assert report.as_of_date == DATE and report.partial is True
    assert "不代表完整市场综述" in report.data_caveats[0]


def test_iwencai_industry_rows_cannot_be_market_breadth_evidence():
    evidence = [Evidence(
        id="iwencai-001", source_tool="internal_iwencai_market_query", tool_key="iwencai_market",
        operation_id="internal_iwencai_market_query", domain="a_share", metric="rows[0].最新价",
        value=10.2, as_of_date=DATE, timestamp=DATE, retrieved_at=f"{DATE}T08:00:00+00:00",
        source="iwencai", authority="tencent", completeness="complete", coverage="market",
    )]
    decision = plan_market_evidence_fallback(
        evidence, requested_date=DATE, planned_as_of_date=DATE,
    )
    assert decision.accepted_count == 0
    assert "unsupported_fact_type" in decision.diagnostics()["rejection_reasons"]


def test_market_sector_fact_requires_identity_and_structure_from_same_row():
    evidence = [
        Evidence(id="sector-name", source_tool="list_limit_up_sectors", domain="a_share",
                 metric="rows[0].板块", value="机器人", as_of_date=DATE, retrieved_at=f"{DATE}T08:00:00+00:00",
                 source="ashare-master", authority="ashare-master", completeness="complete", coverage="market"),
        Evidence(id="sector-count", source_tool="list_limit_up_sectors", domain="a_share",
                 metric="rows[1].涨停家数", value=8, as_of_date=DATE, retrieved_at=f"{DATE}T08:00:00+00:00",
                 source="ashare-master", authority="ashare-master", completeness="complete", coverage="market"),
    ]
    plan = plan_market_evidence_fallback(evidence, requested_date=DATE, planned_as_of_date=DATE)
    assert plan.accepted_count == 0


def test_market_sector_fact_is_reported_as_one_row_scoped_claim():
    evidence = [
        Evidence(id="sector-name", source_tool="list_limit_up_sectors", domain="a_share",
                 metric="rows[0].板块", value="机器人", as_of_date=DATE, retrieved_at=f"{DATE}T08:00:00+00:00",
                 source="ashare-master", authority="ashare-master", completeness="complete", coverage="market"),
        Evidence(id="sector-count", source_tool="list_limit_up_sectors", domain="a_share",
                 metric="rows[0].涨停家数", value=8, as_of_date=DATE, retrieved_at=f"{DATE}T08:00:00+00:00",
                 source="ashare-master", authority="ashare-master", completeness="complete", coverage="market"),
    ]
    report = build_market_evidence_only_fallback(
        evidence, requested_date=DATE, planned_as_of_date=DATE,
    )
    assert report is not None
    assert len(report.claims) == 1
    assert set(report.claims[0].evidence_ids) == {"sector-name", "sector-count"}
    assert "机器人涨停家数为8" in report.claims[0].claim


def test_market_timeout_path_selects_market_fallback_without_target():
    evidence = [Evidence(
        id="quote-001", source_tool="get_market_quotes", tool_key="quote", operation_id="get_market_quotes",
        domain="a_share", metric="data[0].price", value=3210.5, instrument="SH000001",
        as_of_date=DATE, timestamp=DATE, retrieved_at=f"{DATE}T08:00:00+00:00",
        source="tencent", authority="tencent", completeness="complete", coverage="instrument",
    )]
    state = {
        "question": "今天A股发生了什么",
        "domain": "a_share",
        "intent": {"domain": "a_share", "task": "market_summary"},
        "requested_date": "2026-10-15",
        "planned_as_of_date": DATE,
        "as_of_date": None,
        "evidence": [item.model_dump() for item in evidence],
        "results": [],
    }
    update = _evidence_fallback_update(
        SimpleNamespace(last_finish_reason=None, last_parse_status=None), state,
        requested_date="2026-10-15", target_instrument=None, phase="timeout",
    )
    assert update is not None and update["fallback_applied"] is True
    assert update["report"].as_of_date == DATE
    assert update["fallback_diagnostics"]["accepted_count"] == 1


def test_reasoning_timeout_node_delivers_market_facts_without_symbol():
    class TimeoutEngine:
        last_finish_reason = None
        last_parse_status = None
        last_llm_usage = None
        last_llm_duration_ms = 90000.0
        last_prompt_chars = 1200
        last_context_build_ms = 12.0
        last_parse_duration_ms = None
        last_evidence_ids = []
        last_completion_chars = 0
        last_request_max_tokens = 256
        last_target_filter = {}

        async def reason(self, **_kwargs):
            raise TimeoutError("fixture reasoning timeout")

    evidence = Evidence(
        id="quote-timeout-001", source_tool="get_market_quotes", tool_key="quote", operation_id="get_market_quotes",
        domain="a_share", metric="data[0].price", value=3210.5, instrument="SH000001",
        as_of_date=DATE, timestamp=DATE, retrieved_at=f"{DATE}T08:00:00+00:00",
        source="tencent", authority="tencent", completeness="complete", coverage="instrument",
    )
    state = {
        "question": "今天A股发生了什么",
        "domain": "a_share",
        "intent": {"domain": "a_share", "task": "market_summary"},
        "requested_date": "2026-10-15",
        "planned_as_of_date": DATE,
        "as_of_date": DATE,
        "market_closed": False,
        "evidence": [evidence.model_dump()],
        "results": [],
        "errors": [],
        "budget_deadline": 9999999999,
        "started_at": 1,
    }
    node = ReasoningNode(Settings(_env_file=None, openai_api_key="offline"))
    node._engine = TimeoutEngine()
    update = asyncio.run(node(state))
    assert update["report"] is not None
    assert update["delivery_status"] == "degraded"
    assert update["persisted"] is False
    assert update["reason_codes"] == ["evidence_only_fallback", "reasoning_timeout"]
    assert update["fallback_diagnostics"]["accepted_count"] == 1


def test_market_summary_prompt_caps_industry_rows_and_date_filters_news():
    evidence = []
    for index in range(30):
        evidence.append(Evidence(
            id=f"industry-{index}", source_tool="internal_iwencai_market_query",
            metric=f"rows[{index}].最新价", value=index + 1, as_of_date=DATE,
        ))
    for index, date in enumerate([DATE, DATE, "2026-10-13", DATE, DATE, DATE]):
        evidence.append(Evidence(
            id=f"news-{index}", source_tool="internal_market_telegraph",
            metric=f"search_results[{index}].title", value=f"事件{index}", as_of_date=date,
        ))
    selected, diagnostics = _select_evidence(
        evidence, None, total_limit=60, per_tool=10,
        market_summary=True, planned_as_of_date=DATE,
    )
    assert len([row for row in selected if row.source_tool == "internal_iwencai_market_query"]) <= 8
    news = [row for row in selected if "telegraph" in row.source_tool]
    assert len(news) <= 4
    assert all(row.as_of_date == DATE for row in news)
    assert diagnostics["omitted_by_tool"]["internal_iwencai_market_query"] >= 22


def test_finalize_strictly_audits_market_fallback_claims():
    evidence = [Evidence(
        id="quote-001", source_tool="get_market_quotes", tool_key="quote", operation_id="get_market_quotes",
        domain="a_share", metric="data[0].price", value=3210.5, instrument="SH000001",
        as_of_date=DATE, timestamp=DATE, retrieved_at=f"{DATE}T08:00:00+00:00",
        source="tencent", authority="tencent", completeness="complete", coverage="instrument",
    )]
    report = build_market_evidence_only_fallback(
        evidence, requested_date="2026-10-15", planned_as_of_date=DATE,
    )
    state = {
        "question": "今天A股发生了什么", "domain": "a_share", "requested_date": "2026-10-15",
        "planned_as_of_date": DATE, "as_of_date": DATE, "market_closed": False,
        "evidence": [item.model_dump() for item in evidence], "results": [], "report": report,
        "critique": {"verdict": "pass", "reason": "strict audit"},
        "reason_codes": ["evidence_only_fallback", "reasoning_timeout"],
        "degraded_reason_codes": ["evidence_only_fallback", "reasoning_timeout"],
    }
    finalized = asyncio.run(FinalizeAuditNode(Settings())(state))
    assert finalized["delivery_status"] == "degraded"
    assert finalized["report"] is not None
    assert finalized["reason_codes"][:2] == ["evidence_only_fallback", "reasoning_timeout"]

    state["evidence"] = [{**evidence[0].model_dump(), "authority": None}]
    blocked = asyncio.run(FinalizeAuditNode(Settings())(state))
    assert blocked["delivery_status"] == "blocked"
    assert blocked["report"] is None
    assert "missing_authority" in blocked["reason_codes"]


def test_reasoning_without_report_cannot_route_to_research_more():
    settings = SimpleNamespace(effective_max_rewrites=1, effective_max_research_rounds=2)
    state = {
        "report": None,
        "reason_codes": ["reasoning_timeout"],
        "critique": {"verdict": "research_more"},
        "rewrite_count": 0,
        "research_round_count": 0,
        "results": [],
    }
    decision, reason = critic_route_outcome(state, settings)
    assert decision == "finalize_audit"
    assert reason == "no_report_to_revise:reasoning_timeout"


def test_critic_marks_reasoning_without_report_as_terminal_error():
    node = CriticNode.__new__(CriticNode)
    result = asyncio.run(node({"report": None, "reason_codes": ["reasoning_timeout"]}))
    assert result["critique"].verdict == "error"
    assert "research_more" != result["critique"].verdict
    assert "reasoning_failed_no_report" in result["reason_codes"]


async def _async(value):
    return value
