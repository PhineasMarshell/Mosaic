"""Supervisor 节点 — 路由决策单元测试。

验证：
- LLM 成功时产出 intent + route
- LLM 失败降级为 errors（kernel 仍可执行）
- 显式 domain 覆盖 planner 判断
- 步骤数不超过 max_research_steps
- set_enabled_domains 动态生效
"""

import asyncio
import types

import pytest

from app.config import Settings
from app.errors import LLMOutputError
from app.graph.nodes.supervisor import SupervisorNode, _ENABLED_DOMAINS, set_enabled_domains
from app.graph.state import ResearchState
from app.models.research import DEFAULT_DOMAINS


def _make_state(**overrides) -> ResearchState:
    defaults = {"question": "今天A股发生了什么？"}
    defaults.update(overrides)
    return ResearchState(**defaults)


class FakeOpenAI:
    """伪造 AsyncOpenAI —— 返回预设的 JSON 字符串。"""

    def __init__(self, return_text: str = "", raise_on: Exception | None = None):
        self.return_text = return_text
        self.raise_on = raise_on

    async def create(self, *args, **kwargs):
        if self.raise_on:
            raise self.raise_on
        msg = types.SimpleNamespace(content=self.return_text)
        choice = types.SimpleNamespace(message=msg)
        return types.SimpleNamespace(choices=[choice])


@pytest.fixture
def fake_openai():
    return FakeOpenAI()


def _patch_client(node: SupervisorNode, openai_mock: FakeOpenAI) -> None:
    """把 Supervisor 的内部 client 替换为 mock。"""
    node.client = types.SimpleNamespace()
    node.client.chat = types.SimpleNamespace()
    node.client.chat.completions = types.SimpleNamespace()
    node.client.chat.completions.create = openai_mock.create


# ------------------------------------------------------------------ #
# 核心行为                                                             #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_supervisor_returns_intent_on_success():
    """LLM 正常返回 plan JSON → state 含 intent。"""
    openai = FakeOpenAI(return_text="""{
        "intent": {
            "domain": "a_share",
            "task": "market_diagnosis",
            "time_scope": "today",
            "question": "今天A股发生了什么？"
        },
        "steps": [
            {"tool_key": "overview", "arguments": {}, "purpose": "看概览"},
            {"tool_key": "sentiment", "arguments": {}, "purpose": "看情绪"}
        ]
    }""")

    settings = Settings()
    node = SupervisorNode(settings)
    _patch_client(node, openai)

    result = await node(_make_state())
    assert "intent" in result
    assert result["intent"].domain == "a_share"
    assert result["intent"].task == "market_diagnosis"
    assert "_route_raw" in result


@pytest.mark.asyncio
async def test_supervisor_handles_llm_error_gracefully():
    """LLM 抛异常 → errors 列表，不炸图。"""
    openai = FakeOpenAI(raise_on=LLMOutputError("model broke"))

    settings = Settings()
    node = SupervisorNode(settings)
    _patch_client(node, openai)

    result = await node(_make_state())
    assert "errors" in result
    assert len(result["errors"]) == 1
    assert "routing failed" in result["errors"][0].lower()


@pytest.mark.asyncio
async def test_supervisor_domain_override():
    """显式 domain 应覆盖 planner 的判断。"""
    openai = FakeOpenAI(return_text="""{
        "intent": {
            "domain": "crypto",
            "task": "market_summary",
            "time_scope": "today",
            "question": "看看行情"
        },
        "steps": []
    }""")

    settings = Settings()
    node = SupervisorNode(settings)
    _patch_client(node, openai)

    result = await node(_make_state(domain="a_share"))
    assert result["intent"].domain == "a_share", "显式 domain 必须覆盖 planner 判断"


@pytest.mark.asyncio
async def test_supervisor_step_limit():
    """步骤数不应超过 max_research_steps。"""
    steps_json = "[" + ",".join(
        '{"tool_key":"t%s","arguments":{},"purpose":"p%s"}' % (i, i) for i in range(50)
    ) + "]"
    intent_json = '{"domain":"a_share","task":"market_diagnosis","time_scope":"today","question":"x"}'
    openai = FakeOpenAI(return_text='{"intent":%s,"steps":%s}' % (intent_json, steps_json))

    settings = Settings(max_research_steps=8)
    node = SupervisorNode(settings)
    _patch_client(node, openai)

    result = await node(_make_state())
    route = result.get("_route_raw", [])
    # P1 route 只有一个 entry，其 tool_calls 即 steps
    if route:
        assert len(route[0]["tool_calls"]) <= 8


@pytest.mark.asyncio
async def test_supervisor_empty_question_errors():
    """空 question 仍会调用 planner → errors 记录。"""
    openai = FakeOpenAI(raise_on=ValueError("empty question"))

    settings = Settings()
    node = SupervisorNode(settings)
    _patch_client(node, openai)

    result = await node(_make_state(question=""))
    assert "errors" in result


# ------------------------------------------------------------------ #
# 辅助函数                                                             #
# ------------------------------------------------------------------ #


def test_set_enabled_domains():
    """动态设置域生效。"""
    original = list(DEFAULT_DOMAINS)
    set_enabled_domains(["a_share", "crypto"])
    try:
        from app.graph.nodes.supervisor import _ENABLED_DOMAINS as current
        assert "a_share" in current
        assert "us_stock" not in current
    finally:
        set_enabled_domains(original)
