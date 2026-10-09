"""Stage 1 regressions: quote contract, plan deduplication and nested args."""

import pytest

from app.agent.prompts_graph import PLANNER_PROMPT
from app.config import Settings
from app.gateway.arguments import canonicalize_tool_arguments, freeze_arguments, semantic_signature
from app.gateway.http_client import MarketGatewayHttpClient
from app.graph.gap_loop import drop_repeat_steps
from app.graph.market_plan import A_SHARE_BENCHMARK_SYMBOL, apply_market_summary_prefix
from app.graph.nodes.analysts.technical import TechnicalAnalystNode
from app.graph import run_log
from app.models.market import NormalizedDatum, ToolResult
from app.models.research import ResearchIntent, ResearchPlan, ToolCallPlan


def _registry() -> str:
    return "\n".join(
        f"- {key}: tool" for key in ("quote", "sentiment", "limit_up_count", "limit_up_sectors", "telegraph")
    )


def _market_plan(*steps: ToolCallPlan) -> ResearchPlan:
    return ResearchPlan(
        intent=ResearchIntent(domain="a_share", task="market_summary"),
        steps=list(steps),
    )


def test_market_plan_quote_is_symbols_array_and_prompt_uses_canonical_contract():
    plan = _market_plan(ToolCallPlan(tool_key="quote", arguments={"symbol": "600519"}, purpose="quote"))
    apply_market_summary_prefix(plan, question="今天 A 股发生了什么？", registry=_registry(), max_steps=8)
    quote = next(step for step in plan.steps if step.tool_key == "quote")
    assert quote.arguments == {"symbols": [A_SHARE_BENCHMARK_SYMBOL]}
    assert "quote(symbols=[\"000300\"])" in PLANNER_PROMPT
    assert "quote 用 symbols" in PLANNER_PROMPT


def test_legacy_symbol_is_converted_only_at_gateway_argument_boundary():
    assert canonicalize_tool_arguments("get_market_quotes", {"symbol": "SH600519"}) == {
        "symbols": ["SH600519"]
    }
    assert canonicalize_tool_arguments("get_market_quotes", {"symbols": ["SZ399001", "SH000001"]}) == {
        "symbols": ["SH000001", "SZ399001"]
    }


def test_first_market_plan_has_no_unexecutable_quote_step():
    plan = _market_plan(
        ToolCallPlan(tool_key="quote", arguments={"symbol": "000300"}, purpose="old quote"),
        ToolCallPlan(tool_key="overview", arguments={}, purpose="invalid"),
    )
    _prefix, dropped = apply_market_summary_prefix(
        plan, question="今天 A 股发生了什么？", registry=_registry(), max_steps=8
    )
    assert plan.steps[0].arguments == {"symbols": ["000300"]}
    assert all(step.arguments.get("symbols") for step in plan.steps if step.tool_key == "quote")
    assert "quote" not in dropped


def test_research_more_replaces_old_quote_and_drops_same_semantic_signature():
    old = ToolCallPlan(tool_key="quote", arguments={"symbol": "000300"}, purpose="old")
    plan = _market_plan(old, ToolCallPlan(tool_key="sentiment", arguments={}, purpose="sentiment"))
    result = ToolResult(
        tool="get_market_quotes",
        tool_key="quote",
        arguments={"symbols": ["000300"]},
        status="success",
        normalized=[NormalizedDatum(tool="get_market_quotes", metric="last", value=1)],
    )
    kept, dropped = drop_repeat_steps(plan.steps, [result])
    assert [step.tool_key for step in kept] == ["sentiment"]
    assert dropped == ["quote"]


def test_nested_symbols_arguments_are_hashable_and_order_insensitive():
    left = {"symbols": ["SH000001", "SZ399001"], "nested": {"b": [2, 1], "a": True}}
    right = {"nested": {"a": True, "b": [2, 1]}, "symbols": ["SZ399001", "SH000001"]}
    assert isinstance(freeze_arguments(left), tuple)
    assert semantic_signature("get_market_quotes", {"symbols": ["SH000001", "SZ399001"]}) == semantic_signature(
        "get_market_quotes", {"symbols": ["SZ399001", "SH000001"]}
    )
    assert isinstance(semantic_signature("x", left), str)
    assert isinstance(semantic_signature("x", right), str)


@pytest.mark.asyncio
async def test_analyst_route_symbols_list_does_not_trigger_unhashable_error(monkeypatch):
    calls = []

    async def execute(self, tool_name, arguments, called_signatures, deadline=None):
        calls.append((tool_name, arguments))
        return ToolResult(tool=tool_name, arguments=arguments, status="success", normalized=[])

    monkeypatch.setattr("app.graph.tool_runtime.ToolRuntime.execute", execute)
    monkeypatch.setattr("app.graph.tool_runtime.ToolRuntime.truncate", lambda self, result: None)
    node = TechnicalAnalystNode(Settings())
    out = await node(
        {
            "question": "查看指数",
            "domain": "a_share",
            "route": [
                {
                    "analyst": "technical",
                    "budget": 2,
                    "tool_calls": [
                        {"tool_key": "quote", "arguments": {"symbols": ["SH000001", "SZ399001"]}},
                        {"tool_key": "quote", "arguments": {"symbols": ["SZ399001", "SH000001"]}},
                    ],
                }
            ],
        }
    )
    assert len(calls) == 1
    assert calls[0][1]["symbols"] == ["SH000001", "SZ399001"]
    assert not out["errors"]


@pytest.mark.asyncio
async def test_http_gateway_encodes_canonical_symbols_for_query_schema():
    client = MarketGatewayHttpClient(Settings(market_gateway_api_key="test"))
    seen = {}

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {"symbols": ["SH000001", "SZ399001"], "last": 1}

        def raise_for_status(self):
            return None

    class FakeHttp:
        async def request(self, method, path, **kwargs):
            seen.update(kwargs)
            return Response()

    client.client = FakeHttp()
    result = await client.call("get_market_quotes", {"symbols": ["SZ399001", "SH000001"]})
    assert result.arguments == {"symbols": ["SH000001", "SZ399001"]}
    assert seen["params"]["symbols"] == "SH000001,SZ399001"


@pytest.mark.asyncio
async def test_gateway_validation_error_preserves_trace_fields():
    client = MarketGatewayHttpClient(Settings(market_gateway_api_key="test"))
    client.client = object()
    result = await client.call("get_market_quotes", {"symbols": []})
    assert result.status == "error"
    assert result.tool_key == "quote"
    assert result.operation_id == "get_market_quotes"
    assert result.arguments == {"symbols": []}


def test_execution_log_contains_trace_fields_and_canonical_arguments():
    payload = run_log.log_executions(
        {"run_id": "r1"},
        [
            ToolResult(
                tool="get_market_quotes",
                tool_key="quote",
                arguments={"symbols": ["000300"]},
                status="success",
                normalized=[],
            )
        ],
    )
    row = payload["results"][0]
    assert row["tool_key"] == "quote"
    assert row["operation_id"] == "get_market_quotes"
    assert row["arguments"] == {"symbols": ["000300"]}
