"""P6 unified Skill adapter, provenance and controlled fallback contracts."""

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.gateway.controlled_fallback import ControlledFallbackRouter, fallback_allowed
from app.gateway.ths_skill_adapter import THSSkillAdapter
from app.gateway.ths_skill_registry import P6_BASELINE_SKILLS, THS_CAPABILITY_MATRIX, THS_SKILL_SPECS
from app.models.market import NormalizedDatum, ToolResult
from app.research.evidence import build_evidence


def _result(*, status="success", reason_codes=None, instrument="SH600519", dated=True, source="同花顺问财"):
    return ToolResult(
        tool="internal_iwencai_market_query",
        operation_id="internal_iwencai_market_query",
        tool_key="iwencai_market",
        arguments={"query": "600519 2026-10-09 最新价"},
        status=status,
        reason_codes=reason_codes or [],
        normalized=[] if status == "error" else [NormalizedDatum(
            tool="internal_iwencai_market_query", domain="a_share", metric="iwencai_market[1].收盘价",
            value=100, instrument=instrument, instrument_name="贵州茅台", as_of_date="2026-10-09" if dated else None,
            retrieved_at="2026-10-12T00:00:00+00:00", source=source, authority=source,
            completeness="complete", instrument_scope="instrument", coverage="instrument",
        )],
        authority=source,
    )


def test_registry_retains_ten_baseline_skills_and_no_mcp_dependency():
    assert len(P6_BASELINE_SKILLS) == 10
    assert len(THS_SKILL_SPECS) == 15
    assert all(item.transport == "http_api" and not item.depends_on_mcp for item in THS_SKILL_SPECS)
    assert "quote" in THS_CAPABILITY_MATRIX and "sector_theme" in THS_CAPABILITY_MATRIX
    assert all(item.input_schema and item.output_schema and item.version for item in THS_SKILL_SPECS)


@pytest.mark.asyncio
async def test_adapter_converts_exceptions_to_zero_datum_error(monkeypatch):
    async def fail(*args, **kwargs):
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr("app.research.iwencai_query.query_data", fail)
    result = await THSSkillAdapter(Settings(_env_file=None)).call(
        "internal_iwencai_market_query", {"query": "600519 最新价"}
    )
    assert result.status == "error"
    assert result.normalized == [] and result.datum_count == 0
    assert build_evidence([result]) == []
    assert "gateway_timeout" in result.reason_codes


def test_missing_date_or_instrument_stays_partial_and_cannot_be_treated_as_full_provenance():
    missing_date = _result(dated=False)
    missing_date.partial = True
    missing_date.status = "partial"
    evidence = build_evidence([missing_date])
    assert evidence and evidence[0].as_of_date == "unknown"
    assert missing_date.status == "partial"
    missing_instrument = _result(instrument=None)
    missing_instrument.partial = True
    missing_instrument.status = "partial"
    assert missing_instrument.normalized[0].instrument is None


def test_fallback_requires_primary_retryable_error_and_complete_provenance():
    primary = _result(status="error", reason_codes=["gateway_tls_error"])
    good = _result()
    assert fallback_allowed(primary, good, "quote", {"symbol": "SH600519"})
    assert not fallback_allowed(_result(), good, "quote", {"symbol": "SH600519"})
    assert not fallback_allowed(primary, _result(dated=False), "quote", {"symbol": "SH600519"})
    conflict = _result(status="error", reason_codes=["authority_conflict", "gateway_timeout"])
    assert not fallback_allowed(conflict, good, "quote", {"symbol": "SH600519"})
    wrong = _result(instrument="SZ000001")
    assert not fallback_allowed(primary, wrong, "quote", {"symbol": "600519", "date": "2026-10-09"})
    stale = _result()
    stale.normalized[0].as_of_date = "2020-01-01"
    assert not fallback_allowed(primary, stale, "quote", {"symbol": "600519", "date": "2026-10-09"})


@pytest.mark.asyncio
async def test_tool_runtime_routes_gateway_error_to_skill(monkeypatch):
    from app.graph.tool_runtime import ToolRuntime

    primary = _result(status="error", reason_codes=["gateway_timeout"])
    fallback = _result()

    class Gateway:
        tools = [SimpleNamespace(name="get_market_quotes")]

        async def call(self, *args, **kwargs):
            return primary

    class Adapter:
        async def call(self, *args, **kwargs):
            return fallback

    monkeypatch.setattr("app.gateway.ths_skill_adapter.skill_adapter_for", lambda settings: Adapter())
    runtime = ToolRuntime(Settings(_env_file=None, market_gateway_mode="mcp"))
    runtime.set_run_id("p6-runtime")
    result = await runtime._call_gateway(
        Gateway(), {"get_market_quotes"}, "get_market_quotes",
        {"symbols": ["600519"], "date": "2026-10-09"}, deadline=None,
    )
    assert result.source_switch is True
    assert result.primary_source == "mcp" and result.fallback_source == "ths_skill"
    assert result.run_id == "p6-runtime"


@pytest.mark.asyncio
async def test_router_keeps_primary_error_when_fallback_provenance_is_bad():
    router = ControlledFallbackRouter()
    primary = _result(status="error", reason_codes=["gateway_timeout"])
    fallback = _result(dated=False)
    result = await router.call(
        run_id="p6-test", capability="quote", arguments={"symbol": "SH600519"},
        primary_source="mcp", primary_transport="mcp", primary=lambda: _async(primary),
        fallback_source="ths", fallback=lambda: _async(fallback),
    )
    assert result.status == "error" and result.normalized == []
    assert result.fallback_reason == "fallback_provenance_insufficient"
    assert len(router.observations) == 2


async def _async(value):
    return value
