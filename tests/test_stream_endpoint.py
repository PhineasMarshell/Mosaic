"""P2.5-6 — SSE 切图：graph.astream 逐节点进度 + 超时预算。

后端 _stream_research 从 market_detective.investigate 改为直接驱动
graph.astream(stream_mode=["updates","values"])，本文件锁：
- updates 模式产出逐节点 progress（supervisor/technical/gate/reasoning/critic）
- values 模式捕获终态，组装 ResearchResponse 后发 result 事件
- 超时分支仍发 code:"timeout" 的 result 事件
"""

import asyncio
import json as _json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.models.response import MarketIntelligence


@pytest.fixture(autouse=True)
def _reset_app_state(monkeypatch):
    monkeypatch.setattr(main, "_orchestrator", None, raising=False)
    monkeypatch.setattr(main, "_settings", None, raising=False)
    yield


def _client():
    return TestClient(main.app)


def _use_budget(monkeypatch, seconds, heartbeat=5):
    monkeypatch.setattr(
        main, "_settings",
        SimpleNamespace(
            research_budget_seconds=seconds,
            stream_heartbeat_seconds=heartbeat,
            graph_recursion_limit=25,
        ),
    )


def _read_sse(payload):
    """把 SSE 响应解析成 [(event, data), ...]。"""
    events = []
    with _client().stream("POST", "/api/ask/stream", json=payload) as resp:
        assert resp.status_code == 200
        name = None
        for line in resp.iter_lines():
            if line.startswith("event: "):
                name = line[7:].strip()
            elif line.startswith("data: ") and name:
                events.append((name, _json.loads(line[6:])))
                name = None
    return events


# ------------------------------------------------------------------ #
# Fake graph：astream 是 async generator，按脚本 yield (mode, payload)
# ------------------------------------------------------------------ #

class FakeGraph:
    """模拟 compiled LangGraph 的 astream 多模式签名。"""

    def __init__(self, script, final_state):
        self.script = script          # list of (mode, payload)
        self.final_state = final_state
        self.calls = []

    async def astream(self, input_state, stream_mode=None, config=None):
        self.calls.append((input_state, stream_mode, config))
        for mode, payload in self.script:
            yield mode, payload
        # 最后一个 values 是终态
        yield "values", self.final_state


def _make_report():
    return MarketIntelligence.model_validate({
        "title": "测试情报",
        "market_state": "震荡",
        "state_label": "Neutral",
        "what_happened": "测试用最小报告",
        "confidence": "low",
    })


def _use_fake_graph(monkeypatch, graph):
    """把 _orchestrator 替换成带 _ensure_graph 的 SimpleNamespace。"""
    orch = SimpleNamespace(_ensure_graph=lambda: graph)
    monkeypatch.setattr(main, "_orchestrator", orch)
    return orch


# ------------------------------------------------------------------ #
# 正常路径：逐节点 progress + result 含 report
# ------------------------------------------------------------------ #

def test_stream_emits_per_node_progress_then_result(monkeypatch):
    report = _make_report()
    final_state = {
        "question": "今天A股发生了什么？",
        "domain": "a_share",
        "report": report,
        "results": [],
        "cache_stats": {},
    }
    script = [
        ("updates", {"supervisor": {"intent": {"domain": "a_share"}}}),
        ("updates", {"technical": {"results": []}}),
        ("updates", {"gate": {"gate": "pass"}}),
        ("updates", {"reasoning": {"report": report}}),
        ("updates", {"critic": {"critique": {"verdict": "pass"}}}),
    ]
    graph = FakeGraph(script, final_state)
    _use_fake_graph(monkeypatch, graph)
    _use_budget(monkeypatch, seconds=30)

    events = _read_sse({"question": "今天A股发生了什么？"})
    names = [n for n, _ in events]

    # 必须有 progress 和 result
    assert "progress" in names
    assert "result" in names

    # 逐节点 progress：node 字段依次出现
    progress_nodes = [
        d.get("node") for n, d in events
        if n == "progress" and d.get("node")
    ]
    assert "supervisor" in progress_nodes
    assert "technical" in progress_nodes
    assert "gate" in progress_nodes
    assert "reasoning" in progress_nodes
    assert "critic" in progress_nodes

    # result 事件含 report
    final = [d for n, d in events if n == "result"][-1]
    assert final.get("report") is not None
    assert final["report"]["what_happened"] == "测试用最小报告"
    assert final["question"] == "今天A股发生了什么？"

    # astream 被调用且传了正确的 stream_mode
    assert len(graph.calls) == 1
    assert graph.calls[0][1] == ["updates", "values"]


def test_stream_empty_route_falls_back_to_kernel_progress(monkeypatch):
    """route 为空时 kernel 兜底，progress 里应出现 node:kernel。"""
    report = _make_report()
    final_state = {"question": "q", "domain": "a_share", "report": report,
                   "results": [], "cache_stats": {}}
    script = [
        ("updates", {"supervisor": {"route": []}}),
        ("updates", {"kernel": {"results": []}}),
        ("updates", {"gate": {"gate": "pass"}}),
        ("updates", {"reasoning": {"report": report}}),
        ("updates", {"critic": {"critique": {"verdict": "pass"}}}),
    ]
    graph = FakeGraph(script, final_state)
    _use_fake_graph(monkeypatch, graph)
    _use_budget(monkeypatch, seconds=30)

    events = _read_sse({"question": "q"})
    progress_nodes = [
        d.get("node") for n, d in events
        if n == "progress" and d.get("node")
    ]
    assert "kernel" in progress_nodes


# ------------------------------------------------------------------ #
# 超时路径：budget 耗尽 → code:"timeout"
# ------------------------------------------------------------------ #

class SlowFakeGraph:
    """astream 第一个 yield 前 sleep 10s，必然触发预算超时。"""

    async def astream(self, input_state, stream_mode=None, config=None):
        await asyncio.sleep(10)
        yield "values", {}


def test_stream_timeout_emits_timeout_result(monkeypatch):
    graph = SlowFakeGraph()
    _use_fake_graph(monkeypatch, graph)
    _use_budget(monkeypatch, seconds=1, heartbeat=0.1)

    events = _read_sse({"question": "今天A股发生了什么？"})
    final = [d for n, d in events if n == "result"][-1]
    assert final.get("code") == "timeout"
    assert final.get("question") == "今天A股发生了什么？"

    # 超时前应有 progress error 事件
    progress_steps = [d.get("step") for n, d in events if n == "progress"]
    assert "error" in progress_steps


# ------------------------------------------------------------------ #
# ToolResult 对象 → dict（values 流模式保留 Pydantic 对象）
# ------------------------------------------------------------------ #

def test_stream_converts_toolresult_objects_to_dicts(monkeypatch):
    """values 模式的 final_state.results 是 ToolResult Pydantic 对象，
    ResearchResponse.tool_results 要求 dict，后端应自动 model_dump。

    修复前：Pydantic 报 "Input should be a valid dictionary, input_type=ToolResult"。
    """
    from app.models.market import ToolResult

    report = _make_report()
    tool_result_objs = [
        ToolResult(tool="quote_tencent_quote_get", arguments={"symbol": "600519"},
                   status="success", normalized=[], error=None),
        ToolResult(tool="public_sentiment_ashare_master_sentiment_get",
                   arguments={}, status="partial", normalized=[], error=None),
    ]
    final_state = {
        "question": "q", "domain": "a_share", "report": report,
        "results": tool_result_objs, "cache_stats": {},
    }
    graph = FakeGraph([], final_state)
    _use_fake_graph(monkeypatch, graph)
    _use_budget(monkeypatch, seconds=30)

    events = _read_sse({"question": "q"})
    final = [d for n, d in events if n == "result"][-1]

    # tool_results 必须是 dict 列表
    assert isinstance(final.get("tool_results"), list)
    assert len(final["tool_results"]) == 2
    for tr in final["tool_results"]:
        assert isinstance(tr, dict), f"tool_results 元素应为 dict，实际 {type(tr)}"
    assert final["tool_results"][0]["tool"] == "quote_tencent_quote_get"
