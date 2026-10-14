"""Mock HTTP and LLM responses; run actual adapters, Runtime, Supervisor and analyst.

Verify index identity, screening coverage and provenance admission. Live
upstream reliability and complete Critic/Finalize delivery are not covered.
"""

import asyncio
import json
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

from app.agent.evidence_gate import match_claims_to_evidence, run_evidence_gate
from app.cache import _resolve_ttl, market_cache
from app.config import Settings
from app.gateway.arguments import canonicalize_tool_arguments
from app.gateway.ths_skill_registry import THS_CAPABILITY_MATRIX, THS_SKILL_REGISTRY
from app.gateway.tool_registry import resolve_tool
from app.graph.iwencai_plan import required_query_keys
from app.graph.nodes.analysts.technical import TechnicalAnalystNode
from app.graph.nodes.supervisor import SupervisorNode
from app.graph.tool_runtime import ToolRuntime
from app.research.evidence import build_evidence
from app.research.iwencai_query import normalize_query_data

INDEX = "internal_iwencai_index_query"
SELECTOR = "internal_iwencai_astock_selector"


def settings(**kw):
    return Settings(_env_file=None, iwencai_api_key="synthetic-secret", openai_api_key="synthetic-model", **kw)


def payload(op):
    if op == INDEX:
        return {
            "status_code": 0,
            "code_count": 1,
            "columns": [{"key": "点位[20261009]", "unit": "点"}, {"key": "涨跌幅[20261009]", "unit": "%"}],
            "datas": [{"指数代码": "000001.SH", "指数简称": "上证指数", "点位[20261009]": 3800, "涨跌幅[20261009]": 1}],
        }
    return {
        "code_count": 100,
        "columns": [{"key": "涨跌幅[20261009]", "unit": "%"}, {"key": "净利润[20251231]", "unit": "元"}],
        "chunks_info": {"parsed_conditions": ["2026-10-09", "涨跌幅大于5%", "非ST"]},
        "datas": [{"股票代码": "600519.SH", "股票简称": "测试公司", "涨跌幅[20261009]": 6, "净利润[20251231]": 0}],
    }


def arguments(op, query="查询", **kw):
    return canonicalize_tool_arguments(op, {"query": query, **kw})


def normalize(op, p=None, query="查询", **kw):
    return normalize_query_data(json.dumps(payload(op) if p is None else p), op, arguments(op, query, **kw))


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


@pytest.mark.parametrize("op", [INDEX, SELECTOR])
async def test_runtime_fixed_official_protocol_actual_refresh_and_cache(transport, op):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload(op))

    transport(handler)
    runtime = ToolRuntime(settings())
    runtime.set_run_id("test-index-selector")
    query = "2026年10月9日 原标的 原条件"
    result = await runtime.execute(op, arguments(op, query), set())
    spec = THS_SKILL_REGISTRY[op]
    assert result.skill_id == spec.skill_id and result.run_id == "test-index-selector"
    assert result.transport == "http_api" and not spec.depends_on_mcp
    assert result.tool_key == spec.logical_key and result.raw and build_evidence([result])
    assert _resolve_ttl(op, settings()) == 60 and not resolve_tool(spec.logical_key).requires_symbol
    req = requests[0]
    assert str(req.url) == "https://openapi.iwencai.com/v1/query2data"
    assert req.headers["X-Claw-Skill-Id"] == spec.skill_id and req.headers["X-Claw-Skill-Version"] == "1.0.0"
    assert req.headers["X-Claw-Plugin-Id"] == req.headers["X-Claw-Plugin-Version"] == "none"
    assert len(req.headers["X-Claw-Trace-Id"]) == 64
    assert json.loads(req.content) == {
        "query": query,
        "page": "1",
        "limit": "10",
        "is_cache": "1",
        "expand_index": "true",
    }
    assert (await runtime.execute(op, arguments(op, query), set())).cache_hit and len(requests) == 1
    refresh = await runtime.execute(op, arguments(op, query), set(), refresh=True)
    assert refresh.refresh_requested and not refresh.cache_hit and len(requests) == 2
    assert json.loads(requests[-1].content)["is_cache"] == "0"
    assert req.headers["X-Claw-Trace-Id"] != requests[-1].headers["X-Claw-Trace-Id"]


def test_index_point_claim_numeric_date_identity_and_forged_reference_checks():
    result = normalize(INDEX)
    datum = result.normalized[0]
    assert datum.instrument == "SH000001" and datum.instrument_scope == "index" and datum.domain == "reference"
    evidence = build_evidence([result])
    assert evidence[0].instrument_name == "上证指数" and evidence[0].value["value"] == 3800
    claim = {"claim": "2026年10月9日上证指数点位为3800点", "evidence_ids": [evidence[0].id]}
    assert match_claims_to_evidence({"claims": [claim]}, evidence, expected_date="2026-10-09")[0]["supported"]
    for text in [
        "2026年10月9日上证指数点位为9999点",
        "2026年10月12日上证指数点位为3800点",
        "2026年10月9日沪深300点位为3800点",
        "000001股票股价为3800元",
    ]:
        match = match_claims_to_evidence({"claims": [{**claim, "claim": text}]}, evidence, expected_date="2026-10-09")[
            0
        ]
        assert not match["supported"], (text, match)
    forged = match_claims_to_evidence({"claims": [{**claim, "evidence_ids": ["forged"]}]}, evidence)[0]
    assert not forged["supported"]


def test_missing_source_is_blocked_even_without_a_requested_date():
    evidence = build_evidence([normalize(INDEX)])
    evidence[0].source = evidence[0].authority = None
    claim = {"claim": "上证指数点位为3800点", "evidence_ids": [evidence[0].id]}
    matched = match_claims_to_evidence({"claims": [claim]}, evidence)[0]
    assert not matched["supported"] and "unknown_source" in matched["reason_codes"]


def test_weekend_index_observation_cannot_be_called_current_weekend_market():
    evidence = build_evidence([normalize(INDEX)])
    claim = {"claim": "今天上证指数点位为3800点", "evidence_ids": [evidence[0].id]}
    matched = match_claims_to_evidence(
        {"claims": [claim]},
        evidence,
        expected_date="2026-10-09",
        requested_date="2026-10-11",
        market_closed=True,
    )[0]
    assert not matched["supported"] and "closed_day_current_market_claim" in matched["reason_codes"]


@pytest.mark.parametrize("op", [INDEX, SELECTOR])
@pytest.mark.parametrize(
    "bad",
    [
        "plain text: 股票价格上涨",
        "{}",
        '{"datas":[]}',
        '{"status_code":1,"datas":[{"指数代码":"000001.SH","点位[20261009]":3800}]}',
    ],
)
def test_text_empty_and_business_error_never_make_evidence(op, bad):
    result = normalize_query_data(bad, op, arguments(op))
    assert result.status == "error" and result.datum_count == 0 and not build_evidence([result])


def test_default_stock_table_does_not_prove_index_identity():
    p = {"code_count": 1, "datas": [{"股票代码": "000001.SZ", "股票简称": "平安银行", "最新价": 12}]}
    result = normalize(INDEX, p)
    assert result.status == "error" and not build_evidence([result])
    assert "iwencai_index" not in THS_CAPABILITY_MATRIX["quote"]
    assert "iwencai_astock_selector" not in THS_CAPABILITY_MATRIX["quote"]


@pytest.mark.parametrize("code", [".IXIC", ".IXIC.GI", "^GSPC", "HSI.GI", "@CCO.NQI"])
def test_overseas_index_identifiers_are_preserved_without_equity_inference(code):
    p = payload(INDEX)
    p["datas"][0]["指数代码"] = code
    p["datas"][0]["指数简称"] = "海外指数"
    result = normalize(INDEX, p)
    assert all(
        d.instrument == code and d.instrument_scope == "index" and d.domain == "reference" for d in result.normalized
    )


@pytest.mark.parametrize("missing", ["date", "code", "name"])
def test_incomplete_index_provenance_does_not_support_named_current_claim(missing):
    p = payload(INDEX)
    row = p["datas"][0]
    if missing == "date":
        p["datas"] = [{"指数代码": "000001.SH", "指数简称": "上证指数", "点位": 3800}]
        p["columns"] = [{"key": "点位", "unit": "点"}]
    elif missing == "code":
        del row["指数代码"]
    else:
        del row["指数简称"]
    result = normalize(INDEX, p)
    assert result.partial
    evidence = build_evidence([result])
    claim = {"claim": "今天上证指数点位为3800点", "evidence_ids": [evidence[0].id]}
    assert not match_claims_to_evidence({"claims": [claim]}, evidence, expected_date="2026-10-09")[0]["supported"]


def test_selector_page_conditions_and_mixed_dates_remain_unverified():
    query = "2026年10月9日 筛选A股 涨跌幅大于5% 非ST"
    result = normalize(SELECTOR, query=query, limit=1)
    assert result.partial and result.arguments["query"] == query
    assert "screening_conditions_not_verified" in result.reason_codes
    assert "page=2" in result.note and "非ST" in json.loads(result.raw)["chunks_info"]["parsed_conditions"]
    assert result.normalized[1].value["value"] == 0 and result.normalized[1].value["date_scope"] == "reporting_period"
    gate = run_evidence_gate([result], requested_date="2026-10-09")
    assert not gate.stale_evidence
    # A preceding annual field cannot erase the following market date check.
    result.normalized.reverse()
    gate = run_evidence_gate([result], requested_date="2026-10-12")
    assert len(gate.stale_evidence) == 1 and "涨跌幅" in result.normalized[1].metric
    evidence = build_evidence([result])
    claim = {"claim": "2026年10月9日全市场所有A股上涨", "evidence_ids": [e.id for e in evidence]}
    assert not match_claims_to_evidence({"claims": [claim]}, evidence, expected_date="2026-10-09")[0]["supported"]


def test_upstream_ignoring_limit_is_bounded_without_claiming_pagination_complete():
    p = payload(SELECTOR)
    p["code_count"] = 3
    p["datas"] = [{**p["datas"][0], "股票代码": f"60000{i}.SH"} for i in range(3)]
    result = normalize(SELECTOR, p, limit=1)
    assert len({d.instrument for d in result.normalized}) == 1
    assert len(json.loads(result.raw)["datas"]) == 3
    assert "upstream_page_size_exceeded" in result.reason_codes and "仅保留前1行" in result.note


def test_selector_default_quote_does_not_prove_requested_financial_fields():
    p = {"code_count": 1, "datas": [{"股票代码": "600519.SH", "股票简称": "测试公司", "最新价": 10}]}
    result = normalize(SELECTOR, p, query="筛选A股 ROE大于10%")
    assert result.status == "error" and not build_evidence([result])


@pytest.mark.parametrize("op", [INDEX, SELECTOR])
async def test_timeout_failed_refresh_and_auth_never_restore_old_partial(transport, op):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=payload(op)) if calls == 1 else httpx.Response(401, text="synthetic-secret")

    transport(handler)
    runtime = ToolRuntime(settings(max_retry_per_tool=0))
    assert (await runtime.execute(op, arguments(op), set())).normalized
    failed = await runtime.execute(op, arguments(op), set(), refresh=True)
    assert failed.status == "error" and failed.datum_count == 0 and not build_evidence([failed])
    assert "synthetic-secret" not in failed.model_dump_json()
    assert (await runtime.execute(op, arguments(op), set())).status == "error" and calls == 3

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=payload(op))

    transport(slow)
    failed = await runtime.execute(op, arguments(op), set(), deadline=time.monotonic() + 0.02)
    assert failed.status == "error" and "deadline_exhausted" in failed.reason_codes and not build_evidence([failed])


@pytest.mark.parametrize("op", [INDEX, SELECTOR])
async def test_invalid_arguments_have_stable_reason_and_zero_evidence(op):
    result = await ToolRuntime(settings()).execute(op, {"query": "查询", "limit": 0}, set())
    assert result.status == "error" and result.reason_codes == ["invalid_arguments"]
    assert result.datum_count == 0 and not build_evidence([result])


@pytest.mark.parametrize("op", [INDEX, SELECTOR])
@pytest.mark.parametrize("http_status", [200, 503])
async def test_verifier_persists_tool_probe_without_fabricating_delivery(
    monkeypatch,
    transport,
    capsys,
    tmp_path,
    op,
    http_status,
):
    from scripts import verify_iwencai as verifier

    transport(lambda request: httpx.Response(http_status, json=payload(op)))
    monkeypatch.setattr(verifier, "get_settings", lambda: settings(max_retry_per_tool=0))
    output = tmp_path / "probe.jsonl"
    monkeypatch.setattr(
        sys, "argv", ["verify", "查询", "--tool", THS_SKILL_REGISTRY[op].logical_key, "--output", str(output)]
    )
    exit_code = await verifier.main()
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record == json.loads(capsys.readouterr().out)
    assert record["run_id"] and record["transport"] == "http_api"
    assert record["critic_verdict"] == record["finalize"] == record["delivery"] == "not_invoked_tool_probe"
    assert not record["persisted"] and record["refresh_requested"] and not record["cache_hit"]
    if http_status == 503:
        assert exit_code == 1 and record["status"] == "error"
        assert record["datum_count"] == record["evidence_count"] == 0
    else:
        assert exit_code == 0 and record["evidence_count"] > 0


@pytest.mark.parametrize(
    "question,domain,key",
    [
        ("上证指数今天多少点", "a_share", "iwencai_index"),
        ("恒生指数2026年10月9日行情", "hk_stock", "iwencai_index"),
        ("纳斯达克指数涨跌幅", "us_stock", "iwencai_index"),
        ("沪深300", "a_share", "iwencai_index"),
        ("2026年10月9日筛选A股 涨跌幅大于5% 非ST", "a_share", "iwencai_astock_selector"),
        ("哪些股票符合均线多头和净利润增长条件", "a_share", "iwencai_astock_selector"),
    ],
)
def test_mandatory_usage(question, domain, key):
    keys = required_query_keys(question, domain)
    assert keys[0] == key
    if key == "iwencai_index":
        assert "iwencai_market" not in keys


@pytest.mark.parametrize(
    "question,domain",
    [
        ("贵州茅台股价", "a_share"),
        ("筛选港股 涨跌幅大于5%", "hk_stock"),
        ("筛选ETF", "a_share"),
        ("上证指数成分股", "a_share"),
    ],
)
def test_non_screening_and_index_composition_boundaries(question, domain):
    assert not {"iwencai_index", "iwencai_astock_selector"}.intersection(required_query_keys(question, domain))


@pytest.mark.parametrize(
    "op,question", [(INDEX, "2026年10月9日上证指数点位"), (SELECTOR, "2026年10月9日筛选A股 非ST 涨跌幅大于5%")]
)
async def test_supervisor_and_technical_analyst_use_new_tools(transport, op, question):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload(op))

    transport(handler)
    node = SupervisorNode(settings())

    async def create(**kw):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({"intent": {"domain": "a_share", "task": "company_research"}, "steps": []})
                    )
                )
            ]
        )

    node.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    state = {"question": question, "run_id": "test-production-route"}
    routed = await node(state)
    key = THS_SKILL_REGISTRY[op].logical_key
    assert key in routed["required_tool_keys"]
    assert not routed.get("errors")
    outputs = await TechnicalAnalystNode(settings())({**state, **routed})
    assert any(r.tool == op for r in outputs["results"]) and outputs["evidence"]
    assert outputs.get("errors") == []
    request = next(r for r in requests if r.headers["X-Claw-Skill-Id"] == THS_SKILL_REGISTRY[op].skill_id)
    assert json.loads(request.content)["query"] == question
