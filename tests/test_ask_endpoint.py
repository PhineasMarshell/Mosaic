"""`/api/ask` 的错误分类与超时预算。

这组测试锁的是 2026-09 那次排查出来的行为：
- 校验拒绝必须留下日志（以前只有一条裸的 `400 Bad Request`，无从定位）
- 上游 LLM 输出坏了是 502，不是 400
- 调查超出总预算是 504，而且同步端点**必须**有超时（以前能挂死浏览器）
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.errors import LLMOutputError
from app.gateway.tool_registry import get_enabled_domains
from app.logging_config import setup_logging


@pytest.fixture(autouse=True)
def _reset_app_state(monkeypatch):
    """每个测试都从干净的全局状态开始，且不让 lifespan 真的跑起来。"""
    monkeypatch.setattr(main, "_orchestrator", None, raising=False)
    monkeypatch.setattr(main, "_settings", None, raising=False)
    yield


@pytest.fixture
def caplog_mosaic(caplog):
    """setup_logging 默认关掉了 propagate，caplog 的 handler 挂在 root 上抓不到。"""
    setup_logging(level="DEBUG", json_format=False, propagate=True)
    yield caplog
    setup_logging(level="INFO", json_format=False)


class FakeOrchestrator:
    """按脚本行为的 orchestrator 替身。"""

    def __init__(self, exc=None, delay=0.0):
        self.exc = exc
        self.delay = delay
        self.calls = []

    async def run(self, question, domain=None, conversation_id=None):
        self.calls.append((question, domain, conversation_id))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        raise AssertionError("FakeOrchestrator 没有被配置成成功返回")


def _client():
    # 不用 with —— 避免触发 lifespan（会初始化 Orchestrator / MCP / 定时任务）
    return TestClient(main.app)


def _use_orchestrator(monkeypatch, orch):
    monkeypatch.setattr(main, "_orchestrator", orch)
    return orch


def _use_budget(monkeypatch, seconds):
    monkeypatch.setattr(
        main, "_settings",
        SimpleNamespace(research_budget_seconds=seconds, stream_heartbeat_seconds=5),
    )


# ------------------------------------------------------------------ #
# 校验层：400 + 必须有日志                                            #
# ------------------------------------------------------------------ #

@pytest.mark.parametrize("payload", [
    {},
    {"question": ""},
    {"question": "   "},
    {"question": None},
    {"question": 123},
    {"question": {}},          # 前端误传 DOM 节点被 JSON 化成 {} 的情形
    {"q": "今天A股发生了什么？"},  # 字段名写错
])
def test_empty_or_non_string_question_is_400(payload, caplog_mosaic):
    resp = _client().post("/api/ask", json=payload)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "question cannot be empty"
    # 关键回归点：拒绝必须可观测
    assert any(r.levelno >= logging.WARNING for r in caplog_mosaic.records), \
        "400 拒绝没有留下任何 WARNING 日志"


def test_unsupported_domain_is_400(caplog_mosaic):
    resp = _client().post("/api/ask", json={"question": "今天A股发生了什么？", "domain": "not_a_domain"})
    assert resp.status_code == 400
    assert "Unsupported domain" in resp.json()["detail"]
    assert any(r.levelno >= logging.WARNING for r in caplog_mosaic.records)


def test_supported_domain_passes_validation(monkeypatch):
    domain = get_enabled_domains()[0]
    orch = _use_orchestrator(monkeypatch, FakeOrchestrator(exc=RuntimeError("boom")))
    resp = _client().post("/api/ask", json={"question": "看看行情", "domain": domain})
    # 走到研究了（500 来自 RuntimeError），说明没被 domain 校验拦下
    assert resp.status_code == 500
    assert orch.calls[0][1] == domain


@pytest.mark.parametrize("endpoint", ["/api/ask", "/api/ask/stream"])
def test_both_endpoints_share_validation(endpoint):
    resp = _client().post(endpoint, json={"question": ""})
    assert resp.status_code == 400


# ------------------------------------------------------------------ #
# 研究层：错误分类                                                    #
# ------------------------------------------------------------------ #

def test_llm_output_error_is_502_not_400(monkeypatch, caplog_mosaic):
    _use_orchestrator(monkeypatch, FakeOrchestrator(
        exc=LLMOutputError("Planner returned invalid JSON: raw='抱歉，我无法…'")
    ))
    resp = _client().post("/api/ask", json={"question": "今天A股发生了什么？"})
    assert resp.status_code == 502, "上游模型输出坏了不该报成客户端错误"
    assert not resp.json()["detail"].startswith("Planner returned")  # 不把原始 prompt 回吐给客户端
    assert any(r.levelno >= logging.ERROR for r in caplog_mosaic.records)


def test_plain_value_error_still_400(monkeypatch):
    _use_orchestrator(monkeypatch, FakeOrchestrator(exc=ValueError("question cannot be empty")))
    resp = _client().post("/api/ask", json={"question": "今天A股发生了什么？"})
    assert resp.status_code == 400


def test_unexpected_error_is_500(monkeypatch):
    _use_orchestrator(monkeypatch, FakeOrchestrator(exc=RuntimeError("gateway exploded")))
    resp = _client().post("/api/ask", json={"question": "今天A股发生了什么？"})
    assert resp.status_code == 500


# ------------------------------------------------------------------ #
# 预算层：同步端点必须会超时                                          #
# ------------------------------------------------------------------ #

def test_sync_endpoint_times_out_with_504(monkeypatch, caplog_mosaic):
    """回归：以前 /api/ask 没有任何总超时，浏览器只能干等到用户刷新页面。"""
    _use_orchestrator(monkeypatch, FakeOrchestrator(delay=5.0))
    _use_budget(monkeypatch, seconds=0.05)

    resp = _client().post("/api/ask", json={"question": "今天A股发生了什么？"})
    assert resp.status_code == 504
    assert "timed out" in resp.json()["detail"]
    assert any(r.levelno >= logging.WARNING for r in caplog_mosaic.records)


def test_budget_and_per_call_timeout_are_decoupled():
    """外层预算必须显著大于单次工具调用超时，否则研究必然被掐断。"""
    from app.config import get_settings

    s = get_settings()
    assert s.research_budget_seconds >= s.research_timeout_seconds * 4
    assert s.research_budget_seconds >= s.llm_timeout_seconds * 2
    assert s.stream_heartbeat_seconds < s.research_budget_seconds


def test_health_exposes_budget():
    """前端靠它对齐客户端兜底时限。"""
    body = _client().get("/health").json()
    assert body["research_budget_seconds"] > 0


# ------------------------------------------------------------------ #
# SSE：心跳保活 + 用 result 事件表达失败                              #
# ------------------------------------------------------------------ #

class FakeDetective:
    def __init__(self, exc=None, delay=0.0):
        self.exc = exc
        self.delay = delay

    async def investigate(self, question, domain=None, conversation_id=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        raise AssertionError("FakeDetective 没有被配置成成功返回")


def _use_detective(monkeypatch, detective):
    monkeypatch.setattr(main, "_orchestrator", SimpleNamespace(market_detective=detective))


def _read_sse(payload):
    """把 SSE 响应解析成 [(event, data), ...]。"""
    import json as _json

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


def test_stream_emits_heartbeats_then_timeout(monkeypatch):
    """回归：以前整个调查期间连接上一个字节都没有，且 60s 必然超时。"""
    _use_detective(monkeypatch, FakeDetective(delay=5.0))
    monkeypatch.setattr(
        main, "_settings",
        SimpleNamespace(research_budget_seconds=0.25, stream_heartbeat_seconds=0.05),
    )

    events = _read_sse({"question": "今天A股发生了什么？"})
    names = [n for n, _ in events]
    assert "progress" in names and "result" in names

    # 调查期间必须有心跳，否则浏览器/代理会把静默连接掐掉
    working = [d for n, d in events if n == "progress" and d.get("step") == "working"]
    assert len(working) >= 1, f"没有收到任何心跳事件: {events}"

    final = [d for n, d in events if n == "result"][-1]
    assert final.get("code") == "timeout"
    assert final.get("question") == "今天A股发生了什么？"


def test_stream_reports_llm_output_error_as_upstream(monkeypatch):
    _use_detective(monkeypatch, FakeDetective(exc=LLMOutputError("Reasoning returned invalid JSON")))
    _use_budget(monkeypatch, seconds=5)

    events = _read_sse({"question": "那茅台呢"})
    final = [d for n, d in events if n == "result"][-1]
    assert final.get("code") == "upstream"
    assert "invalid JSON" not in final.get("error", "")  # 不外泄原始上游内容


def test_stream_reports_generic_error_as_internal(monkeypatch):
    _use_detective(monkeypatch, FakeDetective(exc=RuntimeError("gateway exploded")))
    _use_budget(monkeypatch, seconds=5)

    events = _read_sse({"question": "那茅台呢"})
    final = [d for n, d in events if n == "result"][-1]
    assert final.get("code") == "internal"
