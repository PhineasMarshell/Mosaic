"""P7 source routing tests.

Mocks replace only the THS Skill adapter and MCP Gateway; normalizers, routing,
date gate, and evidence construction remain real. Live credentials and network
quality are covered by the acceptance probe, not this unit module.
"""

from types import SimpleNamespace

import pytest

from app.cache import market_cache
from app.config import Settings
from app.graph.nodes.gate import GateNode
from app.graph.tool_runtime import ToolRuntime
from app.models.market import NormalizedDatum, ToolResult


@pytest.fixture(autouse=True)
def _clear_cache():
    market_cache.clear()
    yield
    market_cache.clear()


def _settings(**overrides):
    values = {
        "_env_file": None,
        "market_primary_source": "ths_skill",
        "market_fallback_source": "mcp",
        "market_source_fallback_enabled": True,
        "market_source_routing_enabled": True,
        "iwencai_api_key": "synthetic-secret",
        "market_gateway_mode": "mcp",
    }
    values.update(overrides)
    return Settings(**values)


def _datum(code="600519", date="2026-10-09", source="同花顺问财"):
    return NormalizedDatum(
        tool="internal_iwencai_market_query", metric="close", value=100,
        instrument=code, as_of_date=date, source=source, authority=source,
        retrieved_at="2026-10-12T00:00:00Z", instrument_scope="instrument",
    )


class _Skill:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def call(self, operation, arguments, **kwargs):
        self.calls += 1
        return self.result.model_copy(deep=True)


class _Gateway:
    calls = 0
    result = None

    def __init__(self, settings):
        self.tools = [SimpleNamespace(name="get_market_quotes")]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def call(self, operation, arguments, deadline=None):
        type(self).calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result.model_copy(deep=True)


@pytest.mark.asyncio
async def test_ths_success_does_not_probe_mcp(monkeypatch):
    result = ToolResult(
        tool="internal_iwencai_market_query", operation_id="internal_iwencai_market_query",
        arguments={"query": "600519 2026-10-09 收盘价"}, status="success",
        authority="同花顺问财", normalized=[_datum()],
    )
    skill = _Skill(result)
    monkeypatch.setattr("app.graph.tool_runtime.skill_adapter_for", lambda settings: skill)
    _Gateway.calls = 0
    runtime = ToolRuntime(_settings())
    runtime._gateway_class = lambda: _Gateway
    output = await runtime.execute("get_market_quotes", {"symbol": "600519", "date": "2026-10-09"}, set())
    assert output.status == "success"
    assert output.primary_source == "ths_skill" and output.fallback_source is None
    assert skill.calls == 1 and _Gateway.calls == 0


@pytest.mark.asyncio
async def test_ths_error_uses_mcp_and_keeps_provenance(monkeypatch):
    failed = ToolResult(tool="internal_iwencai_market_query", operation_id="internal_iwencai_market_query",
                        arguments={}, status="error", normalized=[], reason_codes=["empty_normalized"])
    skill = _Skill(failed)
    monkeypatch.setattr("app.graph.tool_runtime.skill_adapter_for", lambda settings: skill)
    _Gateway.calls = 0
    _Gateway.result = ToolResult(
        tool="get_market_quotes", operation_id="get_market_quotes", arguments={"symbols": ["600519"], "date": "2026-10-09"},
        status="success", authority="MCP", normalized=[_datum(source="MCP")],
    )
    runtime = ToolRuntime(_settings())
    runtime._gateway_class = lambda: _Gateway
    output = await runtime.execute("get_market_quotes", {"symbol": "600519", "date": "2026-10-09"}, set())
    assert output.status == "success" and output.source_switch is True
    assert output.primary_source == "ths_skill" and output.fallback_source == "mcp"
    assert _Gateway.calls == 1 and output.normalized


@pytest.mark.asyncio
async def test_partial_ths_is_retained_without_mcp_replacement(monkeypatch):
    partial = ToolResult(tool="internal_iwencai_market_query", operation_id="internal_iwencai_market_query",
                         arguments={}, status="partial", partial=True,
                         reason_codes=["missing_as_of_date", "unknown_date"], normalized=[_datum(date=None)])
    skill = _Skill(partial)
    monkeypatch.setattr("app.graph.tool_runtime.skill_adapter_for", lambda settings: skill)
    _Gateway.calls = 0
    runtime = ToolRuntime(_settings())
    runtime._gateway_class = lambda: _Gateway
    output = await runtime.execute("get_market_quotes", {"symbol": "600519", "date": "2026-10-09"}, set())
    assert output.status == "partial" and output.normalized
    assert _Gateway.calls == 0


def test_route_table_covers_kline_and_limit_up_capabilities():
    runtime = ToolRuntime(_settings())
    assert runtime._fallback_spec("get_market_klines", {"symbol": "600519", "end": "2026-10-09"})[0] == "kline"
    assert runtime._fallback_spec("get_limit_up_count", {"date": "2026-10-09"})[0] == "limit_up_count"
    assert runtime._fallback_spec("list_limit_up_stocks", {"date": "2026-10-09"})[0] == "limit_up_pool"
    assert runtime._fallback_spec("list_limit_up_sectors", {"date": "2026-10-09"})[0] == "limit_up_sectors"


@pytest.mark.asyncio
async def test_mcp_fallback_failure_has_no_evidence_and_preserves_error(monkeypatch):
    failed = ToolResult(tool="internal_iwencai_market_query", operation_id="internal_iwencai_market_query",
                        arguments={}, status="error", normalized=[], reason_codes=["gateway_upstream_error"])
    monkeypatch.setattr("app.graph.tool_runtime.skill_adapter_for", lambda settings: _Skill(failed))
    _Gateway.result = RuntimeError("mcp upstream timeout")
    runtime = ToolRuntime(_settings())
    runtime._gateway_class = lambda: _Gateway
    output = await runtime.execute("get_market_quotes", {"symbol": "600519", "date": "2026-10-09"}, set())
    assert output.status == "error" and output.normalized == []
    assert output.fallback_source == "mcp" and output.source_switch is True


@pytest.mark.asyncio
async def test_wrong_skill_date_or_symbol_keeps_primary_failure(monkeypatch):
    failed = ToolResult(tool="internal_iwencai_market_query", operation_id="internal_iwencai_market_query",
                        arguments={}, status="error", normalized=[], reason_codes=["empty_normalized"])
    monkeypatch.setattr("app.graph.tool_runtime.skill_adapter_for", lambda settings: _Skill(failed))
    # A returned datum for another code/date is never accepted as the requested
    # quote evidence, even when the Skill call itself returned HTTP 200.
    _Gateway.result = ToolResult(
        tool="get_market_quotes", operation_id="get_market_quotes", arguments={}, status="success",
        authority="MCP", normalized=[_datum(code="000001", date="2026-10-08", source="MCP")],
    )
    runtime = ToolRuntime(_settings())
    runtime._gateway_class = lambda: _Gateway
    output = await runtime.execute("get_market_quotes", {"symbol": "600519", "date": "2026-10-09"}, set())
    assert output.status == "error" and output.normalized == []
    assert output.fallback_reason == "fallback_provenance_insufficient"


@pytest.mark.asyncio
async def test_weekend_missing_as_of_date_is_blocked_quality_gap():
    result = ToolResult(tool="get_market_quotes", tool_key="quote", operation_id="get_market_quotes",
                        arguments={"symbols": ["600519"]}, status="partial", partial=True,
                        normalized=[_datum(date=None, source="同花顺问财")])
    output = await GateNode(_settings())({
        "results": [result], "evidence": [], "requested_date": "2026-10-11",
        "planned_as_of_date": "2026-10-09", "market_closed": True,
    })
    assert output["gate"].has_evidence is False
    assert {"missing_as_of_date", "unknown_date"} <= set(output["gate"].reason_codes)
