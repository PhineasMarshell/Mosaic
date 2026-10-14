"""Mock HTTP and planner LLM only; actual adapters, Supervisor and analysts run.

Verify official contracts, provenance, failure isolation, pagination, required
tool selection and runtime caching. Does not test live upstream or live LLM;
use scripts.verify_iwencai for those provider checks.
"""

import asyncio
import json
import time
import types

import httpx
import pytest

from app.agent.evidence_gate import evidence_date_scope, run_evidence_gate
from app.cache import _resolve_ttl, market_cache
from app.config import Settings
from app.gateway.arguments import ArgumentValidationError, canonicalize_tool_arguments
from app.gateway.iwencai_contracts import QUERY_TOOLS
from app.gateway.tool_registry import registry_text, resolve_tool
from app.graph.gap_loop import append_gap_steps
from app.graph.iwencai_plan import apply_required_iwencai_tools, required_query_keys
from app.graph.nodes.analysts.fundamental import FundamentalAnalystNode
from app.graph.nodes.supervisor import SupervisorNode
from app.graph.tool_runtime import ToolRuntime
from app.models.market import ToolResult
from app.models.research import ResearchPlan, ToolCallPlan
from app.research.evidence import build_evidence
from app.research.iwencai_query import normalize_query_data, query_data

FINANCE = "internal_iwencai_finance_query"
EVENTS = "internal_iwencai_event_query"
BASIC = "internal_iwencai_basicinfo_query"


def _settings(**kwargs):
    return Settings(_env_file=None, iwencai_api_key="synthetic-secret", openai_api_key="synthetic-model", **kwargs)


def _payload():
    return {
        "status_code": 0,
        "code_count": 1,
        "columns": [
            {"key": "营业收入[20251231]", "unit": "元", "timestamp": "20251231"},
            {"key": "净资产收益率[20251231]", "unit": "%", "timestamp": "20251231"},
        ],
        "datas": [
            {
                "股票代码": "600519.SH",
                "股票简称": "测试公司",
                "最新价": "1200",
                "最新涨跌幅": 1.5,
                "营业收入[20251231]": 0,
                "净资产收益率[20251231]": 30.5,
                "净利润[20251231]": "--",
                "公告日期": "20261010",
                "事件类型": "业绩预告",
                "上市日期": "20010827",
                "上市地点": "上海",
            }
        ],
    }


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


@pytest.mark.parametrize("operation", [FINANCE, EVENTS, BASIC])
async def test_official_contract_per_skill(mock_transport, operation):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=_payload())

    mock_transport(handler)
    result = await query_data(_settings(), operation, {"query": "测试公司", "page": "1", "limit": "3"})
    request = requests[0]
    assert request.method == "POST" and str(request.url) == "https://openapi.iwencai.com/v1/query2data"
    assert json.loads(request.content) == {
        "query": "测试公司",
        "page": "1",
        "limit": "3",
        "is_cache": "1",
        "expand_index": "true",
    }
    assert request.headers["X-Claw-Skill-Id"] == QUERY_TOOLS[operation][1]
    assert request.headers["X-Claw-Call-Type"] == "normal"
    assert request.headers["X-Claw-Skill-Version"] == "1.0.0"
    assert len(request.headers["X-Claw-Trace-Id"]) == 64
    assert result.tool_key == QUERY_TOOLS[operation][0] and result.status == "success"
    assert json.loads(result.raw) == _payload()
    assert "synthetic-secret" not in result.model_dump_json()


def test_finance_metric_unit_period_and_zero_preserved():
    args = canonicalize_tool_arguments(FINANCE, {"query": "测试公司2025年收入ROE"})
    result = normalize_query_data(json.dumps(_payload()), FINANCE, args)
    revenue, roe = result.normalized
    assert revenue.value == 0 and revenue.unit == "元" and revenue.as_of_date == "2025-12-31"
    assert roe.value == 30.5 and roe.unit == "%" and roe.instrument == "SH600519"
    assert len(build_evidence([result])) == 2
    assert all("最新" not in d.metric and "上市" not in d.metric and "公告" not in d.metric for d in result.normalized)
    gate = run_evidence_gate([result], requested_date="2026-10-12")
    assert gate.has_evidence and not gate.stale_evidence
    assert evidence_date_scope("iwencai_finance", FINANCE) == "reporting_period"


def test_event_date_and_static_listing_date_are_distinct():
    args = canonicalize_tool_arguments(EVENTS, {"query": "测试公司业绩预告"})
    event = normalize_query_data(json.dumps(_payload()), EVENTS, args)
    assert all(d.as_of_date == "2026-10-10" for d in event.normalized)
    assert not any("营业收入" in d.metric or "上市" in d.metric for d in event.normalized)
    basic = normalize_query_data(json.dumps(_payload()), BASIC, args)
    listing = next(d for d in basic.normalized if "上市日期" in d.metric)
    assert listing.value == "20010827" and listing.timestamp is None and listing.as_of_date is None
    assert listing.retrieved_at and "静态" in listing.note
    assert evidence_date_scope("iwencai_basicinfo", BASIC) == "static"
    assert not run_evidence_gate([basic], requested_date="2026-10-12").stale_evidence


@pytest.mark.parametrize("operation", [FINANCE, EVENTS])
def test_quote_identity_only_is_not_requested_data(operation):
    payload = {
        "status_code": 0,
        "code_count": 1,
        "datas": [{"股票代码": "600519.SH", "股票简称": "公司", "最新价": 10}],
    }
    args = canonicalize_tool_arguments(operation, {"query": "x"})
    result = normalize_query_data(json.dumps(payload), operation, args)
    assert result.status == "error" and result.normalized == [] and build_evidence([result]) == []


@pytest.mark.parametrize(
    "payload",
    [
        "bad json",
        {},
        {"datas": {}},
        {"datas": [], "code_count": 0},
        {"status_code": 1002, "datas": _payload()["datas"]},
        {"status_code": False, "datas": _payload()["datas"]},
        {"error": "synthetic-secret", "datas": _payload()["datas"]},
    ],
)
def test_failure_never_becomes_evidence(payload):
    args = canonicalize_tool_arguments(FINANCE, {"query": "x"})
    result = normalize_query_data(payload if isinstance(payload, str) else json.dumps(payload), FINANCE, args)
    assert result.status == "error" and result.raw is None and build_evidence([result]) == []
    assert "synthetic-secret" not in result.model_dump_json()


def test_pagination_missing_date_and_instrument_are_partial():
    payload = {"datas": [{"营业收入": 1}], "code_count": 35}
    args = canonicalize_tool_arguments(FINANCE, {"query": "x", "page": 2, "limit": 10})
    result = normalize_query_data(json.dumps(payload), FINANCE, args)
    assert result.status == "partial" and "page=3" in result.note
    assert result.normalized[0].as_of_date is None and result.normalized[0].instrument is None
    assert len(result.normalized) == 1  # Count/page metadata are not evidence.


def test_structured_event_values_survive_evidence_builder():
    payload = {
        "code_count": 1,
        "datas": [
            {
                "股票代码": "600519.SH",
                "公告日期": "20261010",
                "机构调研内容": {"内容": "报告" * 200, "url": "https://example.test/" + "x" * 220},
            }
        ],
    }
    args = canonicalize_tool_arguments(EVENTS, {"query": "调研"})
    result = normalize_query_data(json.dumps(payload), EVENTS, args)
    item = next(e for e in build_evidence([result]) if "内容" in e.metric)
    assert isinstance(item.value, dict) and len(item.value["url"]) > 200


def test_expanded_events_keep_rows_dates_and_semantic_columns():
    payload = {
        "code_count": 1,
        "row_count": 2,
        "columns": [
            {"key": "变动日期", "type": "DATE", "index_name": "交易日期"},
            {"key": "实际解禁股数", "unit": "股"},
            {"key": "股份市值", "index_name": "实际解禁金额", "unit": "元"},
            {"key": "股份来源", "index_name": "解禁股类型"},
        ],
        "datas": [
            {
                "股票代码": "300750.SZ",
                "变动日期": date,
                "实际解禁股数": shares,
                "股份市值": 123.0,
                "股份来源": "股权激励一般股份",
                "上市日期": "20180611",
            }
            for date, shares in [("20251120", 300382), ("20260916", 404432)]
        ],
    }
    args = canonicalize_tool_arguments(EVENTS, {"query": "宁德时代解禁", "limit": 1})
    result = normalize_query_data(json.dumps(payload), EVENTS, args)
    assert result.status == "partial" and len(result.normalized) == 8
    assert {d.as_of_date for d in result.normalized} == {"2025-11-20", "2026-09-16"}
    assert "事件总数" in result.note and "page=2" not in result.note
    assert not any("上市日期" in d.metric for d in result.normalized)
    assert next(d for d in result.normalized if "股份市值" in d.metric).unit == "元"


def test_unrelated_date_cannot_date_an_event():
    payload = {
        "code_count": 1,
        "columns": [{"key": "上市日期", "type": "DATE"}],
        "datas": [{"股票代码": "300750.SZ", "上市日期": "20180611", "实际解禁股数": 12}],
    }
    args = canonicalize_tool_arguments(EVENTS, {"query": "宁德时代解禁"})
    result = normalize_query_data(json.dumps(payload), EVENTS, args)
    assert result.partial and result.normalized[0].as_of_date is None


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query": " "},
        {"query": "x", "page": 0},
        {"query": "x", "limit": 21},
        {"query": "x", "page": True},
        {"query": "x", "limit": 1.5},
        {"query": "x", "is_cache": 3},
        {"query": "x", "endpoint": "evil"},
    ],
)
def test_argument_contract_rejects_invalid_values(arguments):
    with pytest.raises(ArgumentValidationError):
        canonicalize_tool_arguments(FINANCE, arguments)


async def test_retry_header_fresh_trace_and_refresh_server_cache(mock_transport):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503) if len(requests) == 1 else httpx.Response(200, json=_payload())

    mock_transport(handler)
    runtime = ToolRuntime(_settings())
    result = await runtime.execute(FINANCE, {"query": "x"}, set(), refresh=True)
    assert result.status == "success" and len(requests) == 2
    assert [r.headers["X-Claw-Call-Type"] for r in requests] == ["normal", "retry"]
    assert len({r.headers["X-Claw-Trace-Id"] for r in requests}) == 2
    assert all(json.loads(r.content)["is_cache"] == "0" for r in requests)
    cached = await runtime.execute(FINANCE, {"query": " x ", "page": "1", "limit": "10"}, set())
    assert cached.cache_hit and len(requests) == 2
    assert _resolve_ttl(FINANCE, _settings()) == 3600
    assert _resolve_ttl(EVENTS, _settings()) == 900
    assert _resolve_ttl(BASIC, _settings()) == 86400


async def test_auth_failure_does_not_retry_or_leak(mock_transport):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(401, text="synthetic-secret")

    mock_transport(handler)
    result = await query_data(_settings(), FINANCE, {"query": "x"})
    assert len(requests) == 1 and result.status == "error" and "gateway_auth_error" in result.reason_codes
    assert "synthetic-secret" not in result.model_dump_json()


async def test_deadline_cancels_query_transport(mock_transport):
    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=_payload())

    mock_transport(handler)
    result = await ToolRuntime(_settings()).execute(FINANCE, {"query": "x"}, set(), deadline=time.monotonic() + 0.05)
    assert result.status == "error" and result.normalized == [] and "deadline_exhausted" in result.reason_codes


@pytest.mark.parametrize(
    "question,keys",
    [
        ("贵州茅台2025年营业收入、净利润和ROE", ["iwencai_finance"]),
        ("宁德时代最近有哪些机构调研和限售解禁", ["iwencai_events"]),
        ("查询510300基金资料和管理费率", ["iwencai_basicinfo"]),
        ("贵州茅台什么时候上市的", ["iwencai_basicinfo"]),
        ("贵州茅台ROE、业绩预告、上市日期", ["iwencai_finance", "iwencai_events", "iwencai_basicinfo"]),
        ("BTC资金费率和行情", []),
        ("今天A股发生了什么", []),
    ],
)
def test_required_skill_selection(question, keys):
    assert required_query_keys(question) == keys


def test_required_calls_repair_args_and_preserve_rewritten_query():
    plan = ResearchPlan(
        steps=[
            ToolCallPlan(
                tool_key="iwencai_finance", arguments={"query": "贵州茅台2025年净利润", "page": "2"}, purpose="财务"
            ),
            ToolCallPlan(tool_key="iwencai_events", arguments={}, purpose="事件"),
        ]
    )
    missing = apply_required_iwencai_tools(
        plan, question="贵州茅台2025年净利润和业绩预告", registry=registry_text(), max_steps=8
    )
    assert not missing and len(plan.steps) == 2
    assert plan.steps[0].arguments["query"] == "贵州茅台2025年净利润" and plan.steps[0].arguments["page"] == 2
    assert plan.steps[1].arguments["query"] == "贵州茅台2025年净利润和业绩预告"


def test_research_round_does_not_blindly_repeat_required_calls():
    prior = [ToolResult(tool=FINANCE, tool_key="iwencai_finance", arguments={"query": "x"}, status="error")]
    plan = ResearchPlan()
    assert not apply_required_iwencai_tools(
        plan, question="贵州茅台ROE", registry=registry_text(), max_steps=8, results=prior
    )
    assert plan.steps == []


def test_gap_fill_gets_natural_language_query():
    steps, forced = append_gap_steps([], ["iwencai_events"], registry_text(), 8, 3, question="宁德时代解禁")
    assert forced == ["iwencai_events"] and steps[0].arguments["query"] == "宁德时代解禁"


def _supervisor(plan_data, **settings_kwargs):
    node = SupervisorNode(_settings(**settings_kwargs))

    async def create(**kwargs):
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=json.dumps(plan_data)))]
        )

    node.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    return node


async def test_supervisor_and_analyst_execute_omitted_required_tool(mock_transport):
    mock_transport(lambda request: httpx.Response(200, json=_payload()))
    node = _supervisor({"intent": {"domain": "a_share", "task": "company_research"}, "steps": []})
    state = {"question": "贵州茅台2025年营收和ROE是多少"}
    routed = await node(state)
    assert routed["required_tool_keys"] == ["iwencai_finance"]
    assert routed["route"][0]["analyst"] == "fundamental"
    analyst = FundamentalAnalystNode(_settings())

    def forbidden():
        raise AssertionError("internal tool must not connect to Market Gateway")

    analyst._runtime._gateway_class = forbidden
    out = await analyst({**state, **routed})
    assert out["results"][0].tool_key == "iwencai_finance" and len(out["evidence"]) == 2


async def test_mandatory_calls_survive_full_planner_budget():
    node = _supervisor(
        {
            "intent": {"domain": "a_share", "task": "company_research"},
            "steps": [{"tool_key": "quote", "arguments": {"symbols": ["600519"]}, "purpose": "行情"}],
        },
        max_research_steps=1,
    )
    out = await node({"question": "贵州茅台ROE"})
    assert out["route"][0]["tool_calls"][0]["tool_key"] == "iwencai_finance"


async def test_insufficient_budget_for_multiple_required_calls_is_explicit():
    node = _supervisor({"intent": {"domain": "a_share", "task": "company_research"}, "steps": []}, max_research_steps=1)
    out = await node({"question": "贵州茅台ROE、业绩预告和上市日期"})
    assert out["finalize_after_gap"] and out["blocked_reason"] == "tool_call_budget_insufficient"
    assert "required_tool_budget_insufficient" in out["reason_codes"]


@pytest.mark.parametrize("operation", QUERY_TOOLS)
def test_reference_tools_are_visible_and_symbol_free(operation):
    key, _slug = QUERY_TOOLS[operation]
    meta = resolve_tool(key)
    assert not meta.requires_symbol and not meta.market_level
    assert key in registry_text(domains=["hk_stock", "cross"])
