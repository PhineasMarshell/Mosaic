"""P14: strict verified delivery and negative evidence gates."""

from __future__ import annotations

import asyncio
import json
import types

import pytest
from fastapi.testclient import TestClient

from app.agent.evidence_gate import match_claims_to_evidence
from app.agent.persistence import persist_research
from app.config import Settings
from app.graph.nodes.finalize import FinalizeAuditNode
from app.memory.storage import MarketMemory
from app.models.evidence import Evidence
from app.models.response import MarketIntelligence, build_response_from_state
from app.research.iwencai import normalize_announcements


def _quote(**overrides) -> Evidence:
    values = {
        "id": "quote-verified-001",
        "source_tool": "internal_iwencai_market_query",
        "tool_key": "quote",
        "operation_id": "internal_iwencai_market_query",
        "metric": "close",
        "value": 1488.0,
        "instrument": "SH600519",
        "instrument_name": "贵州茅台",
        "as_of_date": "2026-10-09",
        "retrieved_at": "2026-10-14T01:00:00Z",
        "source": "同花顺问财",
        "authority": "同花顺问财",
        "completeness": "complete",
        "coverage": "instrument",
        "status": "success",
        "partial": False,
        "result_partial": False,
    }
    values.update(overrides)
    return Evidence(**values)


def _state(evidence: list[Evidence], *, claim: str = "贵州茅台(600519)在2026-10-09收盘价为1488元") -> dict:
    report = MarketIntelligence(
        requested_date="2026-10-09",
        as_of_date="2026-10-09",
        market_state="窄行情事实",
        state_label="NEUTRAL",
        what_happened=claim,
        claims=[{"claim": claim, "claim_type": "fact", "evidence_ids": [evidence[0].id]}],
        evidence=[item.model_dump() for item in evidence],
        source=["同花顺问财"],
        coverage=["instrument"],
    )
    return {
        "question": "SH600519 2026-10-09收盘价",
        "domain": "a_share",
        "requested_date": "2026-10-09",
        "planned_as_of_date": "2026-10-09",
        "as_of_date": "2026-10-09",
        "market_closed": False,
        "report": report,
        "evidence": evidence,
        "results": [],
        "critique": {"verdict": "pass", "reason": "complete evidence"},
        "errors": [],
    }


@pytest.mark.asyncio
async def test_complete_graph_state_reaches_verified_and_persists_all_targets(tmp_path):
    evidence = [_quote()]
    state = _state(evidence)
    finalized = await FinalizeAuditNode(Settings(_env_file=None))(state)
    assert finalized["final_audit_status"] == "pass"
    assert finalized["delivery_status"] == "verified"

    result = build_response_from_state({**state, **finalized}, question=state["question"], conversation_id="p14")
    assert result.final_audit_status == "pass"
    assert result.delivery_status == "verified"
    assert result.report is not None
    memory = MarketMemory(db_path=tmp_path / "memory.db")
    assert await persist_research(result, result.question, "p14", memory=memory) is True
    assert memory.conn.execute("SELECT COUNT(*) FROM research_records").fetchone()[0] == 1
    assert memory.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
    assert memory.get_daily_state() is not None


@pytest.mark.asyncio
async def test_real_graph_nodes_with_deterministic_complete_tool_result(monkeypatch):
    from app.graph.builder import build_graph
    from app.graph.nodes import critic as critic_module
    from app.graph.nodes import reasoning as reasoning_module
    from app.graph.nodes import supervisor as supervisor_module
    from app.models.market import NormalizedDatum, ToolResult
    from app.models.response import ClaimEvidence

    plan = json.dumps({
        "intent": {"domain": "a_share", "task": "market_summary", "time_scope": "2026-10-09", "question": "quote"},
        "steps": [{"tool_key": "iwencai_market", "arguments": {"query": "SH600519 2026-10-09 收盘价"}, "purpose": "close"}],
    })

    class Completions:
        def __init__(self, payload):
            self.payload = payload

        async def create(self, **_kwargs):
            message = types.SimpleNamespace(content=self.payload)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    original_supervisor_init = supervisor_module.SupervisorNode.__init__
    def supervisor_init(self, settings):
        original_supervisor_init(self, settings)
        self.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=Completions(plan)))
    monkeypatch.setattr(supervisor_module.SupervisorNode, "__init__", supervisor_init)

    original_reasoning_init = reasoning_module.ReasoningNode.__init__
    async def deterministic_report(**kwargs):
        ledger = kwargs["evidence"]
        return MarketIntelligence(
            requested_date="2026-10-09", as_of_date="2026-10-09", market_state="窄行情事实",
            what_happened="贵州茅台(600519)在2026-10-09收盘价为1488元",
            claims=[ClaimEvidence(claim="贵州茅台(600519)在2026-10-09收盘价为1488元",
                                 evidence_ids=[ledger[0].id], claim_type="fact")],
        )
    def reasoning_init(self, settings, **kwargs):
        original_reasoning_init(self, settings, **kwargs)
        self._engine = types.SimpleNamespace(reason=deterministic_report)
    monkeypatch.setattr(reasoning_module.ReasoningNode, "__init__", reasoning_init)

    original_critic_init = critic_module.CriticNode.__init__
    def critic_init(self, settings):
        original_critic_init(self, settings)
        self.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=Completions(
            json.dumps({"verdict": "pass", "reason": "direct fact verified", "issues": []}),
        )))
    monkeypatch.setattr(critic_module.CriticNode, "__init__", critic_init)

    async def complete_tool(self, tool_name, arguments, called_signatures, deadline=None):
        return ToolResult(
            tool=tool_name, operation_id=tool_name, tool_key="iwencai_market", arguments=arguments,
            status="success", partial=False, retrieved_at="2026-10-14T01:00:00Z",
            authority="同花顺问财", completeness="complete", coverage="instrument",
            normalized=[NormalizedDatum(
                tool=tool_name, domain="a_share", instrument="SH600519", instrument_name="贵州茅台",
                metric="收盘价[2026-10-09]", value=1488.0, timestamp="2026-10-09",
                as_of_date="2026-10-09", source="同花顺问财", authority="同花顺问财",
                retrieved_at="2026-10-14T01:00:00Z", completeness="complete",
                instrument_scope="instrument", coverage="instrument",
            )],
        )
    monkeypatch.setattr("app.graph.tool_runtime.ToolRuntime.execute", complete_tool)
    monkeypatch.setattr("app.graph.tool_runtime.ToolRuntime.truncate", lambda self, result: None)

    state = await build_graph(Settings(_env_file=None)).ainvoke({
        "question": "SH600519 2026-10-09收盘价", "domain": "a_share",
        "requested_date": "2026-10-09", "planned_as_of_date": "2026-10-09",
        "market_closed": False,
    })
    assert state["critique"].verdict == "pass"
    assert state["final_audit_status"] == "pass"
    assert state["delivery_status"] == "verified"
    result = build_response_from_state(state, question=state["question"])
    assert result.final_audit_status == "pass"
    assert result.delivery_status == "verified"


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"completeness": "unknown"}, "unknown_completeness"),
        ({"coverage": "unknown"}, "unknown_coverage"),
        ({"partial": True}, "partial_only"),
        ({"result_partial": True}, "partial_only"),
        ({"as_of_date": "2026-10-08"}, "stale_date"),
        ({"instrument": "SZ000001"}, "unmatched_instrument"),
        ({"source": None}, "missing_source"),
        ({"retrieved_at": None}, "missing_retrieved_at"),
    ],
)
def test_strict_provenance_negative_gate(overrides, expected):
    evidence = [_quote(**overrides)]
    claim = "贵州茅台(600519)在2026-10-09收盘价为1488元"
    rows = match_claims_to_evidence(
        {"claims": [{"claim": claim, "claim_type": "fact", "evidence_ids": [evidence[0].id]}]},
        evidence,
        expected_date="2026-10-09",
        requested_date="2026-10-09",
        expected_domain="a_share",
        strict_provenance=True,
    )
    assert not rows[0]["supported"]
    assert expected in rows[0]["reason_codes"]


@pytest.mark.parametrize(
    "claim",
    [
        "贵州茅台(600519)上涨是因为资金流入",
        "贵州茅台(600519)在市场排名第一",
        "贵州茅台(600519)相比其他股票更强",
        "2026-10-09全市场所有股票上涨",
    ],
)
def test_single_price_does_not_support_strong_inference(claim):
    evidence = [_quote()]
    rows = match_claims_to_evidence(
        {"claims": [{"claim": claim, "claim_type": "inference", "evidence_ids": [evidence[0].id]}]},
        evidence,
        expected_date="2026-10-09",
        requested_date="2026-10-09",
        expected_domain="a_share",
    )
    assert not rows[0]["supported"]
    assert "inference_scope_exceeded" in rows[0]["reason_codes"] or "missing_direction_metric" in rows[0]["reason_codes"]


def test_forged_reference_and_authority_conflict_block_verified():
    evidence = [_quote()]
    state = _state(evidence)
    state["report"].claims[0].evidence_ids = ["forged-evidence"]
    out = asyncio.run(FinalizeAuditNode(Settings(_env_file=None))(state))
    assert out["delivery_status"] == "blocked"
    assert out["report"] is None

    state = _state(evidence)
    state["report"].entity_conflicts = [{
        "name": "贵州茅台", "conflict": "authoritative mismatch", "source": "master",
        "as_of": "2026-10-09", "authority": "authoritative",
    }]
    out = asyncio.run(FinalizeAuditNode(Settings(_env_file=None))(state))
    assert out["delivery_status"] == "blocked"
    assert "authority_conflict" in out["reason_codes"]


def test_announcement_month_requires_real_code_and_date_and_never_price_claim():
    payload = json.dumps({"status_code": 0, "data": [{
        "channel": "announcement", "title": "贵州茅台公告", "publish_date": "2026-10-12",
        "stock_infos": [{"code": "600519", "name": "贵州茅台"}], "summary": "公告事实",
    }]}, ensure_ascii=False)
    result = normalize_announcements(payload, {"query": "600519 2026年10月公告", "size": 10})
    assert result.status == "success"
    datum = result.normalized[0]
    assert datum.instrument == "600519" and datum.as_of_date == "2026-10-12"
    assert datum.as_of_date.startswith("2026-10")

    wrong_month = payload.replace("2026-10-12", "2026-09-30")
    assert normalize_announcements(wrong_month, {"query": "600519 2026年10月公告", "size": 10}).status == "error"
    wrong_code = payload.replace("600519", "000001")
    assert normalize_announcements(wrong_code, {"query": "600519 2026年10月公告", "size": 10}).status == "error"


def test_sync_and_sse_use_same_verified_state_and_one_terminal_event(monkeypatch):
    from app import main

    state = _state([_quote()])
    state.update({"run_id": "p14-shared", "final_audit_status": "pass", "delivery_status": "verified"})

    class Graph:
        async def astream(self, *_args, **_kwargs):
            yield "values", state

    class Orchestrator:
        async def run(self, question, domain=None, conversation_id=None):
            return build_response_from_state(dict(state), question=question, conversation_id=conversation_id)

        def _ensure_graph(self):
            return Graph()

    async def persist(result, question, conversation_id, **kwargs):
        result.persisted = True
        return True

    monkeypatch.setattr(main, "_get_orchestrator", lambda: Orchestrator())
    monkeypatch.setattr(main, "persist_research", persist)
    monkeypatch.setattr(main, "_settings", types.SimpleNamespace(
        research_budget_seconds=30, stream_heartbeat_seconds=5, graph_recursion_limit=25,
    ), raising=False)

    with TestClient(main.app) as client:
        sync = client.post("/api/ask", json={"question": state["question"]}).json()
        stream = client.post("/api/ask/stream", json={"question": state["question"]}).text
    events = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: ")]
    terminal = [item for item in events if item.get("terminal") is True]
    assert len(terminal) == 1
    sse = terminal[0]
    for field in ("status", "code", "final_audit_status", "delivery_status", "reason_codes", "report", "persisted"):
        assert sync[field] == sse[field]
    assert sync["report"]["claims"] == sse["report"]["claims"]
    assert sync["report"]["evidence"] == sse["report"]["evidence"]
    assert sync["status"] == "verified"
    assert sync["code"] == "ok"
    assert sync["persisted"] is True
