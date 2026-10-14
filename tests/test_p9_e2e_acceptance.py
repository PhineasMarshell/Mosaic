"""P9 regression checks for the complete main-route delivery contract."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.agent.evidence_gate import match_claims_to_evidence
from app.agent.persistence import persist_research
from app.graph import run_log
from app.models.market import NormalizedDatum, ToolResult
from app.models.response import ClaimEvidence, EvidenceItem, MarketIntelligence, build_response_from_state
from scripts import p9_e2e_acceptance as p9


def _terminal_state(*, delivery: str = "verified", verdict: str = "pass", result: ToolResult | None = None):
    report = MarketIntelligence(
        title="测试报告",
        market_state="stable",
        what_happened="已核验事实",
        claims=[ClaimEvidence(claim="已核验事实", evidence_ids=["quote-1"])],
    )
    evidence = [EvidenceItem(id="quote-1", source_tool="quote", tool_key="quote", metric="change_pct", value=1.0,
                             instrument="600519", as_of_date="2026-10-09", source="quote-feed", coverage="instrument")]
    return {
        "question": "2026-10-09 今天 A 股发生了什么",
        "domain": "a_share",
        "run_id": "p9-test-run",
        "report": report,
        "evidence": evidence,
        "results": [result] if result else [],
        "critique": {"verdict": verdict, "reason": "test", "issues": []},
        "final_audit_status": "pass" if delivery == "verified" else "revise_exhausted",
        "delivery_status": delivery,
        "delivery_reason": "test",
        "reason_codes": [],
        "persisted": False if delivery != "verified" else True,
        "errors": [],
    }


def test_response_aggregates_auth_and_source_switch_diagnostics():
    result = ToolResult(
        tool="get_market_quotes", operation_id="get_market_quotes", tool_key="quote", arguments={"symbol": "600519"},
        status="error", normalized=[], reason_codes=["gateway_auth_error", "ths_invalid_credentials",
                                                      "mcp_fallback_not_attempted_auth"],
        primary_source="ths_skill", fallback_source=None, fallback_reason="authentication_failure_no_fallback",
        source_switch=False,
    )
    response = build_response_from_state({
        "question": "查询行情", "run_id": "p9-auth", "results": [result], "evidence": [],
        "critique": {"verdict": "research_more", "issues": []}, "report": None,
        "reason_codes": [], "errors": [], "delivery_status": "blocked", "final_audit_status": "research_exhausted",
    }, "查询行情")
    assert response.persisted is False
    assert response.source_switch is False
    assert {"ths_invalid_credentials", "mcp_fallback_not_attempted_auth"} <= set(response.reason_codes)


def test_response_aggregates_source_switch_from_tool_result():
    result = ToolResult(tool="get_market_quotes", operation_id="get_market_quotes", tool_key="quote", arguments={},
                        status="success", normalized=[NormalizedDatum(tool="get_market_quotes", metric="close", value=1,
                                                                       instrument="600519", as_of_date="2026-10-09",
                                                                       source="mcp", authority="mcp", coverage="instrument")],
                        primary_source="ths_skill", fallback_source="mcp", source_switch=True)
    response = build_response_from_state(_terminal_state(result=result), "2026-10-09 今天 A 股发生了什么")
    assert response.source_switch is True
    assert response.tool_results[0]["source_switch"] is True


def test_delivery_log_keeps_source_switch_and_run_diagnostics(caplog):
    switched = ToolResult(tool="get_market_quotes", operation_id="get_market_quotes", arguments={}, status="error",
                          normalized=[], reason_codes=["fallback_transport_error"], source_switch=True)
    with caplog.at_level(logging.INFO, logger="app.graph.run_log"):
        run_log.log_delivery({"run_id": "p9-log", "tool_results": [switched], "reason_codes": ["fallback_transport_error"],
                              "elapsed_ms": 12.5, "final_audit_status": "error"}, delivery_status="failed",
                             persisted=False, sink="sync")
    payload = json.loads(caplog.records[-1].getMessage())
    assert payload["run_id"] == "p9-log"
    assert payload["source_switch"] is True
    assert payload["persisted"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("reason_codes", [
    ["gateway_timeout"],
    ["ths_invalid_credentials", "mcp_fallback_not_attempted_auth"],
    ["fallback_transport_error"],
])
async def test_transport_failures_never_persist_verified_report(reason_codes):
    class NoWriteMemory:
        def save_research(self, *_args, **_kwargs):
            raise AssertionError("transport failure entered persistence")

    failed = ToolResult(tool="get_market_quotes", operation_id="get_market_quotes", tool_key="quote", arguments={},
                        status="error", normalized=[], reason_codes=reason_codes, fallback_source="mcp")
    response = build_response_from_state(_terminal_state(result=failed), "2026-10-09 今天 A 股发生了什么")
    # The terminal can remain useful to the caller, but transport failures are
    # never written into reusable memory.
    assert await persist_research(response, response.question, None, memory=NoWriteMemory()) is False
    assert response.persisted is False


def test_claim_constraints_reject_news_as_price_and_missing_date():
    news = EvidenceItem(id="n1", source_tool="news_search", metric="headline", value="上涨", instrument="600519",
                        source="news", coverage="instrument")
    rows = match_claims_to_evidence(
        {"claims": [{"claim": "600519 今日上涨", "evidence_ids": ["n1"]}]}, [news],
        expected_date="2026-10-09", requested_date="2026-10-09", market_closed=False,
    )
    assert not rows[0]["supported"]
    assert {"news_mention_not_price", "unknown_date"} <= set(rows[0]["reason_codes"])


def test_p9_scenario_catalog_covers_requested_real_paths():
    assert {"trading_day_today_ashare", "weekend_today_ashare", "quote", "kline", "limit_up_pool",
            "limit_up_sectors", "news", "announcement", "index", "sector_selector", "business", "insresearch"} <= set(p9.SCENARIOS)


def test_p9_replay_rejects_missing_transport(tmp_path, monkeypatch):
    artifact = tmp_path / "p9.jsonl"
    run_id = "fixture-run"
    rows = [{"kind": "run_complete", "probe_run_id": run_id}]
    for scenario in p9.REQUIRED_SCENARIOS:
        terminal = {
            "run_id": "fixture-run",
            "final_audit_status": "pass",
            "delivery_status": "verified",
            "persisted": True,
            "reason_codes": [],
            "elapsed_ms": 1,
            "stage": "finalize",
            "exception_type": None,
            "delivery_reason": "ok",
            "blocked_reason": "",
            "source_switch": False,
            "partial": False,
        }
        rows.append({"kind": "scenario", "probe_run_id": run_id, "scenario": scenario, "transport": "sync",
                     "terminal": terminal})
    artifact.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    monkeypatch.setattr(p9, "ARTIFACT", artifact)
    with pytest.raises(AssertionError):
        p9.replay()


def test_sync_blocked_response_has_same_terminal_code_as_sse(monkeypatch):
    class Orchestrator:
        async def run(self, question, domain=None, conversation_id=None):
            from app.models.response import ResearchResponse

            return ResearchResponse(question=question, report=None, critique={"verdict": "revise"},
                                    final_audit_status="revise_exhausted", delivery_status="blocked",
                                    delivery_reason="audit blocked", blocked_reason="rewrite_budget_exhausted",
                                    run_id="sync-blocked", persisted=False)

    monkeypatch.setattr(main, "_orchestrator", Orchestrator(), raising=False)
    monkeypatch.setattr(main, "_settings", SimpleNamespace(research_budget_seconds=30, finalize_min_budget_seconds=2), raising=False)
    with TestClient(main.app) as client:
        response = client.post("/api/ask", json={"question": "q"})
    assert response.status_code == 200
    assert response.json()["code"] == "blocked"
    assert response.json()["persisted"] is False


def test_sync_and_sse_expose_the_same_terminal_field_set(monkeypatch):
    from app.models.response import ResearchResponse

    class Graph:
        async def astream(self, initial_state, stream_mode=None, config=None):
            yield "values", {
                **initial_state,
                "report": None,
                "critique": {"verdict": "revise", "issues": []},
                "final_audit_status": "revise_exhausted",
                "delivery_status": "blocked",
                "delivery_reason": "audit blocked",
                "blocked_reason": "rewrite_budget_exhausted",
                "reason_codes": ["rewrite_budget_exhausted"],
                "errors": [],
            }

    class Orchestrator:
        async def run(self, question, domain=None, conversation_id=None):
            return ResearchResponse(
                question=question, report=None, critique={"verdict": "revise", "issues": []},
                final_audit_status="revise_exhausted", delivery_status="blocked", delivery_reason="audit blocked",
                blocked_reason="rewrite_budget_exhausted", reason_codes=["rewrite_budget_exhausted"],
                run_id="sync-terminal", persisted=False, stage="finalize",
            )

        def _ensure_graph(self):
            return Graph()

    monkeypatch.setattr(main, "_orchestrator", Orchestrator(), raising=False)
    monkeypatch.setattr(main, "_settings", SimpleNamespace(research_budget_seconds=30, stream_heartbeat_seconds=1,
                                                              graph_recursion_limit=25, finalize_min_budget_seconds=2), raising=False)
    with TestClient(main.app) as client:
        sync_payload = client.post("/api/ask", json={"question": "q"}).json()
        stream_payload = next(json.loads(line[6:]) for line in client.post("/api/ask/stream", json={"question": "q"}).text.splitlines()
                               if line.startswith("data: ") and "delivery_status" in line)
    fields = ("final_audit_status", "delivery_status", "persisted", "reason_codes", "stage", "exception_type",
              "delivery_reason", "blocked_reason", "source_switch", "partial")
    assert {key for key in fields if key in sync_payload} == set(fields)
    assert {key for key in fields if key in stream_payload} == set(fields)
    assert {sync_payload[key] for key in fields if key not in {"reason_codes"}} == {stream_payload[key] for key in fields if key not in {"reason_codes"}}
    assert sync_payload["reason_codes"] == stream_payload["reason_codes"]


def _replay_payload(evidence, claim="600519今日上涨"):
    return {
        "run_id": "p10-fixture",
        "requested_date": "2026-10-09",
        "as_of_date": "2026-10-09",
        "report": {"as_of_date": "2026-10-09", "claims": [{"claim": claim, "evidence_ids": [evidence.id]}],
                   "evidence": [evidence.model_dump()]},
        "delivery_status": "verified", "final_audit_status": "pass", "persisted": True,
        "reason_codes": [], "stage": "finalize", "elapsed_ms": 1, "exception_type": None,
        "delivery_reason": "ok", "blocked_reason": "", "source_switch": False, "partial": False,
    }


@pytest.mark.parametrize("evidence,expected_reason", [
    (EvidenceItem(id="fake", source_tool="quote", metric="change_pct", value=2, instrument="600519",
                  as_of_date="2026-10-09", retrieved_at="2026-10-09T10:00:00Z", source="forged-source",
                  authority="forged-source", completeness="complete", coverage="instrument"), "unknown_source"),
    (EvidenceItem(id="date", source_tool="quote", metric="change_pct", value=2, instrument="600519",
                  as_of_date="2026-10-08", retrieved_at="2026-10-09T10:00:00Z", source="feed", authority="exchange",
                  completeness="complete", coverage="instrument"), "stale_date"),
    (EvidenceItem(id="instrument", source_tool="quote", metric="change_pct", value=2, instrument="000001",
                  as_of_date="2026-10-09", retrieved_at="2026-10-09T10:00:00Z", source="feed", authority="exchange",
                  completeness="complete", coverage="instrument"), "unmatched_instrument"),
    (EvidenceItem(id="news", source_tool="news_search", metric="headline", value="上涨", instrument="600519",
                  as_of_date="2026-10-09", retrieved_at="2026-10-09T10:00:00Z", source="wire", authority="wire",
                  completeness="complete", coverage="instrument"), "news_mention_not_price"),
    (EvidenceItem(id="partial", source_tool="quote", metric="change_pct", value=2, instrument="600519",
                  as_of_date="2026-10-09", retrieved_at="2026-10-09T10:00:00Z", source="feed", authority="exchange",
                  completeness="partial", coverage="instrument", partial=True), "partial_only"),
])
def test_p10_replay_claim_validation_rejects_unsafe_evidence(evidence, expected_reason):
    terminal = p9._redacted_payload(_replay_payload(evidence))
    assert terminal["claims_valid"] is False
    assert expected_reason in terminal["claim_validation"][0]["reason_codes"]


def test_p10_replay_claim_validation_requires_retrieved_at_and_coverage():
    evidence = EvidenceItem(id="incomplete", source_tool="quote", metric="change_pct", value=2, instrument="600519",
                             as_of_date="2026-10-09", source="feed", authority="exchange", completeness="complete",
                             coverage="unknown")
    terminal = p9._redacted_payload(_replay_payload(evidence))
    assert terminal["claims_valid"] is False
    assert {"missing_retrieved_at", "insufficient_coverage"} <= set(terminal["claim_validation"][0]["reason_codes"])


def test_optional_failed_tool_does_not_invalidate_independent_degraded_claim():
    evidence = EvidenceItem(
        id="quote-ok", source_tool="quote", metric="change_pct", value=2,
        instrument="600519", as_of_date="2026-10-09", retrieved_at="2026-10-09T10:00:00Z",
        source="feed", authority="exchange", completeness="complete", coverage="instrument",
    )
    payload = {
        "run_id": "optional-failure",
        "requested_date": "2026-10-09",
        "as_of_date": "2026-10-09",
        "report": {
            "claims": [{"claim": "600519在2026-10-09上涨2%", "evidence_ids": [evidence.id]}],
            "evidence": [evidence.model_dump()],
        },
        "delivery_status": "degraded", "final_audit_status": "research_exhausted", "persisted": False,
        "reason_codes": ["ths_invalid_credentials"], "stage": "finalize", "elapsed_ms": 1,
        "exception_type": None, "delivery_reason": "degraded_partial_evidence", "blocked_reason": "",
        "source_switch": False, "partial": True,
        "tool_results": [
            {"status": "success", "normalized": [{"value": 2}]},
            {"status": "error", "normalized": [], "reason_codes": ["ths_invalid_credentials"]},
        ],
    }
    terminal = p9._redacted_payload(payload)
    row = {"status": "observed", "status_code": 200, "terminal": terminal}
    p9._assert_terminal(row)
    assert terminal["claims_valid"] is True
    assert terminal["evidence_count"] == 1
