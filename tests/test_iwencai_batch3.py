"""Observed industry/expanded holder shapes and actual planning/runtime paths."""

import asyncio
import json
import time
import types

import httpx
import pytest

from app.agent.evidence_gate import match_claims_to_evidence, run_evidence_gate
from app.cache import _resolve_ttl, market_cache
from app.config import Settings
from app.gateway.arguments import canonicalize_tool_arguments
from app.gateway.tool_registry import resolve_tool
from app.graph.iwencai_plan import required_query_keys
from app.graph.nodes.analysts.fundamental import FundamentalAnalystNode
from app.graph.nodes.supervisor import SupervisorNode
from app.graph.tool_runtime import ToolRuntime
from app.research.evidence import build_evidence
from app.research.iwencai_query import normalize_query_data

INDUSTRY = "internal_iwencai_industry_query"
MANAGEMENT = "internal_iwencai_management_query"
TOOLS = {
    INDUSTRY: ("iwencai_industry", "hithink-industry-query", 60),
    MANAGEMENT: ("iwencai_management", "hithink-management-query", 3600),
}


def settings(**kw):
    return Settings(_env_file=None, iwencai_api_key="synthetic-secret", openai_api_key="synthetic-model", **kw)


def payload(op):
    if op == INDUSTRY:
        return {
            "code_count": 1,
            "columns": [
                {"key": "市盈率(pe,ttm)[20261009]", "unit": "倍", "timestamp": "20261009"},
                {"key": "净利润[20251231]", "unit": "元", "timestamp": "20251231"},
            ],
            "datas": [
                {
                    "指数代码": "881273.TI",
                    "指数简称": "白酒",
                    "最新价": "1951.966",
                    "最新涨跌幅:前复权": -0.6587,
                    "市盈率(pe,ttm)[20261009]": 19.65,
                    "净利润[20251231]": 0,
                }
            ],
        }
    return {
        "code_count": 1,
        "row_count": 2,
        "columns": [
            {"key": "名称", "index_name": "大股东名称(最新)"},
            {"key": "持股数量", "index_name": "大股东持股数量(最新)", "unit": "股"},
            {"key": "总户数[20261012]", "index_name": "股东总户数(虚拟表)", "unit": "人", "timestamp": "20261012"},
        ],
        "datas": [
            {
                "股票代码": "600519.SH",
                "股票简称": "测试公司",
                "最新价": 100,
                "名称": holder,
                "排名": rank,
                "公告日期": "20260815",
                "总户数[20261012]": 296404,
                "持股数量": shares,
                "持股比例": ratio,
                "持股变动类型": "不变",
                "实际控制人": "测试国资委",
            }
            for holder, rank, shares, ratio in [("甲股东", 1, 681282935, 54.5), ("乙股东", 2, 0, 0)]
        ],
    }


def args(op, query="查询", **kw):
    return canonicalize_tool_arguments(op, {"query": query, **kw})


def normalize(op, p=None, query="查询", **kw):
    return normalize_query_data(json.dumps(payload(op) if p is None else p), op, args(op, query, **kw))


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
async def test_actual_runtime_protocol_refresh_cache_and_ttl(transport, op):
    requests = []
    raw = json.dumps(payload(op))

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=raw)

    transport(handler)
    runtime = ToolRuntime(settings())
    result = await runtime.execute(op, args(op), set())
    key, skill, ttl = TOOLS[op]
    assert result.tool_key == key and result.raw == raw and build_evidence([result])
    assert _resolve_ttl(op, settings()) == ttl
    assert not resolve_tool(key).requires_symbol and not resolve_tool(key).market_level
    request = requests[0]
    assert request.method == "POST" and str(request.url) == "https://openapi.iwencai.com/v1/query2data"
    assert request.headers["X-Claw-Skill-Id"] == skill
    assert request.headers["X-Claw-Skill-Version"] == "1.0.0"
    assert request.headers["X-Claw-Call-Type"] == "normal"
    assert request.headers["X-Claw-Plugin-Id"] == request.headers["X-Claw-Plugin-Version"] == "none"
    assert len(request.headers["X-Claw-Trace-Id"]) == 64
    assert json.loads(request.content) == {
        "query": "查询",
        "page": "1",
        "limit": "10",
        "is_cache": "1",
        "expand_index": "true",
    }
    assert (await runtime.execute(op, args(op), set())).cache_hit and len(requests) == 1
    fresh = await runtime.execute(op, args(op), set(), refresh=True)
    assert fresh.refresh_requested and not fresh.cache_hit and len(requests) == 2
    assert json.loads(requests[-1].content)["is_cache"] == "0"
    assert request.headers["X-Claw-Trace-Id"] != requests[-1].headers["X-Claw-Trace-Id"]


def test_industry_index_identity_missing_fields_and_per_metric_date_scope():
    result = normalize(INDUSTRY, query="白酒行业 营业收入 净利润 市盈率")
    assert result.partial and "未返回所问字段：营业收入" in result.note
    assert all(d.instrument == "881273.TI" and d.instrument_scope == "industry_index" for d in result.normalized)
    assert all(d.instrument_name == "白酒" and d.value["classification"] == {} for d in result.normalized)
    price = next(d for d in result.normalized if d.metric.endswith("最新价"))
    profit = next(d for d in result.normalized if "净利润" in d.metric)
    assert price.as_of_date is None and price.value["date_scope"] == "market"
    assert profit.value["value"] == 0 and profit.as_of_date == "2025-12-31"
    gate = run_evidence_gate([result], requested_date="2026-10-12")
    assert any("市盈率" in q["metric"] and "stale_date" in q["reason_codes"] for q in gate.evidence_quality)
    assert all("stale_date" not in q["reason_codes"] for q in gate.evidence_quality if "净利润" in q["metric"])
    assert all(isinstance(e.value, dict) for e in build_evidence([result]))


def test_name_only_industry_classification_and_ranking_are_preserved():
    p = {"code_count": 31, "datas": [{"行业名称": "银行", "行业分类": "申万一级", "排名": 1, "行业估值": "5.23PE"}]}
    result = normalize(INDUSTRY, p, query="行业估值排名", limit=1)
    assert result.partial and "page=2" in result.note
    assert any(d.metric.endswith("排名") and d.value["value"] == 1 for d in result.normalized)
    assert all(d.instrument is None and d.instrument_scope == "industry" for d in result.normalized)
    assert all(d.value["classification"] == {"行业分类": "申万一级"} for d in result.normalized)


def test_shareholder_name_rank_zero_and_dates_stay_together():
    result = normalize(MANAGEMENT, query="股东户数 前十大股东 实际控制人", limit=1)
    assert result.partial and "股东总数" in result.note and "page=2" not in result.note
    holdings = [d for d in result.normalized if d.metric.endswith("持股数量")]
    assert [(d.value["holder_name"], d.value["holder_rank"], d.value["value"]) for d in holdings] == [
        ("甲股东", 1, 681282935),
        ("乙股东", 2, 0),
    ]
    assert all(d.instrument == "SH600519" and d.instrument_name == "测试公司" for d in result.normalized)
    assert all(d.as_of_date is None for d in holdings)
    assert all(d.value["source_dates"]["公告日期"] == "20260815" for d in holdings)
    assert not any(d.metric.endswith("最新价") or ".公告日期" in d.metric for d in result.normalized)
    assert next(d for d in result.normalized if "总户数" in d.metric).as_of_date == "2026-10-12"
    assert not run_evidence_gate([result], requested_date="2026-10-12").stale_evidence
    assert any(e.value.get("holder_name") == "乙股东" and e.value["value"] == 0 for e in build_evidence([result]))


def test_snapshot_date_from_explicit_report_period_not_announcement():
    p = payload(MANAGEMENT)
    for row in p["datas"]:
        row["报告期"] = "20260630"
    result = normalize(MANAGEMENT, p)
    holding = next(d for d in result.normalized if d.metric.endswith("持股数量"))
    assert holding.as_of_date == "2026-06-30" and holding.value["source_dates"]["公告日期"] == "20260815"
    assert not run_evidence_gate([result], requested_date="2026-10-12").stale_evidence


def test_names_only_holder_table_requires_confirmed_holder_semantics():
    p = {
        "code_count": 1,
        "columns": [{"key": "名称", "index_name": "大股东名称(最新)"}],
        "datas": [{"股票代码": "600519.SH", "名称": "甲股东", "排名": 1}],
    }
    result = normalize(MANAGEMENT, p, query="前十大股东")
    assert result.partial and result.normalized[0].instrument_name is None
    assert result.normalized[0].value["holder_name"] == "甲股东"
    p["columns"] = []
    assert normalize(MANAGEMENT, p, query="前十大股东").status == "error"


def test_industry_latest_quote_never_borrows_financial_report_period():
    p = payload(INDUSTRY)
    p["datas"][0]["报告期"] = "20251231"
    result = normalize(INDUSTRY, p)
    assert next(d for d in result.normalized if d.metric.endswith("最新价")).as_of_date is None


def test_top_shareholders_do_not_satisfy_free_float_shareholder_scope():
    result = normalize(MANAGEMENT, query="前十大流通股东")
    assert result.partial and "未返回所问字段：流通股东口径" in result.note


def test_structured_industry_change_checks_direction_and_never_proves_stock_price():
    p = {
        "code_count": 1,
        "columns": [{"key": "涨跌幅[20261012]", "unit": "%", "timestamp": "20261012"}],
        "datas": [{"行业名称": "白酒", "行业分类": "同花顺行业", "涨跌幅[20261012]": -1.25}],
    }
    evidence = build_evidence([normalize(INDUSTRY, p)])
    assert evidence

    def check(claim):
        return match_claims_to_evidence({"claims": [{"claim": claim, "evidence_ids": [evidence[0].id]}]}, evidence)[0]

    assert check("白酒行业下跌1.25%")["supported"]
    assert "contradictory_direction" in check("白酒行业上涨1.25%")["reason_codes"]
    assert "numeric_mismatch" in check("白酒行业下跌2.5%")["reason_codes"]
    assert "unmatched_instrument" in check("白酒股票价格为10元")["reason_codes"]


@pytest.mark.parametrize("op,query", [(INDUSTRY, "白酒行业营业收入"), (MANAGEMENT, "股东户数")])
def test_default_or_unrelated_fields_cannot_satisfy_requested_data(op, query):
    p = payload(op)
    if op == INDUSTRY:
        p["datas"][0].pop("净利润[20251231]")
    else:
        for row in p["datas"]:
            row.pop("总户数[20261012]")
    result = normalize(op, p, query=query)
    assert result.status == "error" and not build_evidence([result])


@pytest.mark.parametrize("op", TOOLS)
async def test_auth_failure_and_deadline_produce_no_evidence(transport, op):
    transport(lambda r: httpx.Response(401, text="synthetic-secret"))
    result = await ToolRuntime(settings()).execute(op, args(op), set())
    assert result.status == "error" and len(result.attempts) == 1 and "synthetic-secret" not in result.model_dump_json()

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=payload(op))

    transport(slow)
    result = await ToolRuntime(settings()).execute(op, args(op), set(), deadline=time.monotonic() + 0.05)
    assert result.status == "error" and "deadline_exhausted" in result.reason_codes and not build_evidence([result])


@pytest.mark.parametrize(
    "question,expected",
    [
        ("白酒行业估值排名", {"iwencai_industry"}),
        ("新能源板块今天怎么样", {"iwencai_industry"}),
        ("银行业盈利如何", {"iwencai_industry"}),
        ("贵州茅台所属行业", {"iwencai_basicinfo"}),
        ("贵州茅台股东大会", {"iwencai_events"}),
        ("贵州茅台股权质押", {"iwencai_management", "iwencai_events"}),
        ("贵州茅台前十大流通股东及实际控制人", {"iwencai_management"}),
        ("中国GDP", {"iwencai_macro"}),
    ],
)
def test_required_query_boundaries(question, expected):
    assert set(required_query_keys(question)) == expected


async def test_supervisor_forces_new_tools_and_fundamental_executes(transport):
    transport(
        lambda request: httpx.Response(
            200,
            json=payload(INDUSTRY if request.headers["X-Claw-Skill-Id"] == "hithink-industry-query" else MANAGEMENT),
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
    state = {"question": "白酒行业估值及贵州茅台前十大股东"}
    routed = await node(state)
    assert set(routed["required_tool_keys"]) == {"iwencai_industry", "iwencai_management"}
    outputs = await FundamentalAnalystNode(settings())({**state, **routed})
    assert {r.tool_key for r in outputs["results"]} == {"iwencai_industry", "iwencai_management"}
    assert outputs["evidence"]
