"""Run real adapters/Runtime/planning with synthetic HTTP/LLM responses.

Live transport observations are recorded separately; these regressions do
not assert upstream reliability or complete research delivery acceptance.
"""

import asyncio
import hashlib
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
from app.graph.nodes.analysts.fundamental import FundamentalAnalystNode
from app.graph.nodes.analysts.technical import TechnicalAnalystNode
from app.graph.nodes.supervisor import SupervisorNode
from app.graph.tool_runtime import ToolRuntime
from app.research.evidence import build_evidence
from app.research.iwencai_query import normalize_query_data

SECTOR = "internal_iwencai_sector_selector"
BUSINESS = "internal_iwencai_business_query"
RESEARCH = "internal_iwencai_insresearch_query"
OPERATIONS = [SECTOR, BUSINESS, RESEARCH]


def settings(**kw):
    return Settings(_env_file=None, iwencai_api_key="synthetic-secret", openai_api_key="synthetic-model", **kw)


def payload(op):
    if op == SECTOR:
        row = {"板块代码": "881001.TI", "板块名称": "半导体", "指数类型": "同花顺行业指数", "涨跌幅[20261009]": 2}
        columns = [{"key": "涨跌幅[20261009]", "unit": "%"}]
    elif op == BUSINESS:
        row = {"股票代码": "300033.SZ", "股票简称": "同花顺", "报告期截止日": "20251231", "公告日期": "20260330",
               "项目名称": "软件销售及维护", "分类标准": "产品", "业务收入[20251231]": 100, "收入占比[20251231]": 10}
        columns = [{"key": "业务收入[20251231]", "unit": "元"}, {"key": "收入占比[20251231]", "unit": "%"}]
    else:
        row = {"股票代码": "600519.SH", "股票简称": "贵州茅台", "报告日期": "20260330", "报告期截止日": "20261231",
               "研究机构": "测试证券", "预测净利润[20261231]": 100, "原始评级": "买入", "目标价": 1500, "研报": "测试研报标题"}
        columns = [{"key": "预测净利润[20261231]", "unit": "元"}, {"key": "原始评级", "index_name": "机构研究原始评级"}, {"key": "目标价", "unit": "元"}]
    return {"status_code": 0, "code_count": 1, "columns": columns, "datas": [row]}


def arguments(op, query="查询", **kw):
    return canonicalize_tool_arguments(op, {"query": query, **kw})


def normalize(op, p=None, query="查询", **kw):
    return normalize_query_data(json.dumps(payload(op) if p is None else p), op, arguments(op, query, **kw))


@pytest.fixture(autouse=True)
def cache_isolation():
    market_cache.clear()
    yield
    market_cache.clear()


@pytest.fixture
def transport(monkeypatch):
    client = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr("app.research.iwencai.httpx.AsyncClient", lambda **kw: client(**kw, transport=httpx.MockTransport(handler), trust_env=False))

    return install


@pytest.mark.parametrize("op,ttl", [(SECTOR, 60), (BUSINESS, 3600), (RESEARCH, 3600)])
async def test_official_protocol_runtime_cache_actual_refresh_and_provenance(transport, op, ttl):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload(op))

    transport(handler)
    runtime = ToolRuntime(settings())
    runtime.set_run_id("test-new-skills")
    query = "原日期 原标的 全部条件"
    first = await runtime.execute(op, arguments(op, query), set())
    spec = THS_SKILL_REGISTRY[op]
    assert first.run_id == "test-new-skills" and first.skill_id == spec.skill_id
    assert first.transport == "http_api" and not spec.depends_on_mcp
    assert _resolve_ttl(op, settings()) == ttl and not resolve_tool(spec.logical_key).requires_symbol
    assert build_evidence([first])
    for datum in first.normalized:
        assert datum.source and datum.authority and datum.retrieved_at and datum.instrument and datum.instrument_name
        assert datum.coverage != "unknown"
    req = requests[0]
    assert str(req.url) == "https://openapi.iwencai.com/v1/query2data"
    assert req.headers["X-Claw-Skill-Id"] == spec.skill_id and req.headers["X-Claw-Skill-Version"] == "1.0.0"
    assert req.headers["X-Claw-Plugin-Id"] == req.headers["X-Claw-Plugin-Version"] == "none"
    assert len(req.headers["X-Claw-Trace-Id"]) == 64
    assert json.loads(req.content) == {"query": query, "page": "1", "limit": "10", "is_cache": "1", "expand_index": "true"}
    assert (await runtime.execute(op, arguments(op, query), set())).cache_hit and len(requests) == 1
    refreshed = await runtime.execute(op, arguments(op, query), set(), refresh=True)
    assert refreshed.refresh_requested and not refreshed.cache_hit and len(requests) == 2
    assert json.loads(requests[-1].content)["is_cache"] == "0"
    assert req.headers["X-Claw-Trace-Id"] != requests[-1].headers["X-Claw-Trace-Id"]
    assert all(spec.logical_key not in THS_CAPABILITY_MATRIX[capability] for capability in ("quote", "kline", "market_breadth"))


def test_sector_identity_classification_dates_limit_and_partial_remain_explicit():
    p = payload(SECTOR)
    p["code_count"] = 3
    p["datas"] = [{**p["datas"][0], "板块代码": f"88100{i}.TI"} for i in range(3)]
    result = normalize(SECTOR, p, query="2026年10月9日筛选涨跌幅大于1%的行业板块", limit=1)
    assert len({d.instrument for d in result.normalized}) == 1 and len(json.loads(result.raw)["datas"]) == 3
    assert result.partial and {"screening_conditions_not_verified", "upstream_page_size_exceeded"} <= set(result.reason_codes)
    datum = next(d for d in result.normalized if "涨跌幅" in d.metric)
    assert datum.instrument_scope == "industry" and datum.domain == "reference"
    assert datum.as_of_date == "2026-10-09" and datum.value["classification"]["指数类型"] == "同花顺行业指数"
    evidence = build_evidence([result])
    for text in ("今天半导体板块上涨2%", "2026年10月9日全部股票上涨", "股票881000上涨2%"):
        claim = {"claim": text, "evidence_ids": [e.id for e in evidence]}
        assert not match_claims_to_evidence({"claims": [claim]}, evidence, expected_date="2026-10-09", requested_date="2026-10-11", market_closed=True)[0]["supported"]


def test_company_business_preserves_segment_identity_report_period_and_announcement_date():
    result = normalize(BUSINESS)
    datum = result.normalized[0]
    assert datum.instrument == "SZ300033" and datum.instrument_name == "同花顺"
    assert datum.value["relationship"] == {"项目名称": "软件销售及维护", "分类标准": "产品"}
    assert datum.as_of_date == "2025-12-31" and datum.value["source_dates"]["公告日期"] == "20260330"
    assert not run_evidence_gate([result], requested_date="2026-10-09").stale_evidence
    assert datum.value["date_scope"] == "reporting_period"


def test_company_generic_counterparty_name_never_fills_company_identity():
    p = {"code_count": 1, "datas": [{"股票代码": "300033.SZ", "名称": "主要客户甲", "销售占比[20251231]": 10}], "columns": [{"key": "销售占比[20251231]", "unit": "%"}]}
    result = normalize(BUSINESS, p)
    assert result.partial and result.normalized[0].instrument_name is None
    assert result.normalized[0].value["relationship"]["名称"] == "主要客户甲"


def test_contract_event_date_is_distinct_from_business_reporting_period():
    p = payload(BUSINESS)
    p["datas"][0].update({"重大合同": "测试合同", "合同公告日期": "20261009"})
    result = normalize(BUSINESS, p)
    contract = next(d for d in result.normalized if "重大合同" in d.metric)
    assert contract.as_of_date == "2026-10-09" and contract.value["date_scope"] == "event"
    gate = run_evidence_gate([result], requested_date="2026-10-12", event_date="2026-10-09")
    assert not gate.stale_evidence


def test_forecast_publication_and_target_period_are_not_interchangeable():
    result = normalize(RESEARCH)
    datum = next(d for d in result.normalized if "预测" in d.metric)
    assert datum.as_of_date == "2026-03-30" and datum.value["field_date"] == "2026-12-31"
    assert datum.value["target_period"] == "20261231" and datum.value["institution_name"] == "测试证券"
    assert datum.value["publication_date"] == "2026-03-30" and datum.value["report_title"] == "测试研报标题"
    assert not run_evidence_gate([result], event_date="2026-03-30").stale_evidence
    assert run_evidence_gate([result], event_date="2026-12-31").stale_evidence
    evidence = build_evidence([result])
    item = next(e for e in evidence if "预测" in e.metric)
    claim = {"claim": "测试证券预测贵州茅台2026年净利润为100元", "evidence_ids": [item.id]}
    assert match_claims_to_evidence({"claims": [claim]}, evidence)[0]["supported"]
    for text in ("贵州茅台2026年已实现净利润100元", "贵州茅台实际利润为100元", "贵州茅台必然实现净利润100元"):
        matched = match_claims_to_evidence({"claims": [{**claim, "claim": text}]}, evidence)[0]
        assert not matched["supported"] and "institutional_opinion_not_realized" in matched["reason_codes"]
    assert not match_claims_to_evidence({"claims": [{**claim, "evidence_ids": ["forged"]}]}, evidence)[0]["supported"]
    for text, reason in (("其他证券预测贵州茅台2026年净利润为100元", "unattributed_institutional_opinion"),
                         ("测试证券预测贵州茅台2027年净利润为100元", "forecast_period_mismatch")):
        matched = match_claims_to_evidence({"claims": [{**claim, "claim": text}]}, evidence)[0]
        assert not matched["supported"] and reason in matched["reason_codes"]


@pytest.mark.parametrize("missing", ["研究机构", "报告日期", "股票代码", "股票简称"])
def test_incomplete_opinion_provenance_blocks_corresponding_claim(missing):
    p = payload(RESEARCH)
    del p["datas"][0][missing]
    result = normalize(RESEARCH, p)
    assert result.partial
    evidence = build_evidence([result])
    item = next(e for e in evidence if "预测" in e.metric)
    claim = {"claim": "观察：测试证券预测贵州茅台2026年净利润为100元", "evidence_ids": [item.id]}
    assert not match_claims_to_evidence({"claims": [claim]}, evidence)[0]["supported"]
    if missing == "报告日期":
        assert result.normalized[0].as_of_date is None and item.as_of_date == "unknown"
        assert item.value["field_date"] == "2026-12-31"
        assert "missing_publication_date" in result.reason_codes


@pytest.mark.parametrize("op", OPERATIONS)
@pytest.mark.parametrize("bad", ["股票价格上涨", "{}", '{"datas":[]}', '{"status_code":1,"datas":[{"股票代码":"600519.SH","预测净利润":100}]}'])
def test_text_empty_and_business_error_do_not_make_evidence(op, bad):
    result = normalize_query_data(bad, op, arguments(op))
    assert result.status == "error" and result.datum_count == 0 and not build_evidence([result])


@pytest.mark.parametrize("op", OPERATIONS)
def test_default_quote_cannot_satisfy_target_fields(op):
    p = {"code_count": 1, "datas": [{"股票代码": "600519.SH", "股票简称": "贵州茅台", "最新价": 1500, "涨跌幅[20261009]": 1}]}
    assert normalize(op, p).status == "error" and not build_evidence([normalize(op, p)])


@pytest.mark.parametrize("op", OPERATIONS)
async def test_failed_refresh_invalid_arguments_auth_error_and_deadline_never_restore_old_evidence(transport, op):
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
    invalid = await runtime.execute(op, {"query": "查询", "limit": 0}, set())
    assert invalid.reason_codes == ["invalid_arguments"] and not build_evidence([invalid])

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=payload(op))

    transport(slow)
    timeout = await runtime.execute(op, arguments(op), set(), deadline=time.monotonic() + 0.02)
    assert timeout.status == "error" and timeout.datum_count == 0 and not build_evidence([timeout])
    assert "deadline_exhausted" in timeout.reason_codes


@pytest.mark.parametrize("op", OPERATIONS)
@pytest.mark.parametrize("status", [200, 503])
async def test_probe_saves_raw_response_and_never_invents_delivery(transport, monkeypatch, capsys, tmp_path, op, status):
    from scripts import verify_iwencai as verifier

    transport(lambda request: httpx.Response(status, json=payload(op)))
    monkeypatch.setattr(verifier, "get_settings", lambda: settings(max_retry_per_tool=0))
    output = tmp_path / "probe.jsonl"
    raw_dir = tmp_path / "raw"
    monkeypatch.setattr(sys, "argv", ["verify", "查询", "--tool", THS_SKILL_REGISTRY[op].logical_key, "--output", str(output), "--raw-output-dir", str(raw_dir)])
    code = await verifier.main()
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record == json.loads(capsys.readouterr().out)
    assert record["critic_verdict"] == record["finalize"] == record["delivery"] == "not_invoked_tool_probe"
    assert record["persisted"] is False and record["refresh_requested"] and not record["cache_hit"]
    if status == 503:
        assert code == 1 and record["datum_count"] == record["evidence_count"] == 0
        assert record["raw_response_path"] is None
    else:
        assert code == 0
        raw_path = raw_dir / (record["run_id"] + ".json")
        assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == record["response_sha256"]


@pytest.mark.parametrize("question,key", [
    ("2026年10月9日筛选涨跌幅大于1%的行业板块", "iwencai_sector_selector"),
    ("涨幅前五的板块", "iwencai_sector_selector"),
    ("哪些概念板块资金净流入大于零", "iwencai_sector_selector"),
    ("同花顺主营业务构成和主要客户", "iwencai_business"),
    ("同花顺公司经营数据", "iwencai_business"),
    ("贵州茅台供应商和参控股公司", "iwencai_business"),
    ("重大合同和股权投资", "iwencai_business"),
    ("贵州茅台2026年盈利预测", "iwencai_insresearch"),
    ("贵州茅台机构观点与评级", "iwencai_insresearch"),
    ("基金评级、信用评级和ESG评级", "iwencai_insresearch"),
    ("券商金股和目标价", "iwencai_insresearch"),
])
def test_required_planning_usage(question, key):
    assert key in required_query_keys(question, "a_share")


@pytest.mark.parametrize("op,question", [
    (SECTOR, "2026年10月9日筛选涨跌幅大于1%的行业板块"),
    (BUSINESS, "同花顺2025年主营业务构成"),
    (RESEARCH, "贵州茅台2026年盈利预测"),
])
async def test_supervisor_and_analyst_production_execution_route(transport, op, question):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload(op))

    transport(handler)
    node = SupervisorNode(settings())

    async def create(**kw):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"intent": {"domain": "a_share", "task": "company_research"}, "steps": []})))])

    node.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    state = {"question": question, "run_id": "test-route-three"}
    routed = await node(state)
    assert THS_SKILL_REGISTRY[op].logical_key in routed["required_tool_keys"]
    analyst = TechnicalAnalystNode(settings()) if op == SECTOR else FundamentalAnalystNode(settings())
    outputs = await analyst({**state, **routed})
    assert any(result.tool == op for result in outputs["results"]) and outputs["evidence"]
    request = next(r for r in requests if r.headers["X-Claw-Skill-Id"] == THS_SKILL_REGISTRY[op].skill_id)
    assert json.loads(request.content)["query"] == question
