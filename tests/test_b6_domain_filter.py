"""B6：域过滤在生产链路生效 + cross 工具 purpose 边界声明 + 零成本域守卫。

背景（2026-10-06 实测缺陷）：supervisor 用 ``registry_text()`` 不传 ``domains=``，
planner 永远看到全部 44 条工具 —— 给 ``domain="us_stock"`` 的 AAPL 问题挑了
binance 的 snapshot（必然 422）。修法三步：
  1. 显式域时 ``registry_text(domains=[domain, "cross"])``；
  2. cross 三条（klines/snapshot/window）purpose 加边界声明（真正防错的一步）；
  3. 守卫：step key 不在本次渲染文本里 → 不执行 + ``tool_call_filtered`` 记账。

本文件 mock 掉了什么：supervisor 的 AsyncOpenAI client（FakeOpenAI 返回预设
plan JSON）——因此不覆盖真实 LLM 行为；registry_text 断言为纯函数直测，不 mock。
"""

import types

import pytest

from app.gateway.tool_registry import registry_text
from app.graph.nodes.supervisor import SupervisorNode

# ------------------------------------------------------------------ #
# 步 1：registry_text 域过滤（严格等值匹配，cross 必须显式带）           #
# ------------------------------------------------------------------ #


def _keys(text: str) -> set[str]:
    """渲染文本里每行 '- <key>: ...' 的 key 集合。"""
    return {line[2:].split(":")[0] for line in text.splitlines() if line.startswith("- ")}


def test_registry_text_us_stock_exact_six():
    text = registry_text(domains=["us_stock"])
    keys = _keys(text)
    assert keys == {"us_klines", "us_window", "us_fundamentals", "us_filings_recent", "health", "market_health"}
    assert "snapshot" not in keys and "longhu" not in keys and "sentiment" not in keys
    assert "list_stock_discussions" not in text


def test_registry_text_a_share_excludes_us_and_snapshot():
    keys = _keys(registry_text(domains=["a_share"]))
    assert not any(k.startswith("us_") for k in keys)
    assert "snapshot" not in keys


def test_registry_text_crypto_needs_explicit_cross():
    """守住「严格等值匹配 + crypto 必须显式带 cross」——防止后来者『优化』掉 cross。"""
    crypto_only = _keys(registry_text(domains=["crypto"]))
    assert "snapshot" not in crypto_only
    assert "klines" not in crypto_only
    crypto_cross = _keys(registry_text(domains=["crypto", "cross"]))
    assert {"snapshot", "klines", "window", "news_search"} <= crypto_cross


def test_registry_text_us_stock_with_cross_includes_snapshot():
    """已知残余风险固化：带 cross 会把 snapshot 重新暴露给美股查询，
    靠 snapshot 的 purpose 边界声明兜住（见 test_cross_purpose_boundary）。"""
    assert "snapshot" in _keys(registry_text(domains=["us_stock", "cross"]))


# ------------------------------------------------------------------ #
# 步 2：cross 工具 purpose 边界声明（文本契约，防被改回去）              #
# ------------------------------------------------------------------ #


def test_cross_purpose_boundary():
    from app.gateway.tool_registry import BY_KEY

    assert "不支持" in BY_KEY["snapshot"].purpose
    assert "A股" in BY_KEY["snapshot"].purpose
    assert "us_klines" in BY_KEY["klines"].purpose
    assert "us_klines" in BY_KEY["window"].purpose


# ------------------------------------------------------------------ #
# 步 3：零成本域守卫（supervisor._plan 产出后校验）                     #
# ------------------------------------------------------------------ #


class FakeOpenAI:
    """返回预设 plan JSON，并记录 last_prompt（供断言域过滤真的进了 prompt）。"""

    def __init__(self, plan_json: str):
        self.plan_json = plan_json
        self.last_prompt: str | None = None

    async def create(self, *args, **kwargs):
        messages = kwargs.get("messages")
        if messages is None and args:
            messages = args[0]
        if isinstance(messages, list):
            self.last_prompt = "\n".join(
                m.get("content", "") for m in messages if isinstance(m, dict) and m.get("role") == "user"
            )
        msg = types.SimpleNamespace(content=self.plan_json)
        choice = types.SimpleNamespace(message=msg)
        return types.SimpleNamespace(choices=[choice])


def _make_node(plan_json: str) -> tuple[SupervisorNode, FakeOpenAI]:
    node = SupervisorNode.__new__(SupervisorNode)
    node.settings = types.SimpleNamespace(
        max_conversation_turns=10,
        max_research_steps=8,
        openai_model="test-model",
    )
    fake = FakeOpenAI(plan_json)
    node.client = types.SimpleNamespace()
    node.client.chat = types.SimpleNamespace()
    node.client.chat.completions = types.SimpleNamespace()
    node.client.chat.completions.create = fake.create
    return node, fake


_PLAN_JSON = (
    '{"intent":{"domain":"us_stock","task":"market_diagnosis",'
    '"time_scope":"recent","question":"AAPL"},'
    '"steps":['
    '{"tool_key":"us_klines","arguments":{},"purpose":"K线"},'
    '{"tool_key":"longhu","arguments":{},"purpose":"串域幻觉"}]'
    "}"
)


@pytest.mark.asyncio
async def test_explicit_domain_filters_registry_in_prompt():
    """显式域时 planner 的 prompt 只含该域 + cross 的工具。"""
    node, fake = _make_node(_PLAN_JSON)
    await node._plan({"question": "AAPL", "domain": "us_stock"})
    assert "- us_klines:" in fake.last_prompt
    assert "- longhu:" not in fake.last_prompt  # a_share 工具不可见
    assert "- snapshot:" in fake.last_prompt  # cross 工具可见（含残余风险）


@pytest.mark.asyncio
async def test_no_explicit_domain_keeps_full_registry():
    """无显式域时保持全量注册表——planner 要自己判域，不能预先过滤。"""
    node, fake = _make_node(_PLAN_JSON)
    await node._plan({"question": "今天A股怎么样"})
    assert "- longhu:" in fake.last_prompt
    assert "- us_klines:" in fake.last_prompt


@pytest.mark.asyncio
async def test_out_of_domain_step_is_filtered_and_recorded():
    """守卫：不在渲染文本里的 step 不执行，errors 记 tool_call_filtered。"""
    node, _fake = _make_node(_PLAN_JSON)
    plan, filtered, _adjustments = await node._plan({"question": "AAPL", "domain": "us_stock"})
    assert filtered == ["longhu"]
    assert [s.tool_key for s in plan.steps] == ["us_klines"]


@pytest.mark.asyncio
async def test_call_returns_tool_call_filtered_error():
    """__call__ 出口：tool_call_filtered 进 state.errors（kernel 可见）。"""
    node, _fake = _make_node(_PLAN_JSON)
    result = await node({"question": "AAPL", "domain": "us_stock"})
    assert any("tool_call_filtered: longhu" in e for e in result.get("errors", []))
    # route 里也不再有被过滤的 step
    routed_keys = {c["tool_key"] for r in result["route"] for c in r["tool_calls"]}
    assert routed_keys == {"us_klines"}
