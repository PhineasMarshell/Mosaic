"""Mock HTTP/LLM only; run real new adapters, runtime, Supervisor and analysts.

Validate provenance, requested fields, forecast isolation and macro periods.
Live upstream/UI is intentionally outside these tests; use verify_iwencai.
"""

import asyncio
import json
import time
import types

import httpx
import pytest

from app.agent.evidence_gate import evidence_date_scope, match_claims_to_evidence, run_evidence_gate
from app.cache import _resolve_ttl, market_cache
from app.config import Settings
from app.gateway.arguments import ArgumentValidationError, canonicalize_tool_arguments
from app.gateway.tool_registry import get_enabled_domains, registry_text, resolve_tool
from app.graph.iwencai_plan import required_query_keys
from app.graph.nodes.analysts.fundamental import FundamentalAnalystNode
from app.graph.nodes.analysts.technical import TechnicalAnalystNode
from app.graph.nodes.supervisor import SupervisorNode
from app.graph.tool_runtime import ToolRuntime
from app.research.evidence import build_evidence
from app.research.iwencai_query import normalize_query_data
from app.research.iwencai_search import normalize_search

MARKET = "internal_iwencai_market_query"
MACRO = "internal_iwencai_macro_query"
REPORT = "internal_iwencai_report_search"
NEWS = "internal_iwencai_news_search"
TOOLS = {
    MARKET: ("iwencai_market", "hithink-market-query", None, 60),
    MACRO: ("iwencai_macro", "hithink-macro-query", None, 3600),
    REPORT: ("iwencai_report_search", "report-search", "report", 3600),
    NEWS: ("iwencai_news_search", "news-search", "news", 300),
}


def settings(**kw):
    return Settings(_env_file=None, iwencai_api_key="synthetic-secret", openai_api_key="synthetic-model", **kw)


def payload(operation):
    if operation == MARKET:
        return {
            "status_code": 0,
            "code_count": 1,
            "columns": [
                {"key": "收盘价[20261012]", "index_name": "收盘价:前复权", "unit": "元", "timestamp": "20261012"}
            ],
            "datas": [{"股票代码": "600519.SH", "股票简称": "测试公司", "收盘价[20261012]": "1283.19"}],
        }
    if operation == MACRO:
        return {
            "status_code": 0,
            "code_count": 1,
            "row_count": 1,
            "datas": [
                {
                    "国家": "中国",
                    "时间": "20251231",
                    "指标": "GDP",
                    "指标值": 140187920000000,
                    "周期": "年",
                    "macro_id": "M001620247",
                    "macro_name": "GDP:全国",
                    "单位": "元",
                }
            ],
        }
    channel = TOOLS[operation][2]
    return {
        "status_code": 0,
        "data": [
            {
                "channel": channel,
                "title": "测试公司研究" if operation == REPORT else "产业政策新进展",
                "summary": "分析摘要" * 150,
                "publish_date": "2026-10-10 08:00:00",
                "url": "https://example.test/" + "x" * 250,
                "uid": "same-document",
                "para_index": 0,
                "extra": {
                    "organization": "测试证券",
                    "rating": "买入",
                    "target_price": 1500,
                    "publish_source": "转载网站",
                    "real_publish_source": "原始媒体",
                },
            }
        ],
    }


def arguments(op, **kw):
    return canonicalize_tool_arguments(
        op, {"query": "中国2025年GDP" if op == MACRO else "查询行情" if op == MARKET else "查询", **kw}
    )


@pytest.fixture(autouse=True)
def clear_cache():
    market_cache.clear()
    yield
    market_cache.clear()


@pytest.fixture
def transport(monkeypatch):
    client = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(
            "app.research.iwencai.httpx.AsyncClient",
            lambda **kw: client(**kw, transport=httpx.MockTransport(handler), trust_env=False),
        )

    return install


@pytest.mark.parametrize("op", TOOLS)
async def test_fixed_contract_runtime_cache_and_refresh(transport, op):
    requests = []
    raw = json.dumps(payload(op), ensure_ascii=False, indent=2)

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=raw)

    transport(handler)
    runtime = ToolRuntime(settings())
    result = await runtime.execute(op, arguments(op), set())
    request = requests[0]
    body = json.loads(request.content)
    key, skill, channel, ttl = TOOLS[op]
    assert request.method == "POST"
    assert str(request.url) == "https://openapi.iwencai.com" + (
        "/v1/comprehensive/search" if channel else "/v1/query2data"
    )
    assert body == (
        {"query": arguments(op)["query"], "channels": [channel], "app_id": "AIME_SKILL", "size": 10}
        if channel
        else {"query": arguments(op)["query"], "page": "1", "limit": "10", "is_cache": "1", "expand_index": "true"}
    )
    assert request.headers["X-Claw-Skill-Id"] == skill
    assert request.headers["X-Claw-Skill-Version"] == "1.0.0"
    assert request.headers["X-Claw-Call-Type"] == "normal"
    assert request.headers["X-Claw-Plugin-Id"] == request.headers["X-Claw-Plugin-Version"] == "none"
    assert len(request.headers["X-Claw-Trace-Id"]) == 64
    assert result.tool_key == key and result.raw == raw and build_evidence([result])
    assert _resolve_ttl(op, settings()) == ttl
    cached = await runtime.execute(op, arguments(op), set())
    assert cached.cache_hit and len(requests) == 1
    fresh = await runtime.execute(op, arguments(op), set(), refresh=True)
    assert fresh.refresh_requested and not fresh.cache_hit and len(requests) == 2
    if not channel:
        assert json.loads(requests[-1].content)["is_cache"] == "0"


def test_report_paragraph_identity_rating_and_price_isolation():
    p = payload(REPORT)
    p["data"] *= 3
    raw = json.dumps(p)
    result = normalize_search(raw, REPORT, arguments(REPORT))
    assert len(result.normalized) == 3 and result.raw == raw and "不同文档1份" in result.note
    value = result.normalized[0].value
    assert value["organization"] == "测试证券" and value["rating"] == "买入" and value["target_price"] == 1500
    assert value["document_id"] == "same-document" and value["chunk_index"] == 0
    evidence = build_evidence([result])
    assert evidence[0].value["url"] == value["url"] and len(evidence[0].value["summary"]) > 200
    report = {"claims": [{"claim": "测试公司股价上涨", "evidence_ids": [evidence[0].id]}]}
    matched = match_claims_to_evidence(report, evidence)
    assert not matched[0]["supported"] and "news_mention_not_price" in matched[0]["reason_codes"]
    assert evidence_date_scope("iwencai_report_search", REPORT) == "event"


def test_news_original_publisher_no_guessed_stock_code_and_missing_forecast():
    p = payload(NEWS)
    p["data"][0]["summary"] = "宏观\x01政策\n摘要"
    result = normalize_search(json.dumps(p), NEWS, arguments(NEWS))
    item = result.normalized[0]
    assert (
        item.value["publisher"] == "原始媒体" and item.instrument is None and item.instrument_scope == "search_results"
    )
    assert item.as_of_date == "2026-10-10" and item.value["summary"] == "宏观政策\n摘要"
    p = payload(REPORT)
    p["data"][0]["extra"] = {}
    report = normalize_search(json.dumps(p), REPORT, arguments(REPORT))
    assert report.normalized[0].value["rating"] is None and report.normalized[0].value["target_price"] is None


@pytest.mark.parametrize("op", [REPORT, NEWS])
@pytest.mark.parametrize("failure", ["empty", "wrong_channel", "missing_status", "business_error", "invalid_rows"])
def test_search_failures_never_produce_evidence(op, failure):
    p = payload(op)
    if failure == "empty":
        p["data"] = []
    if failure == "wrong_channel":
        p["data"][0]["channel"] = "announcement"
    if failure == "missing_status":
        p.pop("status_code")
    if failure == "business_error":
        p["status_code"] = 1001
    if failure == "invalid_rows":
        p["data"] = [None, {"summary": "no title"}]
    result = normalize_search(json.dumps(p), op, arguments(op))
    assert result.status == "error" and not build_evidence([result])


def test_market_dates_do_not_spread_to_default_latest_values():
    p = payload(MARKET)
    p["datas"][0].update({"最新涨跌幅": 1.5, "最新价": 1284, "上市日期": "20010827", "净利润": 10})
    result = normalize_query_data(json.dumps(p), MARKET, arguments(MARKET))
    assert result.partial and len(result.normalized) == 3
    close = next(d for d in result.normalized if "收盘价" in d.metric)
    assert close.as_of_date == "2026-10-12" and close.unit == "元" and "前复权" in close.note
    latest = next(d for d in result.normalized if "最新价" in d.metric)
    assert latest.as_of_date is None and latest.retrieved_at
    assert evidence_date_scope("iwencai_market", MARKET) == "market"


@pytest.mark.parametrize("query", ["贵州茅台RSI", "贵州茅台主力资金流向", "贵州茅台成交量"])
def test_default_prices_do_not_fake_requested_market_fields(query):
    result = normalize_query_data(json.dumps(payload(MARKET)), MARKET, arguments(MARKET, query=query))
    assert result.status == "error" and not build_evidence([result])


def test_partly_missing_requested_market_fields_are_explicit():
    result = normalize_query_data(json.dumps(payload(MARKET)), MARKET, arguments(MARKET, query="贵州茅台收盘价和MACD"))
    assert result.partial and "未返回所问字段：MACD" in result.note


def test_technical_indicator_names_with_chinese_and_numeric_boundaries():
    p = payload(MARKET)
    p["datas"][0]["6日RSI值[20261012]"] = 58.6
    p["datas"][0]["MA5[20261012]"] = 1230
    result = normalize_query_data(json.dumps(p), MARKET, arguments(MARKET, query="贵州茅台RSI和均线"))
    assert result.status == "success"
    assert any(d.value == 58.6 for d in result.normalized) and any(d.value == 1230 for d in result.normalized)


def test_macro_period_unit_country_and_zero_without_stock_identity():
    p = payload(MACRO)
    p["datas"][0]["指标值"] = 0
    result = normalize_query_data(json.dumps(p), MACRO, arguments(MACRO))
    item = result.normalized[0]
    assert result.status == "success" and len(result.normalized) == 1
    assert item.domain == "macro" and item.instrument == "M001620247" and item.instrument_scope == "macro_series"
    assert item.value["country"] == "中国" and item.value["frequency"] == "年" and item.value["value"] == 0
    assert item.unit == "元" and item.as_of_date == "2025-12-31"
    assert isinstance(build_evidence([result])[0].value, dict)
    assert evidence_date_scope("iwencai_macro", MACRO) == "economic_period"
    assert not run_evidence_gate([result], requested_date="2026-10-12").stale_evidence


def test_macro_year_precision_does_not_invent_date_or_stock_code():
    p = {
        "code_count": 1,
        "datas": [{"指标名称": "GDP", "数值": 126.06, "时间": "2024年", "单位": "万亿元", "国家": "中国"}],
    }
    result = normalize_query_data(json.dumps(p), MACRO, arguments(MACRO))
    assert result.status == "success" and result.normalized[0].instrument is None
    assert result.normalized[0].as_of_date is None and result.normalized[0].value["period"] == "2024年"
    assert "未补造" in result.normalized[0].note
    p["datas"][0].pop("国家")
    p["datas"][0].pop("单位")
    result = normalize_query_data(json.dumps(p), MACRO, arguments(MACRO))
    assert result.partial and result.normalized[0].unit is None


def test_macro_quotes_or_missing_value_never_count_as_macro_evidence():
    for row in [{"股票代码": "600519.SH", "最新价": 10}, {"指标": "GDP", "国家": "中国", "时间": "20251231"}]:
        result = normalize_query_data(json.dumps({"code_count": 1, "datas": [row]}), MACRO, arguments(MACRO))
        assert result.status == "error" and not build_evidence([result])


@pytest.mark.parametrize(
    "question,domain,expected",
    [
        ("贵州茅台股价和主力资金流向", "a_share", ["iwencai_market"]),
        ("510300 ETF行情及RSI", "a_share", ["iwencai_market"]),
        ("中国GDP和CPI", "macro", ["iwencai_macro"]),
        ("美国LPR利率及中国社融", "macro", ["iwencai_macro"]),
        ("净利率和毛利率", "a_share", ["iwencai_finance"]),
        ("债券票面利率", "a_share", ["iwencai_basicinfo"]),
        ("BTC行情和资金费率", "crypto", []),
        ("BTC资金费率和行情", None, []),
        ("黄金成交量", "commodities", []),
        ("BTC价格和中国CPI", "crypto", ["iwencai_macro"]),
    ],
)
def test_mandatory_query_scopes(question, domain, expected):
    assert required_query_keys(question, domain) == expected


async def test_supervisor_routes_required_tools_and_analysts_execute(transport):
    transport(
        lambda request: httpx.Response(
            200, json=payload(MACRO if request.headers["X-Claw-Skill-Id"] == "hithink-macro-query" else MARKET)
        )
    )
    node = SupervisorNode(settings())

    async def create(**kw):
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(
                        content=json.dumps({"intent": {"domain": "a_share", "task": "company_research"}, "steps": []})
                    )
                )
            ]
        )

    node.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    state = {"question": "中国GDP与贵州茅台行情"}
    routed = await node(state)
    assert routed["required_tool_keys"] == ["iwencai_market", "iwencai_macro"]
    outputs = await asyncio.gather(
        TechnicalAnalystNode(settings())({**state, **routed}), FundamentalAnalystNode(settings())({**state, **routed})
    )
    assert {r.tool_key for out in outputs for r in out["results"]} == {"iwencai_market", "iwencai_macro"}
    assert all(out["evidence"] for out in outputs)


def test_macro_is_supported_request_domain_and_tools_are_visible():
    from app.main import _parse_ask_payload

    assert "macro" in get_enabled_domains()
    assert _parse_ask_payload({"question": "中国GDP", "domain": "macro"}, "test")[1] == "macro"
    for op, (key, _, _, _) in TOOLS.items():
        meta = resolve_tool(key)
        assert not meta.requires_symbol and not meta.market_level and meta.tool_name == op
        assert f"- {key}:" in registry_text(domains=["hk_stock", "cross"])


@pytest.mark.parametrize("op", TOOLS)
async def test_auth_and_deadline_failures_are_isolated_and_redacted(transport, op):
    transport(lambda request: httpx.Response(401, text="synthetic-secret"))
    result = await ToolRuntime(settings()).execute(op, arguments(op), set())
    assert result.status == "error" and len(result.attempts) == 1 and "synthetic-secret" not in result.model_dump_json()

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=payload(op))

    transport(slow)
    result = await ToolRuntime(settings()).execute(op, arguments(op), set(), deadline=time.monotonic() + 0.05)
    assert result.status == "error" and "deadline_exhausted" in result.reason_codes and not build_evidence([result])


@pytest.mark.parametrize("op", [REPORT, NEWS])
@pytest.mark.parametrize(
    "invalid",
    [
        {"query": "x", "channels": ["news"]},
        {"query": "x", "size": 21},
        {"query": "x", "size": True},
        {"query": "x", "endpoint": "evil"},
    ],
)
def test_search_contract_rejects_model_selected_channels_and_endpoints(op, invalid):
    with pytest.raises(ArgumentValidationError):
        canonicalize_tool_arguments(op, invalid)
