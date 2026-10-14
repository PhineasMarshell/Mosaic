"""P8 regression coverage for production source selection and auth policy."""

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.gateway.source_routing import is_primary_skill_enabled
from app.graph.tool_runtime import ToolRuntime
from app.models.market import NormalizedDatum, ToolResult


def _datum(source: str = "同花顺问财") -> NormalizedDatum:
    return NormalizedDatum(
        tool="internal_iwencai_market_query",
        metric="close",
        value=100,
        instrument="SH600519",
        as_of_date="2026-10-09",
        retrieved_at="2026-10-12T00:00:00Z",
        source=source,
        authority=source,
        instrument_scope="instrument",
        coverage="instrument",
    )


def _settings(**updates) -> Settings:
    values = {
        "_env_file": None,
        "market_primary_source": "ths_skill",
        "market_fallback_source": "mcp",
        "market_source_fallback_enabled": True,
        "market_source_routing_enabled": True,
        "iwencai_api_key": "synthetic-secret",
        "market_gateway_mode": "mcp",
    }
    values.update(updates)
    return Settings(**values)


def test_default_primary_source_is_enabled_by_real_credential():
    settings = Settings(_env_file=None, iwencai_api_key="synthetic-secret", market_source_routing_enabled=True)
    assert settings.market_primary_source == "ths_skill"
    assert "market_primary_source" not in settings.model_fields_set
    assert is_primary_skill_enabled(settings)


@pytest.mark.asyncio
async def test_runtime_calls_ths_first_and_mcp_only_after_failure(monkeypatch):
    calls = []
    primary = ToolResult(
        tool="internal_iwencai_market_query",
        operation_id="internal_iwencai_market_query",
        arguments={},
        status="error",
        normalized=[],
        reason_codes=["empty_normalized"],
    )
    fallback = ToolResult(
        tool="get_market_quotes",
        operation_id="get_market_quotes",
        arguments={},
        status="success",
        normalized=[_datum("MCP")],
    )

    class Skill:
        async def call(self, operation, arguments, **kwargs):
            calls.append("ths")
            return primary.model_copy(deep=True)

    class Gateway:
        tools = [SimpleNamespace(name="get_market_quotes")]

        def __init__(self, settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def call(self, operation, arguments, deadline=None):
            calls.append("mcp")
            return fallback.model_copy(deep=True)

    monkeypatch.setattr("app.graph.tool_runtime.skill_adapter_for", lambda settings: Skill())
    runtime = ToolRuntime(_settings())
    runtime._gateway_class = lambda: Gateway
    result = await runtime.execute(
        "get_market_quotes", {"symbol": "600519", "date": "2026-10-09"}, set(), refresh=True
    )
    assert calls == ["ths", "mcp"]
    assert result.status == "success"
    assert result.primary_source == "ths_skill"
    assert result.fallback_source == "mcp" and result.source_switch is True


@pytest.mark.asyncio
async def test_401_stays_zero_datum_and_does_not_call_mcp(monkeypatch):
    calls = []
    failed = ToolResult(
        tool="internal_iwencai_market_query",
        operation_id="internal_iwencai_market_query",
        arguments={},
        status="error",
        normalized=[],
        reason_codes=["gateway_auth_error", "ths_invalid_credentials"],
    )

    class Skill:
        async def call(self, operation, arguments, **kwargs):
            calls.append("ths")
            return failed.model_copy(deep=True)

    class Gateway:
        tools = [SimpleNamespace(name="get_market_quotes")]

        def __init__(self, settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def call(self, operation, arguments, deadline=None):
            calls.append("mcp")
            raise AssertionError("authentication failures must not fallback")

    monkeypatch.setattr("app.graph.tool_runtime.skill_adapter_for", lambda settings: Skill())
    runtime = ToolRuntime(_settings())
    runtime._gateway_class = lambda: Gateway
    result = await runtime.execute(
        "get_market_quotes", {"symbol": "600519", "date": "2026-10-09"}, set(), refresh=True
    )
    assert calls == ["ths"]
    assert result.status == "error" and result.normalized == [] and result.partial is False
    assert result.fallback_source is None and result.source_switch is False
    assert "mcp_fallback_not_attempted_auth" in result.reason_codes
    assert result.fallback_reason == "authentication_failure_no_fallback"


@pytest.mark.parametrize("status, expected", [(401, "ths_invalid_credentials"), (403, "ths_permission_denied")])
def test_ths_error_classes_are_secret_free(status, expected):
    from app.research.iwencai import _auth_reason_codes

    assert expected in _auth_reason_codes(status)
    assert "synthetic-secret" not in str(_auth_reason_codes(status))
