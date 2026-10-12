"""脱敏回归：2026-10-10 A 股流式调查故障的关键安全契约。"""

import asyncio
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from langgraph.graph import END, START, StateGraph

from app.cache import Cache
from app.config import Settings
from app.gateway.http_client import MarketGatewayHttpClient
from app.gateway.mcp_client import MarketGatewayClient
from app.gateway.normalizer import normalize_tool_result
from app.graph.builder import critic_route_decision
from app.graph.gap_loop import (
    append_gap_steps,
    coverage_records,
    coverage_requirements,
    drop_repeat_steps,
    judge_coverage,
)
from app.graph.nodes.analysts.technical import TechnicalAnalystNode
from app.graph.nodes.critic import _code_level_issues
from app.graph.nodes.finalize import FinalizeAuditNode, resolve_delivery
from app.graph.state import ResearchState
from app.graph.tool_runtime import ToolRuntime
from app.models.evidence import Evidence
from app.models.market import NormalizedDatum, ToolResult
from app.models.research import ToolCallPlan
from app.research.reasoning import ReasoningEngine, _parse_evidence, _validate_report_references, build_evidence_aliases


def _settings(**kwargs):
    return Settings(_env_file=None, openai_api_key="acceptance-placeholder", **kwargs)


def _route(key, arguments):
    return [
        {
            "analyst": "technical",
            "budget": 1,
            "tool_calls": [{"tool_key": key, "arguments": arguments, "purpose": "incident regression"}],
        }
    ]


class _McpResult:
    is_error = False
    structured_content = {"last": 1}
    content = []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema_type, expected", [("string", "SH000001,SZ399001"), ("array", ["SH000001", "SZ399001"])]
)
async def test_incident_mcp_quote_transport_schema_preserves_canonical(schema_type, expected):
    seen = []

    class Session:
        async def call_tool(self, name, arguments=None):
            seen.append(arguments)
            return _McpResult()

    client = MarketGatewayClient(Settings(research_timeout_seconds=1))
    client.session = Session()
    client.tools = [
        SimpleNamespace(
            name="get_market_quotes", inputSchema={"type": "object", "properties": {"symbols": {"type": schema_type}}}
        )
    ]
    result = await client.call(
        "get_market_quotes", {"symbols": ["SZ399001", "SH000001"]}, deadline=time.monotonic() + 2
    )
    assert seen == [{"symbols": expected}]
    assert result.arguments == {"symbols": ["SH000001", "SZ399001"]}
    assert result.status == "success"


@pytest.mark.asyncio
async def test_incident_mcp_timeout_is_capped_by_deadline():
    class SlowSession:
        async def call_tool(self, name, arguments=None):
            await asyncio.sleep(30)

    client = MarketGatewayClient(Settings(research_timeout_seconds=30, max_retry_per_tool=1))
    client.session = SlowSession()
    started = time.monotonic()
    result = await client.call("get_ashare_sentiment", {}, deadline=started + 0.05)
    assert time.monotonic() - started < 1
    assert result.status == "error"
    assert result.normalized == []
    assert result.attempts[0]["attempt"] == 1
    assert result.attempts[0]["status"] == "timeout"


@pytest.mark.asyncio
async def test_abnormal_reasons_gets_bounded_window_and_optional_error_code():
    node = TechnicalAnalystNode(Settings(_env_file=None, research_budget_seconds=30))
    seen = []

    async def execute(tool, arguments, called_signatures, deadline=None, refresh=False):
        seen.append(arguments)
        return ToolResult(tool=tool, arguments=arguments, status="error", normalized=[], error="upstream timeout")

    node._runtime.execute = execute
    results = await node._execute_tools(
        {
            "question": "688981 异动原因",
            "requested_date": "2026-10-09",
            "planned_as_of_date": "2026-10-09",
            "route": _route("abnormal_reasons", {"symbol": "SH688981"}),
            "budget_deadline": time.monotonic() + 30,
        },
        set(),
    )
    assert seen[0]["start"] == "2026-09-09"
    assert seen[0]["end"] == "2026-10-09"
    assert results[0].status == "error"
    assert "optional_evidence_unavailable" in results[0].reason_codes


def test_incident_partial_sector_result_keeps_coverage_retry():
    critique = {
        "missing_tool_keys": ["limit_up_sectors"],
        "issues": [
            {
                "action": "research_more",
                "required_tool_keys": ["limit_up_sectors"],
                "required_coverage": {"allow_partial": False, "min_datum_count": 20},
            }
        ],
    }
    result = ToolResult(
        tool="list_limit_up_sectors",
        tool_key="limit_up_sectors",
        arguments={},
        status="partial",
        partial=True,
        normalized=[NormalizedDatum(tool="list_limit_up_sectors", metric="sector", value="x")],
    )
    requirements = coverage_requirements(critique)
    step = ToolCallPlan(tool_key="limit_up_sectors", arguments={}, purpose="existing")
    kept, _ = drop_repeat_steps([step], [result], requirements)
    retried, forced = append_gap_steps(
        kept,
        ["limit_up_sectors"],
        "- limit_up_sectors: list_limit_up_sectors",
        max_steps=8,
        max_gap_steps=3,
        requirements=requirements,
        results=[result],
    )
    assert forced == ["limit_up_sectors"]
    assert len(retried) == 1
    assert "覆盖不足重拉" in retried[0].purpose
    assert retried[0].refresh is True


def test_incident_evidence_aliases_resolve_and_unknown_alias_is_invalid():
    ledger = [Evidence(id="technical-58e74920441933cd", source_tool="quote", metric="last", value=1)]
    aliases = build_evidence_aliases(ledger)
    parsed = _parse_evidence([{"id": "E001"}], ledger, aliases)
    assert parsed[0].id == ledger[0].id
    payload = {"what_happened": "发生 E999", "why": []}
    violations = _validate_report_references(payload, {ledger[0].id})
    assert violations and "E999" in violations[0]


def test_incident_blocked_delivery_has_reason_and_no_verified_status():
    audit, delivery, reason = resolve_delivery(
        {"verdict": "research_more", "reason": "证据不足"},
        [],
        blocked_reason="research_budget_exhausted",
    )
    assert audit == "research_exhausted"
    assert delivery == "blocked"
    assert "research_budget_exhausted" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("question,symbol", [("查看600519行情", "600519"), ("查看贵州茅台行情", "600519")])
async def test_quote_parameters_are_filled_before_validation(monkeypatch, question, symbol):
    node = TechnicalAnalystNode(_settings())
    calls = []

    async def execute(tool, arguments, called_signatures, deadline=None):
        calls.append(arguments)
        return normalize_tool_result(tool, arguments, {"last": 1})

    monkeypatch.setattr(node._runtime, "execute", execute)
    output = await node({"question": question, "route": _route("quote", {}), "budget_deadline": time.monotonic() + 30})
    assert output["errors"] == []
    assert calls == [{"symbols": [symbol]}]


@pytest.mark.asyncio
async def test_unknown_company_without_symbol_keeps_structured_gap(monkeypatch):
    node = TechnicalAnalystNode(_settings())
    calls = []

    async def execute(*args, **kwargs):
        calls.append(args)
        raise AssertionError("invalid quote must not reach Gateway")

    monkeypatch.setattr(node._runtime, "execute", execute)
    output = await node(
        {"question": "查看行云科技行情", "route": _route("quote", {}), "budget_deadline": time.monotonic() + 30}
    )
    assert calls == []
    assert output["errors"] == []
    assert output["results"][0].status == "error"
    assert output["results"][0].tool_key == "quote"
    assert output["results"][0].normalized == []


@pytest.mark.asyncio
async def test_partial_research_retry_fetches_new_data(monkeypatch):
    monkeypatch.setattr("app.graph.tool_runtime.market_cache", Cache())
    node = TechnicalAnalystNode(_settings())
    calls = []

    async def gateway(tool, arguments, deadline=None):
        calls.append(tool)
        return normalize_tool_result(
            tool, arguments, {"data": [{"sector": "test", "count": 3}], "partial": len(calls) == 1}
        )

    monkeypatch.setattr(node._runtime, "_do_execute", gateway)
    state = {
        "question": "今天A股发生了什么",
        "route": _route("limit_up_sectors", {}),
        "budget_deadline": time.monotonic() + 100,
    }
    first = await node(state)
    assert first["results"][0].partial
    requirements = coverage_requirements(
        {
            "issues": [
                {
                    "action": "research_more",
                    "required_tool_keys": ["limit_up_sectors"],
                    "required_coverage": {"allow_partial": False},
                }
            ]
        }
    )
    steps, _ = drop_repeat_steps(
        [ToolCallPlan(tool_key="limit_up_sectors", arguments={}, purpose="retry")], first["results"], requirements
    )
    steps, _ = append_gap_steps(
        steps,
        ["limit_up_sectors"],
        "- limit_up_sectors: tool",
        8,
        3,
        requirements=requirements,
        results=first["results"],
    )
    assert len(steps) == 1 and steps[0].refresh
    state["results"] = first["results"]
    state["route"] = [{"analyst": "technical", "budget": 1, "tool_calls": [step.model_dump() for step in steps]}]
    second = await node(state)
    assert second["errors"] == []
    assert calls == ["list_limit_up_sectors", "list_limit_up_sectors"]
    assert second["results"][0].status == "success"


@pytest.mark.asyncio
async def test_refresh_bypasses_same_run_duplicate_guard(monkeypatch):
    """An explicit refresh remains executable even after the same signature ran."""
    monkeypatch.setattr("app.graph.tool_runtime.market_cache", Cache())
    runtime = ToolRuntime(_settings())
    calls = []

    async def gateway(tool, arguments, deadline=None):
        calls.append(arguments)
        return normalize_tool_result(tool, arguments, {"data": [{"value": len(calls)}]})

    monkeypatch.setattr(runtime, "_do_execute", gateway)
    called = set()
    first = await runtime.execute("list_limit_up_sectors", {}, called)
    refreshed = await runtime.execute("list_limit_up_sectors", {}, called, refresh=True)

    assert first.status == refreshed.status == "success"
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason,extra,config",
    [
        ("time_budget_insufficient", {"budget_deadline": "soon"}, {"research_round_min_remaining_seconds": 60}),
        ("tool_call_budget_insufficient", {"results": [{"tool": "x"}]}, {"max_tool_calls": 1}),
        ("no_report_to_revise", {"report": None}, {}),
        ("no_executable_gap_steps", {"gap_key_decisions": [{"key": "quote", "decision": "dropped"}]}, {}),
        ("research_budget_exhausted", {"research_round_count": 1}, {}),
    ],
)
async def test_graph_terminal_reason_matches_route(reason, extra, config, caplog):
    settings = _settings(max_research_rounds=1, **config)
    state_update = {
        "report": {"what_happened": "test"},
        "critique": {"verdict": "research_more", "reason": "needs quote"},
        "gap_key_decisions": [{"key": "quote", "decision": "kept"}],
    }
    state_update.update({k: v for k, v in extra.items() if k != "budget_deadline"})
    graph = StateGraph(ResearchState)
    graph.add_node("audit", lambda state: state_update)
    graph.add_node("finalize_audit", FinalizeAuditNode(settings))
    graph.add_edge(START, "audit")
    graph.add_conditional_edges(
        "audit", lambda state: critic_route_decision(state, settings), {"finalize_audit": "finalize_audit"}
    )
    graph.add_edge("finalize_audit", END)
    deadline = time.monotonic() + (1 if extra.get("budget_deadline") == "soon" else 300)
    with caplog.at_level(logging.INFO, logger="app.graph.run_log"):
        final = await graph.compile().ainvoke({"question": "test", "budget_deadline": deadline})
    assert final["delivery_status"] == "blocked" and final["report"] is None
    assert final["blocked_reason"] == reason
    assert reason in final["delivery_reason"]
    events = [json.loads(record.getMessage()) for record in caplog.records if record.name == "app.graph.run_log"]
    assert {event["blocked_reason"] for event in events if event["event"] in {"route", "finalize"}} == {reason}


@pytest.mark.asyncio
async def test_http_deadline_caps_complete_chunked_response():
    async def handler(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n")
            await writer.drain()
            for chunk in (b"{", b'"last"', b":", b"1", b"}"):
                await asyncio.sleep(0.04)
                writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                await writer.drain()
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    gateway = MarketGatewayHttpClient(_settings(max_retry_per_tool=0))
    gateway.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False)
    try:
        started = time.monotonic()
        result = await gateway.call("get_market_quotes", {"symbols": ["600519"]}, deadline=started + 0.09)
        elapsed = time.monotonic() - started
    finally:
        await gateway.close()
        server.close()
        await server.wait_closed()
    assert result.status == "error"
    assert elapsed < 0.18


@pytest.mark.asyncio
async def test_alias_in_claim_text_resolves_and_unknown_remains_invalid():
    ledger = [Evidence(id="technical-58e74920441933cd", source_tool="get_market_quotes", metric="last", value=1)]
    engine = ReasoningEngine(_settings())

    async def create(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "market_state": "mixed",
                                "what_happened": "价格见 E001",
                                "confidence": "medium",
                                "claims": [{"claim": "价格见 E001", "evidence_ids": ["E001"], "claim_type": "fact"}],
                                "evidence": [{"id": "E001"}],
                            }
                        )
                    )
                )
            ]
        )

    engine.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    report = await engine.reason("test", [], ledger)
    assert report.claims[0].evidence_ids == [ledger[0].id]
    assert ledger[0].id in report.claims[0].claim
    assert report.evidence_violations == []
    assert _validate_report_references({"what_happened": "价格见 E999"}, {ledger[0].id})


@pytest.mark.parametrize(
    "text,blocked",
    [
        ("行云科技尚未验证，无法确认其行情。", False),
        ("行云科技尚未验证。行云科技今日领涨。", True),
        ("行云科技不存在。", True),
    ],
)
def test_unverified_entity_qualification_is_sentence_scoped(text, blocked):
    issues = _code_level_issues({"what_happened": text, "unverified_entities": ["行云科技"]})
    assert bool(issues) is blocked


@pytest.mark.asyncio
async def test_gateway_logs_redact_query_for_mcp_and_http(caplog):
    private_query = "PRIVATE_USER_QUERY_ACCEPTANCE_SENTINEL"
    mcp = MarketGatewayClient(_settings())

    async def call_tool(*args, **kwargs):
        return SimpleNamespace(is_error=False, structured_content={"matches": 1})

    mcp.session = SimpleNamespace(call_tool=call_tool)
    http = MarketGatewayHttpClient(_settings(max_retry_per_tool=0))
    http.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"matches": 1})),
        base_url="https://gateway.test",
    )
    try:
        with caplog.at_level(logging.INFO):
            await mcp.call("search_stocks", {"q": private_query})
            await http.call("search_stocks", {"q": private_query})
    finally:
        await http.close()
    gateway_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name in {"app.gateway.mcp_client", "app.gateway.http_client"}
    )
    assert private_query not in gateway_logs


def test_local_display_truncation_keeps_source_coverage():
    result = ToolResult(
        tool="list_limit_up_sectors",
        tool_key="limit_up_sectors",
        arguments={},
        status="success",
        normalized=[NormalizedDatum(tool="list_limit_up_sectors", metric="sector", value=i) for i in range(250)],
    )
    ToolRuntime(_settings()).truncate(result)
    assert result.partial and result.source_datum_count == 250 and result.source_partial is False
    record = coverage_records([result])[0]
    assert judge_coverage(record, {"limit_up_sectors"}, {"allow_partial": False, "min_datum_count": 220}).satisfies


def test_redacted_incident_fixture_is_exercised():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "incident_ashare_stream_fix.json").read_text("utf-8"))
    quote_results = [
        normalize_tool_result(row["tool"], row["arguments"], None, error=row["gateway_error"])
        for row in fixture["quote_attempts"]
    ]
    assert all(result.status == "error" and result.normalized == [] for result in quote_results)
    assert quote_results[0].arguments != quote_results[1].arguments
    partial = fixture["partial"]
    sector_result = ToolResult(
        tool=partial["tool"],
        tool_key="limit_up_sectors",
        arguments={},
        status=partial["status"],
        partial=True,
        normalized=[
            NormalizedDatum(tool=partial["tool"], metric="sector", value=i) for i in range(partial["datum_count"])
        ],
    )
    assert not judge_coverage(
        coverage_records([sector_result])[0], {"limit_up_sectors"}, {"allow_partial": False}
    ).satisfies
    violations = _validate_report_references({"what_happened": " ".join(fixture["forged_evidence_ids"])}, set())
    assert all(any(alias in violation for violation in violations) for alias in fixture["forged_evidence_ids"])
    _, delivery, _ = resolve_delivery(
        {"verdict": "research_more", "reason": "missing evidence"}, blocked_reason="research_budget_exhausted"
    )
    assert delivery == fixture["expected_delivery_status"]
    assert fixture["expected_persistence"] is False
