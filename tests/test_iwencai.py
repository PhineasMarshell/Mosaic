"""同花顺公告适配回归：mock HTTP transport，不替换被测运行时/analyst。

覆盖请求协议、失败无证据、重试/预算、缓存刷新与证据保真；不覆盖真实上游/LLM。
真实接口通过 scripts.verify_iwencai 单独验证。
"""

import asyncio
import json
import re
import time

import httpx
import pytest

from app.agent.evidence_gate import evidence_date_scope, match_claims_to_evidence
from app.cache import _resolve_ttl, market_cache
from app.config import Settings
from app.gateway.arguments import ArgumentValidationError, canonicalize_tool_arguments
from app.gateway.tool_registry import registry_text, resolve_tool
from app.graph.nodes.analysts.technical import TechnicalAnalystNode
from app.graph.tool_runtime import ToolRuntime
from app.research.evidence import build_evidence
from app.research.iwencai import OPERATION_ID, SOURCE, normalize_announcements, search_announcements


def _settings(**kwargs):
    return Settings(_env_file=None, iwencai_api_key="synthetic-secret", **kwargs)


def _payload():
    # Synthetic fields follow the observed official data[] contract, including internal code alias.
    return {
        "status_code": 0,
        "data": [
            {
                "channel": "announcement",
                "title": "测试公司分红公告",
                "summary": "分红方案" * 300,
                "url": "https://example.test/" + "a" * 240 + ".pdf",
                "publish_date": "2026-10-10 00:00:00",
                "publish_time": 1791590400,
                "stock_infos": [{"code": "INTERNAL80"}, {"code": "600519", "name": "测试公司"}],
            }
        ],
        "total": 99,
        "extra_field": {"keep": True},
    }


def test_announcement_query_filters_wrong_code_and_month():
    payload = _payload()
    payload["data"].append({
        **payload["data"][0],
        "title": "错误月份公告",
        "publish_date": "2026-09-10 00:00:00",
        "stock_infos": [{"code": "600519", "name": "测试公司"}],
    })
    payload["data"].append({
        **payload["data"][0],
        "title": "错误标的公告",
        "stock_infos": [{"code": "000001", "name": "其他公司"}],
    })
    result = normalize_announcements(
        json.dumps(payload, ensure_ascii=False), {"query": "600519 2026年10月公告", "size": 20}
    )
    assert len(result.normalized) == 1
    assert result.normalized[0].as_of_date == "2026-10-10"


def test_announcement_normalizes_exchange_suffix_code():
    payload = _payload()
    payload["data"][0]["stock_infos"] = [
        {"code": "600519.SH", "name": "测试公司"},
        {"code": "INTERNAL80"},
    ]
    result = normalize_announcements(
        json.dumps(payload, ensure_ascii=False), {"query": "600519 2026年10月公告", "size": 20}
    )
    assert result.status == "success"
    assert result.normalized[0].instrument == "SH600519"
    assert result.normalized[0].instrument_name == "测试公司"


def test_announcement_rejects_wrong_exchange_suffix_code():
    payload = _payload()
    payload["data"][0]["stock_infos"] = [{"code": "000001.SZ", "name": "其他公司"}]
    result = normalize_announcements(
        json.dumps(payload, ensure_ascii=False), {"query": "600519 2026年10月公告", "size": 20}
    )
    assert result.status == "error"
    assert result.normalized == []
    assert "empty_normalized" in result.reason_codes


@pytest.fixture(autouse=True)
def _cache():
    market_cache.clear()
    yield
    market_cache.clear()


@pytest.fixture
def mock_transport(monkeypatch):
    real_client = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(
            "app.research.iwencai.httpx.AsyncClient",
            lambda **kw: real_client(
                **kw,
                transport=httpx.MockTransport(handler),
                trust_env=False,
            ),
        )

    return install


async def test_official_request_and_lossless_evidence(mock_transport):
    calls = []
    raw = json.dumps(_payload(), ensure_ascii=False, indent=2)

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=raw)

    mock_transport(handler)
    result = await ToolRuntime(_settings()).execute(OPERATION_ID, {"query": " 分红公告 ", "size": 3}, set())
    request = calls[0]
    assert str(request.url) == "https://openapi.iwencai.com/v1/comprehensive/search"
    assert request.method == "POST"
    assert json.loads(request.content) == {
        "query": "分红公告",
        "channels": ["announcement"],
        "app_id": "AIME_SKILL",
        "size": 3,
    }
    assert request.headers["Authorization"] == "Bearer synthetic-secret"
    assert request.headers["X-Claw-Skill-Id"] == "announcement-search"
    assert request.headers["X-Claw-Skill-Version"] == "1.0.0"
    assert request.headers["X-Claw-Call-Type"] == "normal"
    assert request.headers["X-Claw-Plugin-Id"] == request.headers["X-Claw-Plugin-Version"] == "none"
    assert re.fullmatch(r"[a-f0-9]{64}", request.headers["X-Claw-Trace-Id"])
    assert result.status == "success" and result.raw == raw
    assert result.tool_key == "announcement_search" and result.operation_id == OPERATION_ID
    datum = result.normalized[0]
    assert datum.instrument == "600519" and datum.instrument_name == "测试公司"
    assert datum.as_of_date == "2026-10-10" and datum.retrieved_at != datum.timestamp
    assert len(datum.value["summary"]) == 800 and "截断" in datum.note
    assert datum.completeness == "complete" and datum.source == SOURCE
    # P13 §四：单条公告的完整性描述它自己的字段（代码 + 发布日期都在），
    # 而"检索结果集"这一信封语义保留在 result 层。
    assert result.completeness == "search_results"
    evidence = build_evidence([result])
    assert evidence[0].value == datum.value  # Long original URL must survive the generic 200-character cap.
    assert evidence[0].authority == SOURCE and evidence[0].coverage == "instrument"
    assert evidence_date_scope("announcement_search", OPERATION_ID) == "event"


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query": " "},
        {"query": 123},
        {"query": "x", "size": True},
        {"query": "x", "size": 0},
        {"query": "x", "size": 21},
        {"query": "x", "size": "3"},
        {"query": "x", "endpoint": "/evil"},
        {"query": "x", "symbol": "600519"},
    ],
)
def test_invalid_arguments(arguments):
    with pytest.raises(ArgumentValidationError):
        canonicalize_tool_arguments(OPERATION_ID, arguments)


@pytest.mark.parametrize(
    "payload,code",
    [
        ("not json", "gateway_invalid_response"),
        ({"status_code": 1001, "data": _payload()["data"]}, "gateway_business_error"),
        ({"data": _payload()["data"]}, "gateway_business_error"),
        ({"status_code": False, "data": _payload()["data"]}, "gateway_business_error"),
        ({"status_code": 0, "data": {}}, "gateway_invalid_response"),
        ({"status_code": 0, "data": []}, "empty_normalized"),
        ({"status_code": 0, "data": [{"summary": "no title"}]}, "empty_normalized"),
        ({"status_code": 0, "data": [{"title": "wrong channel", "channel": "news"}]}, "empty_normalized"),
    ],
)
def test_invalid_and_empty_response_is_never_evidence(payload, code):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    result = normalize_announcements(raw, {"query": "x", "size": 10})
    assert result.status == "error" and result.normalized == [] and code in result.reason_codes
    assert build_evidence([result]) == []


def test_unknown_publication_and_instrument_are_not_guessed():
    # P13 §四.3：明确要求某一标的时，没有可验证证券代码的公告被拒绝，
    # 绝不用请求参数里的代码回填成"目标标的的公告"。
    result = normalize_announcements(
        json.dumps({"status_code": 0, "data": [{"title": "公告"}]}), {"query": "600519 今日公告", "size": 10}
    )
    assert result.status == "error" and result.normalized == []
    assert "empty_normalized" in result.reason_codes
    assert build_evidence([result]) == []

    # 未指定标的/年月时行被保留，但标的与日期仍为 None，且完整性不是 complete。
    loose = normalize_announcements(
        json.dumps({"status_code": 0, "data": [{"title": "公告"}]}), {"query": "公告", "size": 10}
    )
    datum = loose.normalized[0]
    assert datum.timestamp is None and datum.as_of_date is None and datum.instrument is None
    assert datum.instrument_scope == "search_results"
    assert datum.completeness == "partial"
    assert loose.status == "partial" and "核对" in loose.note and "证券代码" in datum.note


def test_announcement_cannot_substitute_for_live_price():
    result = normalize_announcements(json.dumps(_payload()), {"query": "x", "size": 10})
    evidence = build_evidence([result])
    claim = {"claims": [{"claim": "600519今日上涨", "evidence_ids": [evidence[0].id]}]}
    row = match_claims_to_evidence(claim, evidence)[0]
    assert not row["supported"] and "news_mention_not_price" in row["reason_codes"]


@pytest.mark.parametrize("epoch", [1791590400, 1791590400000])
def test_publication_epoch_seconds_and_milliseconds(epoch):
    payload = _payload()
    payload["data"][0].pop("publish_date")
    payload["data"][0]["publish_time"] = epoch
    result = normalize_announcements(json.dumps(payload), {"query": "x", "size": 10})
    assert result.normalized[0].timestamp == "2026-10-10 08:00:00+08:00"


def test_mixed_invalid_rows_mark_partial_and_preserve_raw():
    payload = _payload()
    payload["data"].append({"title": None})
    raw = json.dumps(payload)
    result = normalize_announcements(raw, {"query": "x", "size": 10})
    assert result.status == "partial" and result.partial and len(result.normalized) == 1 and result.raw == raw


@pytest.mark.parametrize(
    "status,code", [(401, "gateway_auth_error"), (403, "gateway_auth_error"), (400, "gateway_http_error")]
)
async def test_nonretryable_http_failure(mock_transport, status, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="synthetic-secret should not appear in errors")

    mock_transport(handler)
    result = await search_announcements(_settings(), {"query": "x"})
    assert len(calls) == 1 and code in result.reason_codes
    assert result.normalized == [] and result.raw is None
    assert "synthetic-secret" not in result.model_dump_json()


@pytest.mark.parametrize("status", [429, 503])
async def test_transient_retry_with_new_trace(mock_transport, status):
    traces = []

    def handler(request):
        traces.append(request.headers["X-Claw-Trace-Id"])
        return httpx.Response(status) if len(traces) == 1 else httpx.Response(200, json=_payload())

    mock_transport(handler)
    result = await search_announcements(_settings(max_retry_per_tool=1), {"query": "x"})
    assert result.status == "success" and len(result.attempts) == 2 and len(set(traces)) == 2


async def test_missing_key_and_expired_deadline_never_connect(mock_transport):
    def forbidden(request):
        raise AssertionError("request must not be sent")

    mock_transport(forbidden)
    result = await search_announcements(Settings(_env_file=None, iwencai_api_key=""), {"query": "x"})
    assert result.status == "error" and "IWENCAI_API_KEY" in result.error
    result = await search_announcements(_settings(), {"query": "x"}, deadline=time.monotonic() - 1)
    assert result.status == "error" and "deadline_exhausted" in result.reason_codes


async def test_inflight_deadline_cancels_transport(mock_transport):
    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=_payload())

    mock_transport(handler)
    started = time.monotonic()
    result = await ToolRuntime(_settings()).execute(OPERATION_ID, {"query": "x"}, set(), deadline=started + 0.05)
    assert time.monotonic() - started < 0.5 and result.normalized == [] and result.status == "error"
    assert "deadline_exhausted" in result.reason_codes


async def test_cache_and_refresh_without_gateway(mock_transport):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=_payload())

    mock_transport(handler)
    runtime = ToolRuntime(_settings())
    first = await runtime.execute(OPERATION_ID, {"query": "x"}, set())
    first.normalized[0].value["title"] = "mutated"
    cached = await runtime.execute(OPERATION_ID, {"query": " x ", "size": 10}, set())
    assert cached.cache_hit and cached.normalized[0].value["title"] != "mutated" and len(calls) == 1
    fresh = await runtime.execute(OPERATION_ID, {"query": "x"}, set(), refresh=True)
    assert fresh.refresh_requested and not fresh.cache_hit and len(calls) == 2
    assert _resolve_ttl(OPERATION_ID, _settings()) == 900
    assert _resolve_ttl(OPERATION_ID, _settings(iwencai_announcement_ttl_seconds=0)) == 0


async def test_analyst_executes_without_symbol_or_market_gateway(mock_transport):
    mock_transport(lambda request: httpx.Response(200, json=_payload()))
    node = TechnicalAnalystNode(_settings())

    def forbidden_gateway():
        raise AssertionError("announcement tool must not open Market Gateway")

    node._runtime._gateway_class = forbidden_gateway
    out = await node(
        {
            "question": "查询分红公告",
            "route": [
                {
                    "analyst": "technical",
                    "budget": 1,
                    "tool_calls": [{"tool_key": "announcement_search", "arguments": {"query": "分红公告"}}],
                }
            ],
        }
    )
    assert out["results"][0].status == "success" and len(out["evidence"]) == 1
    meta = resolve_tool("announcement_search")
    assert not meta.requires_symbol and not meta.market_level
    assert "announcement_search" in registry_text(domains=["hk_stock", "cross"])
