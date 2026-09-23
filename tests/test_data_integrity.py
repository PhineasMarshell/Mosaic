"""第一批止血修复的回归测试。

每一项都对应一个"静默产出错误数据"的缺陷 —— 它们的共同特征是：
不报错、不打日志、测试全绿，但喂给 LLM 和展示给用户的东西是错的。
"""

import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.cache import Cache
from app.config import Settings
from app.errors import LLMOutputError
from app.gateway import normalizer
from app.gateway.normalizer import normalize_tool_result
from app.gateway.tool_registry import get_enabled_domains
from app.llm_json import extract_json_text, parse_json_object
from app.memory.storage import MarketMemory
from app.models.market import ToolResult
from app.models.research import ResearchPlan
from app.models.response import MarketIntelligence


def _settings(**over):
    return Settings(openai_api_key="test", **over)


# ================================================================== #
# B1.1  没有数据就必须是 error                                        #
# ================================================================== #

def test_raw_none_without_error_is_not_success():
    """HTTP 401 / MCP 失败以前会走到这里，产出 value="None" 的"成功"证据。"""
    r = normalize_tool_result("quote_tencent_quote_get", {"symbol": "sh600519"}, None)
    assert r.status == "error"
    assert r.error
    assert r.normalized == []


def test_explicit_error_still_wins():
    r = normalize_tool_result("quote_tencent_quote_get", {}, None, error="boom")
    assert r.status == "error"
    assert r.error == "boom"


def test_empty_dict_is_not_treated_as_no_data():
    """{} 是"成功但没有内容"，跟 None（连响应都没有）语义不同，不该混为一谈。"""
    r = normalize_tool_result("quote_tencent_quote_get", {}, {})
    assert r.status == "success"


# ================================================================== #
# B1.4  截断必须留末尾（时间序列）并且说出来                            #
# ================================================================== #

def _candles(n):
    """升序日线，收盘价 = 100 + i，便于断言"保留的是哪一段"。"""
    return [{"t": 1700000000000 + i * 86400000, "o": 100 + i, "h": 105 + i,
             "l": 95 + i, "c": 100 + i, "v": 1000} for i in range(n)]


def _closes(result):
    return {int(d.metric.split("[")[1].split("]")[0]): d.value
            for d in result.normalized if d.metric.endswith(".c")}


def test_time_series_truncation_keeps_the_newest():
    """回归：90 天日线只保留前 50 根 → "最新收盘"比真实值低 21%。"""
    r = normalize_tool_result(
        "klines_market_klines_post",
        {"symbol": "BTC/USDT", "interval": "1d"},
        {"candles": _candles(90), "count": 90, "partial": False},
    )
    closes = _closes(r)
    assert max(closes) == 89, "必须保留到最后一根"
    assert closes[89] == 189, "最新收盘必须是真实最新值"
    assert min(closes) == 40, "时间序列保留末尾 50 条"


def test_time_series_truncation_is_reported():
    r = normalize_tool_result(
        "klines_market_klines_post", {}, {"candles": _candles(90), "count": 90},
    )
    assert r.status == "partial"
    assert r.partial is True
    assert r.note and "90" in r.note and "50" in r.note
    assert "newest" in r.note


def test_ranked_list_truncation_keeps_the_head():
    """排行榜/名单类不是时间序列，头部才是重点。"""
    rows = [{"rank": i, "name": f"stock{i}", "value": i} for i in range(90)]
    r = normalize_tool_result(
        "public_limit_up_pool_ashare_master_limit_up_pool_get", {}, {"data": rows},
    )
    idx = sorted({int(d.metric.split("[")[1].split("]")[0])
                  for d in r.normalized if "data[" in d.metric})
    assert idx[0] == 0 and idx[-1] == 49
    assert r.partial is True and "first" in (r.note or "")


def test_no_truncation_no_partial_no_note():
    r = normalize_tool_result("klines_market_klines_post", {}, {"candles": _candles(10)})
    assert r.status == "success"
    assert r.partial is False
    assert r.note is None


def test_upstream_note_is_preserved():
    """gateway 契约：partial=true 时另有 note 说明原因。以前被 _METADATA_KEYS 丢掉。"""
    r = normalize_tool_result(
        "klines_market_klines_post", {},
        {"candles": _candles(3), "partial": True, "note": "hyperliquid 超出保留期"},
    )
    assert r.partial is True
    assert "hyperliquid" in r.note


def test_count_reaches_evidence_so_truncation_is_cross_checkable():
    from app.research.evidence import _is_tool_param

    assert not _is_tool_param("count"), "count 被滤掉后，截断就彻底不可见了"


# ================================================================== #
# B1.2  上游失败必须变成 error                                        #
# ================================================================== #

@pytest.mark.asyncio
async def test_mcp_is_error_becomes_error_result():
    """MCP 的工具级失败是带内 signalling（isError=True），以前从没被读过。"""
    from mcp.types import CallToolResult, TextContent

    from app.gateway.mcp_client import MarketGatewayClient

    client = MarketGatewayClient(_settings(market_gateway_mode="mcp"))

    class FakeSession:
        async def call_tool(self, tool_name, arguments=None):
            return CallToolResult(
                content=[TextContent(type="text", text="Server Error: symbol KPEPE not supported")],
                isError=True,
            )

    client.session = FakeSession()
    r = await client.call("hyperliquid_liqmap_coinglass_hyperliquid_liqmap_get", {"symbol": "KPEPE"})
    assert r.status == "error"
    assert "KPEPE" in (r.error or "")
    assert r.normalized == []


@pytest.mark.asyncio
async def test_mcp_success_path_unchanged():
    from mcp.types import CallToolResult, TextContent

    from app.gateway.mcp_client import MarketGatewayClient

    client = MarketGatewayClient(_settings(market_gateway_mode="mcp"))

    class FakeSession:
        async def call_tool(self, tool_name, arguments=None):
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps({"last": 79205.42}))],
                isError=False,
            )

    client.session = FakeSession()
    r = await client.call("quote_tencent_quote_get", {"symbol": "sh600519"})
    assert r.status == "success"


def test_mcp_structured_content_attribute_name():
    """以前写的是驼峰 structuredContent，hasattr 永远 False，快路径是死代码。"""
    from mcp.types import CallToolResult

    from app.gateway.mcp_client import _mcp_result_to_json

    payload = {"last": 79205.42}
    r = CallToolResult(content=[], structured_content=payload)
    assert _mcp_result_to_json(r) == payload


@pytest.mark.asyncio
async def test_http_401_is_an_error_not_a_none_success():
    """回归：last_error 在首次尝试时还是 None，于是 401 变成 value="None" 的成功证据。"""
    from app.gateway.http_client import MarketGatewayHttpClient

    client = MarketGatewayHttpClient(_settings(
        market_gateway_mode="http", market_gateway_api_key="k",
    ))

    class FakeResponse:
        status_code = 401
        text = "unauthorized"

        def raise_for_status(self):
            raise AssertionError("401 不该走到 raise_for_status")

        def json(self):
            return {}

    class FakeHttp:
        async def request(self, *a, **kw):
            return FakeResponse()

    client.client = FakeHttp()
    r = await client.call("quote_tencent_quote_get", {"symbol": "sh600519"})
    assert r.status == "error"
    assert "401" in (r.error or "")
    assert r.normalized == []


# ================================================================== #
# B1.3  写入必须真的落盘、且对别的连接可见                              #
# ================================================================== #

def _memory():
    return MarketMemory(db_path=Path(tempfile.mkdtemp()) / "t.db")


def test_writes_are_visible_to_a_second_connection():
    """回归：未提交事务只有写入方自己看得见 → 多轮对话上下文永远是空的。"""
    m = _memory()
    m.save_turn("conv-1", "那茅台呢", "RISK_ON：…")
    m.save_daily_state(data={"state_label": "RISK_ON"})
    m.save_research("那茅台呢", {"report": {"state_label": "RISK_ON"}})

    other = sqlite3.connect(str(m.db_path))
    try:
        for table in ("conversations", "daily_states", "research_records"):
            n = other.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert n == 1, f"{table} 对第二条连接不可见（写入没有提交）"
    finally:
        other.close()


def test_writes_survive_a_restart():
    """回归：进程退出时未提交事务被回滚，当天写入全部丢失。"""
    m = _memory()
    path = m.db_path
    m.save_turn("conv-1", "今天A股发生了什么？", "…")
    m.save_daily_state(data={"state_label": "RISK_OFF"})
    m.conn.close()                      # 模拟进程退出

    reopened = MarketMemory(db_path=path)
    assert "今天A股发生了什么？" in reopened.get_conversation_history("conv-1", 10)
    assert (reopened.get_daily_state() or {}).get("state_label") == "RISK_OFF"


def test_conversation_history_round_trips_across_instances():
    """investigate() 以前自己 new MarketMemory()，读不到 main.py 单例写的轮次。"""
    m = _memory()
    m.save_turn("conv-9", "第一问", "第一答")
    reader = MarketMemory(db_path=m.db_path)     # 不同实例、不同连接
    history = reader.get_conversation_history("conv-9", 10)
    assert "第一问" in history and "第一答" in history


def test_get_memory_is_a_singleton():
    from app.memory.storage import get_memory

    assert get_memory() is get_memory()


# ================================================================== #
# B1.5  cross 不是一个可以研究的市场                                   #
# ================================================================== #

def test_enabled_domains_exclude_cross_and_unknown():
    domains = get_enabled_domains()
    assert "cross" not in domains, "cross 是工具可用性标记，不是研究目标"
    assert "unknown" not in domains
    assert "a_share" in domains and "crypto" in domains


def test_api_rejects_domain_cross():
    """回归：{"question":"贵州茅台怎么样","domain":"cross"} 会去研究 BTC/USDT。"""
    from fastapi.testclient import TestClient

    import app.main as main

    resp = TestClient(main.app).post(
        "/api/ask", json={"question": "贵州茅台怎么样", "domain": "cross"}
    )
    assert resp.status_code == 400
    assert "Unsupported domain" in resp.json()["detail"]


def _plan(domain, steps=None):
    return ResearchPlan.model_validate({
        "intent": {"domain": domain, "task": "market_diagnosis", "time_scope": "today",
                   "question": "q", "needs_comparison": False, "needs_evidence": True},
        "steps": steps or [],
    })


async def _run_investigate(domain):
    """跑一次 investigate，返回实际发起的工具调用（不联网、不碰 LLM）。"""
    import app.research.market_detective as md

    detective = md.MarketDetective(_settings(market_gateway_mode="mcp"))
    called = []

    async def fake_plan(question, conversation_history=""):
        return _plan(domain)

    async def fake_execute_one(req, called_signatures):
        called.append((req.tool_name, dict(req.arguments)))
        return ToolResult(tool=req.tool_name, arguments=req.arguments,
                          status="success", normalized=[])

    async def fake_reason(question, results, evidence, history_context=""):
        return MarketIntelligence(market_state="x", state_label="Risk-On",
                                  what_happened="x", confidence="low", used_tools=[])

    class FakeMemory:
        def get_conversation_history(self, *a, **kw): return ""
        def get_context_for_question(self, *a, **kw): return ""

    detective.planner.plan = fake_plan
    detective._execute_one = fake_execute_one
    detective.reasoning.reason = fake_reason

    fresh_cache = Cache()
    orig = (md.market_cache, md.get_memory)
    md.market_cache = fresh_cache
    md.get_memory = lambda: FakeMemory()
    try:
        await detective.investigate("测试问题")
    finally:
        md.market_cache, md.get_memory = orig
    return called


@pytest.mark.asyncio
async def test_us_stock_does_not_get_bitcoin_data():
    """回归：us_stock 域零个注册工具，兜底 else 是 crypto 分支 →
    问美股拿到 BTC/USDT 的 K 线和 "Bitcoin" 的搜索，标题还写「今日美股市场情报」。"""
    called = await _run_investigate("us_stock")
    blob = json.dumps(called, ensure_ascii=False)
    assert "BTC" not in blob and "Bitcoin" not in blob, f"美股问题调用了加密工具: {called}"
    assert called == [], "没有注册工具的域不该被塞进任何兜底调用"


@pytest.mark.asyncio
async def test_macro_does_not_get_bitcoin_data():
    called = await _run_investigate("macro")
    assert called == []


@pytest.mark.asyncio
async def test_crypto_still_gets_its_fallback_tools():
    """对照组：crypto 的兜底必须照常工作，别被上一条修复误伤。"""
    called = await _run_investigate("crypto")
    tools = {name for name, _ in called}
    blob = json.dumps(called, ensure_ascii=False)
    assert "BTC" in blob, "crypto 域应该仍然注入 BTC 兜底参数"
    assert len(tools) >= 4


# ================================================================== #
# B1.6  LLM 输出解析：三个调用点统一，且失败要归到 502                  #
# ================================================================== #

@pytest.mark.parametrize("content", [
    '{"a": 1}',
    '```json\n{"a": 1}\n```',
    '```\n{"a": 1}\n```',
    'Sure! Here you go: {"a": 1} — hope that helps.',
])
def test_parse_json_object_accepts_common_shapes(content):
    assert parse_json_object(content, source="Test") == {"a": 1}


@pytest.mark.parametrize("content", ["", "   ", None])
def test_parse_json_object_rejects_empty_completion(content):
    """回归：`content or "{}"` 会把空 completion 变成合法空计划 →
    任何问题静默翻转成 domain="a_share" 并触发 9 个 A 股兜底工具。"""
    with pytest.raises(LLMOutputError):
        parse_json_object(content, source="Planner")


def test_parse_json_object_rejects_top_level_array():
    """回归：以前会在后处理里抛 AttributeError('list' has no 'get') → 500。"""
    with pytest.raises(LLMOutputError):
        parse_json_object('[{"a": 1}]', source="Reasoning")


def test_parse_json_object_rejects_unparseable():
    with pytest.raises(LLMOutputError):
        parse_json_object("抱歉，我无法回答这个问题。", source="Reasoning")


def test_extract_json_text_is_the_promoted_evaluator_helper():
    from app.agent.evaluator import _extract_json

    assert _extract_json is extract_json_text
    assert _extract_json("```json\n{\"x\": 1}\n```") == '{"x": 1}'
    assert _extract_json("") is None


def _stub_reasoning(content):
    from app.research.reasoning import ReasoningEngine

    engine = ReasoningEngine(_settings())

    async def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content), finish_reason="stop")])

    engine.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return engine


@pytest.mark.asyncio
async def test_reasoning_accepts_fenced_json():
    """回归：planner/evaluator 能救围栏，reasoning 不能 → 最后一步 502，
    而所有工具调用都已经付费了。"""
    engine = _stub_reasoning(
        '```json\n{"market_state": "走强", "what_happened": "市场走强", "confidence": "high"}\n```'
    )
    report = await engine.reason("q", [], [])
    assert report.what_happened == "市场走强"


@pytest.mark.asyncio
async def test_reasoning_evidence_without_id_graceful_degradation():
    """新行为：没有 id / id 不匹配的新格式证据被静默丢弃。"""
    # reason() 内部调用 _parse_evidence(raw_ev, []) — evidence=[] 时所有项都被丢弃
    # 但模型不应该崩溃或返回 400，而是生成一个空证据的报告
    engine = _stub_reasoning(json.dumps({
        "market_state": "走强", "what_happened": "x",
        "evidence": [
            {"id": "e-real", "source_tool": "sentiment", "metric": "risk_on",
             "value": 0.5, "status": "success"},
            {"source_tool": "klines", "metric": "price", "value": 100},  # 无 id
        ],
    }))
    report = await engine.reason("q", [], [])
    assert report.what_happened == "x"  # 报告仍然生成，只是没有证据


@pytest.mark.asyncio
async def test_reasoning_array_at_top_level_is_502_not_500():
    """LLM 返回顶层数组而非对象 → parse_json_object 报 LLMOutputError（502）。"""
    engine = _stub_reasoning('[{"a": 1}]')
    with pytest.raises(LLMOutputError):
        await engine.reason("q", [], [])


@pytest.mark.asyncio
async def test_reasoning_empty_completion_is_502():
    engine = _stub_reasoning("")
    with pytest.raises(LLMOutputError):
        await engine.reason("q", [], [])
