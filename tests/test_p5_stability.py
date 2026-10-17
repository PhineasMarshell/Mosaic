"""Transport doubles supplement (and do not substitute for) real P5 JSONL."""
import asyncio
import time
from types import SimpleNamespace

import httpx

from app.cache import Cache
from app.config import Settings
from app.gateway.arguments import canonicalize_tool_arguments, quote_transport_symbols
from app.gateway.http_client import MarketGatewayHttpClient
from app.gateway.mcp_client import MarketGatewayClient
from app.gateway.normalizer import normalize_tool_result
from app.graph.nodes.analysts.technical import TechnicalAnalystNode
from app.graph.nodes.critic import _format_evidence_for_review
from app.graph.tool_runtime import ToolRuntime
from app.research.evidence import build_evidence


async def test_http_200_incomplete_retries_then_preserves_real_empty():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={"code": "gateway_error", "message": "Cloudflare origin incomplete 502"})
        return httpx.Response(200, content=b"")

    client = MarketGatewayHttpClient(Settings(_env_file=None))
    client.client = httpx.AsyncClient(base_url="http://local", transport=httpx.MockTransport(handler))
    try:
        result = await client.call("get_stock_abnormal_reasons", {"symbol": "688981"}, time.monotonic()+3)
    finally:
        await client.close()
    assert len(calls) == 2
    assert [a["status"] for a in result.attempts] == ["error", "empty"]
    assert result.status == "error" and result.upstream_status == "empty"
    assert result.normalized == [] and build_evidence([result]) == []
    assert "empty_gateway_response" in result.reason_codes


async def test_mcp_inband_transient_retries_but_auth_does_not():
    for message, expected in [("Cloudflare origin incomplete 502", 2), ("Authentication failed 401", 1)]:
        calls = []

        async def call_tool(*args, calls=calls, message=message, **kwargs):
            calls.append(1)
            return SimpleNamespace(isError=True, content=[SimpleNamespace(text=message)])

        client = MarketGatewayClient(Settings(_env_file=None))
        client.session = SimpleNamespace(call_tool=call_tool)
        result = await client.call("get_stock_abnormal_reasons", {"symbol": "688981"}, time.monotonic()+3)
        assert len(calls) == expected
        assert result.normalized == [] and build_evidence([result]) == []


def test_compact_tencent_time_is_row_scoped_and_not_retrieval_date():
    result = normalize_tool_result("get_market_quotes", {"symbols": ["688981", "600519"]}, {
        "source": "tencent", "items": [
            {"code": "688981", "name": "中芯国际", "price": 108.42, "time": "20261009161452"},
            {"code": "600519", "name": "贵州茅台", "price": 1431, "time": "20261008160000"},
        ]})
    prices = [d for d in result.normalized if d.metric.endswith(".price")]
    assert [(d.instrument, d.as_of_date) for d in prices] == [("688981", "2026-10-09"), ("600519", "2026-10-08")]
    assert result.as_of_date is None  # mixed dates cannot get a global stamp
    assert all(d.retrieved_at for d in prices)
    invalid = normalize_tool_result("get_market_quotes", {"symbols": ["688981"]},
                                    {"items": [{"price": 1, "time": "20261309161452"}]})
    assert invalid.as_of_date is None


def test_empty_quote_collection_is_zero_evidence_and_index_encoding_is_scoped():
    result = normalize_tool_result("get_market_quotes", {"symbols": ["000300"]},
                                   {"source": "tencent", "count": 0, "items": []})
    assert result.status == "error" and result.normalized == []
    assert build_evidence([result]) == []
    assert quote_transport_symbols(["000300", "688981", "000001"]) == ["sh000300", "688981", "000001"]
    canonical = canonicalize_tool_arguments("get_market_quotes", {"symbols": ["sh000300", "000001"]})
    assert canonical == {"symbols": ["000001", "SH000300"]}
    assert quote_transport_symbols(canonical["symbols"]) == ["000001", "sh000300"]


async def test_failed_refresh_cannot_resurrect_old_partial(monkeypatch):
    cache = Cache()
    monkeypatch.setattr("app.graph.tool_runtime.market_cache", cache)
    runtime = ToolRuntime(Settings(_env_file=None))
    calls = []

    async def execute(*args, **kwargs):
        calls.append(1)
        return normalize_tool_result("get_ashare_sentiment", {}, {"partial": True, "data": {"up_count": 2}}) if len(calls) == 1 else normalize_tool_result("get_ashare_sentiment", {}, None, error="timeout")

    monkeypatch.setattr(runtime, "_do_execute", execute)
    assert (await runtime.execute("get_ashare_sentiment", {}, set())).partial
    refreshed = await runtime.execute("get_ashare_sentiment", {}, set(), refresh=True)
    later = await runtime.execute("get_ashare_sentiment", {}, set())
    assert refreshed.refresh_requested and not refreshed.cache_hit
    assert later.status == "error" and not later.cache_hit and len(calls) == 3


async def test_cache_hit_does_not_replay_old_attempt_diagnostics(monkeypatch):
    monkeypatch.setattr("app.graph.tool_runtime.market_cache", Cache())
    runtime = ToolRuntime(Settings(_env_file=None))

    async def execute(*args, **kwargs):
        result = normalize_tool_result("get_ashare_sentiment", {}, {"data": {"up_count": 2}})
        result.attempts = [{"status": "success", "duration_ms": 5000}]
        result.duration_ms = 5000
        return result

    monkeypatch.setattr(runtime, "_do_execute", execute)
    fresh = await runtime.execute("get_ashare_sentiment", {}, set())
    cached = await runtime.execute("get_ashare_sentiment", {}, set())
    assert fresh.attempts and not fresh.cache_hit
    assert cached.cache_hit and cached.attempts == [] and cached.duration_ms < 1000
    assert cached.retrieved_at == fresh.retrieved_at


async def test_tools_overlap_and_preserve_downstream_reserve(monkeypatch):
    settings = Settings(_env_file=None, tools_phase_timeout_seconds=1, tool_concurrency=3)
    node = TechnicalAnalystNode(settings)
    calls = []
    active = peak = 0

    async def execute(tool, args, signatures, deadline=None):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append(deadline)
        await asyncio.sleep(.02)
        active -= 1
        return normalize_tool_result(tool, args, {"value": 1})

    monkeypatch.setattr(node._runtime, "execute", execute)
    deadline = time.monotonic() + 33
    await node({"question": "q", "budget_deadline": deadline, "route": [{"analyst": "technical", "budget": 3,
                "tool_calls": [{"tool_key": key, "arguments": {}} for key in ["sentiment", "limit_up_count", "limit_up_sectors"]]}]})
    assert peak == 3
    assert all(d <= deadline-32 for d in calls)


def test_critic_quality_duplicates_are_bounded_but_references_are_complete():
    quality = [{"evidence_id": f"e{i}", "tool_key": "sentiment", "reason_codes": ["unknown_date"]}
               for i in range(1000)]
    text, stats = _format_evidence_for_review([], [{"id": "e999", "source_tool": "sentiment",
        "metric": "x", "value": "v"*1000}], {"evidence_quality": quality}, referenced_ids=["e999"])
    assert stats["quality_total"] == 1000 and stats["quality_shown"] == 3
    assert "count=1000" in text
    assert "v"*1000 in text
