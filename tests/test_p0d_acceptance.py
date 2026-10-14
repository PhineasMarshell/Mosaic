"""P0-D: real nodes/adapters, with local Gateway/MCP and LLM response doubles.

Mocks replace upstream connection/request methods and LLM completions only;
real planning, canonical arguments, cache, audit, route and delivery run here.
This does not verify external credentials, live sources or real model quality.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import app.main as main
from app.cache import Cache
from app.config import Settings
from app.gateway.http_client import MarketGatewayHttpClient
from app.gateway.mcp_client import MarketGatewayClient
from app.graph import run_log
from app.graph.builder import critic_route_decision
from app.graph.nodes.analysts.technical import TechnicalAnalystNode
from app.graph.nodes.critic import CriticNode
from app.graph.nodes.finalize import FinalizeAuditNode
from app.graph.nodes.supervisor import SupervisorNode
from app.graph.tool_runtime import ToolRuntime
from app.models.evidence import Evidence
from app.models.market import NormalizedDatum, ToolResult
from app.models.response import build_response_from_state
from app.research.reasoning import ReasoningEngine

FIXTURE = json.loads((Path(__file__).parent / "fixtures/incident_ashare_stream_fix.json").read_text("utf-8"))


def settings(**kwargs):
    return Settings(_env_file=None, openai_api_key="local-double", **kwargs)


def llm(payload, prompts=None):
    async def create(**kwargs):
        if prompts is not None:
            prompts.append("\n".join(m["content"] for m in kwargs["messages"]))
        content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


@pytest.mark.asyncio
@pytest.mark.parametrize("status,count", [("partial", 201), ("success", 0), ("success", 1)])
async def test_supervisor_research_more_refreshes_partial_empty_and_insufficient(monkeypatch, status, count):
    monkeypatch.setattr("app.graph.tool_runtime.market_cache", Cache())
    config = settings()
    analyst = TechnicalAnalystNode(config)
    tool = FIXTURE["partial"]["tool"]
    upstream_calls = []

    async def fetch(name, arguments, deadline=None):
        upstream_calls.append(arguments)
        first = len(upstream_calls) == 1
        return ToolResult(
            tool=name, arguments=arguments, status=status if first else "success",
            partial=first and status == "partial",
            normalized=[NormalizedDatum(tool=name, metric="sector", value=i) for i in range(count if first else 250)],
        )

    monkeypatch.setattr(analyst._runtime, "_do_execute", fetch)
    state = {
        "run_id": f"{FIXTURE['run_id']}-refresh-{status}-{count}", "question": "600519板块覆盖核查", "domain": "a_share",
        "budget_deadline": time.monotonic() + 120,
        "route": [{"analyst": "technical", "budget": 1, "tool_calls": [{"tool_key": "limit_up_sectors", "arguments": {}}]}],
    }
    first = await analyst(state)
    state.update(results=first["results"], critique={
        "verdict": "research_more", "missing_tool_keys": ["limit_up_sectors"],
        "issues": [{"kind": "missing_evidence", "action": "research_more", "required_tool_keys": ["limit_up_sectors"],
                    "required_coverage": {"min_datum_count": 220, "allow_partial": False}}],
    })
    supervisor = SupervisorNode(config)
    supervisor.client = llm({"intent": {"domain": "a_share", "question": state["question"]}, "steps": [
        {"tool_key": "limit_up_sectors", "arguments": {}, "purpose": "recheck"},
    ]})
    planned = await supervisor(state)
    assert planned.get("errors", []) == []
    assert not planned["finalize_after_gap"]
    calls = [tc for assignment in planned["route"] for tc in assignment["tool_calls"]]
    assert len(calls) == 1 and calls[0]["refresh"]
    state.update(planned)
    second = await analyst(state)
    assert second["errors"] == []
    assert len(upstream_calls) == 2 and upstream_calls[0] == upstream_calls[1]
    assert second["results"][0].source_partial is False
    assert tool == second["results"][0].tool


@pytest.mark.asyncio
@pytest.mark.parametrize("node_budget", [1, 300])
async def test_every_analyst_request_stays_inside_absolute_deadline(monkeypatch, node_budget):
    analyst = TechnicalAnalystNode(settings(research_budget_seconds=node_budget))
    absolute = time.monotonic() + 0.2
    issued = []

    async def execute(tool, arguments, called_signatures, deadline=None):
        issued.append(deadline)
        await asyncio.sleep(0.04)
        return ToolResult(tool=tool, arguments=arguments, status="success")

    monkeypatch.setattr(analyst._runtime, "execute", execute)
    output = await analyst({"budget_deadline": absolute, "route": [{"analyst": "technical", "budget": 2, "tool_calls": [
        {"tool_key": "limit_up_sectors", "arguments": {}}, {"tool_key": "limit_up_count", "arguments": {}},
    ]}]})
    assert output["errors"] == []
    assert len(issued) == 2 and all(deadline <= absolute for deadline in issued)


@pytest.mark.asyncio
async def test_deadline_includes_connection_and_cleans_cancelled_session(monkeypatch):
    monkeypatch.setattr("app.graph.tool_runtime.market_cache", Cache())
    closed = []
    requested = []

    class SlowConnect:
        def __init__(self, config):
            pass

        async def __aenter__(self):
            await asyncio.sleep(30)

        async def __aexit__(self, *args):
            closed.append(True)

        async def call(self, *args, **kwargs):
            requested.append(True)

    runtime = ToolRuntime(settings())
    monkeypatch.setattr(runtime, "_gateway_class", lambda: SlowConnect)
    started = time.monotonic()
    async with runtime.gateway_session():
        result = await runtime.execute("get_market_quotes", {"symbols": ["000300"]}, set(), deadline=started + 0.05)
    assert time.monotonic() - started < 0.5
    assert result.status == "error" and not result.normalized
    assert closed == [True] and requested == []


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["E001", "E999"])
async def test_reasoning_alias_only_prompt_and_chinese_adjacent_reference(alias):
    evidence = Evidence(id="technical-58e74920441933cd", source_tool="get_market_quotes", metric="last", value=1)
    prompts = []
    engine = ReasoningEngine(settings())
    engine.client = llm({"market_state": "mixed", "what_happened": f"引用{alias}说明", "evidence": [{"id": alias}]}, prompts)
    report = await engine.reason("核查", [], [evidence], history_context=f"上一轮引用 {evidence.id}")
    assert evidence.id not in prompts[0] and "E001" in prompts[0]
    if alias == "E001":
        assert report.evidence[0].id == evidence.id
        assert evidence.id in report.what_happened and not report.evidence_violations
    else:
        assert report.evidence_violations


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["E001", "E999"])
async def test_critic_aliases_resolve_before_validation_and_unknown_blocks(alias):
    evidence = Evidence(id="technical-58e74920441933cd", source_tool="get_market_quotes", metric="last", value=1)
    node = CriticNode(settings())
    prompts = []
    node.client = llm({"issues": [], "reason": f"已审查{alias}说明"}, prompts)
    output = await node({"report": {"what_happened": f"引用 {evidence.id}"}, "evidence": [evidence], "domain": "a_share"})
    assert evidence.id not in prompts[0] and "E001" in prompts[0]
    critique = output["critique"]
    if alias == "E001":
        assert evidence.id in critique.reason and "E001" not in critique.reason
        assert critique.verdict == "pass"
    else:
        assert critique.verdict != "pass"
        assert any("invalid_reference" in issue.reason_codes for issue in critique.issues)


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict,code", [
    ("pass", ""), ("research_more", "research_budget_exhausted"),
    ("revise", "rewrite_budget_exhausted"), ("error", "critic_error"),
])
async def test_redacted_terminal_events_keep_run_id_issues_and_consistent_code(verdict, code, caplog, monkeypatch):
    monkeypatch.setattr(logging.getLogger("app"), "propagate", True)
    secret = "PRIVATE_QUERY_P0D_SENTINEL"
    issue = {"kind": "missing_evidence", "severity": "high", "action": "research_more",
             "claim": secret, "rationale": secret, "reason_codes": ["unknown_date"],
             "required_coverage": {"arguments": {"query": secret, "q": secret, "other": secret}}}
    state = {"run_id": FIXTURE["run_id"] + "-" + verdict, "question": secret, "report": {"what_happened": "草稿"},
             "critique": {"verdict": verdict, "reason": secret, "issues": [issue] if verdict != "pass" else []},
             "research_round_count": 1, "rewrite_count": 1}
    config = settings(max_research_rounds=1, max_rewrites=1)
    if verdict == "pass":
        state["report"] = {"what_happened": "暂无可核验数据", "claims": [
            {"claim": "暂无可核验数据", "claim_type": "structure", "evidence_ids": []},
        ]}
    with caplog.at_level(logging.INFO, logger="app.graph.run_log"):
        run_log.log_run_start(state)
        run_log.log_critic(state, state["critique"])
        route = critic_route_decision(state, config)
        if route == "finalize_audit":
            state.update(await FinalizeAuditNode(config)(state))
        response = build_response_from_state(state, secret)
        run_log.log_delivery(response.model_dump(), delivery_status=response.delivery_status, persisted=False, sink="sync")
    events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "app.graph.run_log"]
    assert secret not in json.dumps(events)
    assert all(e["run_id"] == state["run_id"] for e in events)
    assert response.blocked_reason == code
    if code:
        assert code in response.delivery_reason
        assert {e["blocked_reason"] for e in events if e["event"] in {"route", "finalize", "delivery"}} == {code}
        assert events[-1]["issues"][0]["reason_codes"] == ["unknown_date"]
        assert response.report is None


@pytest.mark.asyncio
async def test_critic_parse_error_emits_redacted_replayable_event(caplog, monkeypatch):
    monkeypatch.setattr(logging.getLogger("app"), "propagate", True)
    node = CriticNode(settings())
    node.client = llm("PRIVATE_QUERY_INVALID_JSON")
    with caplog.at_level(logging.INFO, logger="app.graph.run_log"):
        output = await node({"run_id": FIXTURE["run_id"] + "-parse-error", "report": {"what_happened": "草稿"}})
    events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "app.graph.run_log"]
    assert output["critique"].verdict == "error"
    assert any(e["event"] == "critic" and e["verdict"] == "error" and e["run_id"] == FIXTURE["run_id"] + "-parse-error" for e in events)
    assert "PRIVATE_QUERY_INVALID_JSON" not in json.dumps(events)


@pytest.mark.asyncio
async def test_finalize_exception_does_not_echo_sensitive_error(monkeypatch):
    secret = "PRIVATE_FINALIZE_QUERY"

    def fail(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr("app.graph.nodes.finalize.infer_blocked_reason", fail)
    output = await FinalizeAuditNode(settings())({"critique": {"verdict": "research_more"}})

    assert output["blocked_reason"] == "audit_error"
    assert output["delivery_reason"] == "audit_error: finalize audit failed"
    assert output["errors"] == ["finalize audit failed"]
    assert secret not in json.dumps(output)


@pytest.mark.asyncio
async def test_httpx_url_logs_do_not_echo_query_at_info(caplog):
    secret = "PRIVATE_QUERY_IN_HTTPX_URL"
    gateway = MarketGatewayHttpClient(settings(max_retry_per_tool=0))
    gateway.client = httpx.AsyncClient(base_url="https://gateway.test", transport=httpx.MockTransport(
        lambda request: httpx.Response(422, json={"detail": secret}),
    ))
    try:
        with caplog.at_level(logging.INFO, logger="httpx"):
            result = await gateway.call("search_stocks", {"q": secret})
    finally:
        await gateway.close()
    assert result.status == "error"
    assert secret not in "\n".join(r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("schema_type", ["string", "array"])
async def test_fixture_quote_attempts_and_both_timeout_tools_replay(schema_type):
    calls = []

    async def call_tool(name, arguments=None):
        calls.append((name, arguments))
        if name in FIXTURE["timeouts"]:
            await asyncio.sleep(30)
        return SimpleNamespace(is_error=False, structured_content={"last": 1})

    gateway = MarketGatewayClient(settings(research_timeout_seconds=30, max_retry_per_tool=2))
    gateway.session = SimpleNamespace(call_tool=call_tool)
    gateway.tools = [{"name": "get_market_quotes", "inputSchema": {"properties": {"symbols": {"type": schema_type}}}}]
    for row in FIXTURE["quote_attempts"]:
        result = await gateway.call(row["tool"], row["arguments"], deadline=time.monotonic() + 1)
        expected = sorted(row["arguments"]["symbols"])
        assert result.status == "success"
        assert result.arguments == {"symbols": expected}
        from app.gateway.arguments import quote_transport_symbols

        encoded = quote_transport_symbols(expected)
        assert calls[-1][1]["symbols"] == (encoded if schema_type == "array" else ",".join(encoded))
    for tool in FIXTURE["timeouts"]:
        started = time.monotonic()
        before = len(calls)
        result = await gateway.call(tool, {}, deadline=started + 0.03)
        assert time.monotonic() - started < 0.5
        assert result.status == "error" and not result.normalized
        assert len(calls) == before + 1


@pytest.mark.asyncio
async def test_partial_quote_without_explicit_coverage_retains_target_for_refresh(monkeypatch):
    supervisor = SupervisorNode(settings())
    question = "600519行情"
    supervisor.client = llm({"intent": {"domain": "a_share", "question": question}, "steps": [
        {"tool_key": "quote", "arguments": {"symbols": ["600519"]}, "purpose": "retry"},
    ]})
    output = await supervisor({
        "question": question, "domain": "a_share",
        "critique": {"verdict": "research_more", "missing_tool_keys": ["quote"]},
        "results": [ToolResult(tool="get_market_quotes", tool_key="quote", arguments={"symbols": ["600519"]},
                               status="partial", partial=True, normalized=[NormalizedDatum(tool="get_market_quotes", metric="last", value=1)])],
    })
    assert output.get("errors", []) == []
    calls = [tc for assignment in output["route"] for tc in assignment["tool_calls"]]
    assert {call["tool_key"] for call in calls} == {"iwencai_market", "quote"}
    quote_call = next(call for call in calls if call["tool_key"] == "quote")
    assert quote_call["refresh"] and quote_call["arguments"] == {"symbols": ["600519"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", ["ASML今日订单增长", "地方补贴今日加码", "C50信贷今日增加"])
async def test_external_current_facts_still_block_model_pass(claim):
    critic = CriticNode(settings())
    critic.client = llm({"issues": [], "reason": "pass"})
    output = await critic({"domain": "a_share", "report": {"what_happened": claim, "claims": [
        {"claim": claim, "claim_type": "fact", "evidence_ids": []},
    ]}, "evidence": []})
    assert output.get("errors", []) == []
    assert output["critique"].verdict != "pass"
    assert any(issue.severity == "high" for issue in output["critique"].issues)


@pytest.mark.asyncio
async def test_real_critic_research_more_routes_to_supervisor(caplog, monkeypatch):
    monkeypatch.setattr(logging.getLogger("app"), "propagate", True)
    node = CriticNode(settings())
    node.client = llm({"issues": [{"kind": "missing_evidence", "action": "research_more", "required_tool_keys": ["quote"],
                                  "required_coverage": {"allow_partial": False}}], "reason": "需补行情"})
    state = {"run_id": FIXTURE["run_id"] + "-research-allowed", "domain": "a_share",
             "report": {"what_happened": "草稿"}, "budget_deadline": time.monotonic() + 120}
    with caplog.at_level(logging.INFO, logger="app.graph.run_log"):
        state.update(await node(state))
        route = critic_route_decision(state, settings())
    assert state.get("errors", []) == []
    assert state["critique"].verdict == "research_more" and route == "supervisor"
    events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "app.graph.run_log"]
    assert events[-1]["reason"] == "research_allowed" and not events[-1]["blocked_reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("sink", ["sync", "sse"])
async def test_api_blocked_reason_and_replay_logs_match_route_and_finalize(sink, monkeypatch, caplog):
    monkeypatch.setattr(logging.getLogger("app"), "propagate", True)
    config = settings(max_research_rounds=1)
    secret = "PRIVATE_API_QUERY_P0D"
    state = {"run_id": FIXTURE["run_id"] + "-" + sink, "question": secret, "research_round_count": 1,
             "report": {"what_happened": "草稿"}, "critique": {"verdict": "research_more", "reason": secret,
             "issues": [{"kind": "missing_evidence", "severity": "high", "action": "research_more",
                         "claim": secret, "rationale": secret, "reason_codes": ["unknown_date"]}]}}

    class Graph:
        async def astream(self, *args, **kwargs):
            yield "values", state

    async def run(*args, **kwargs):
        return build_response_from_state(state, secret)

    async def persist(*args, **kwargs):
        raise AssertionError("blocked must not enter persistence")

    monkeypatch.setattr(main, "_get_settings", lambda: config)
    monkeypatch.setattr(main, "_get_orchestrator", lambda: SimpleNamespace(run=run, _ensure_graph=lambda: Graph()))
    monkeypatch.setattr(main, "persist_research", persist)
    with caplog.at_level(logging.INFO):
        assert critic_route_decision(state, config) == "finalize_audit"
        state.update(await FinalizeAuditNode(config)(state))
        if sink == "sync":
            response = await main.ask({"question": secret, "domain": "a_share"})
            assert response.status_code == 200
            body = json.loads(response.body)
        else:
            frames = [frame async for frame in main._stream_research(secret, "a_share")]
            results = [frame for frame in frames if "event: result" in frame]
            body = json.loads(next(line[6:] for line in results[-1].splitlines() if line.startswith("data: ")))
    assert body["report"] is None
    assert body["delivery_status"] == FIXTURE["expected_delivery_status"]
    assert body["persisted"] == FIXTURE["expected_persistence"]
    assert body["blocked_reason"] == "research_budget_exhausted"
    assert body["blocked_reason"] in body["delivery_reason"]
    events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "app.graph.run_log"]
    assert secret not in "\n".join(r.getMessage() for r in caplog.records)
    terminal = [e for e in events if e["event"] in {"route", "finalize", "delivery"}]
    assert {e["blocked_reason"] for e in terminal} == {body["blocked_reason"]}
    assert {e["run_id"] for e in terminal} == {state["run_id"]}
    assert terminal[-1]["issues"][0]["reason_codes"] == ["unknown_date"]
