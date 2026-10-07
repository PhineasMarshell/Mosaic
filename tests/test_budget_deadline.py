"""T23b 回归：重试预算必须按"整次调查"起算，而不是"本节点"。

旧实现：`_execute_tools` 用本节点开始时刻起算、把 research_budget_seconds 当成本
节点全额预算 → research_more 回环的第二轮 analyst 重新获得一整份预算，
"第二轮烧完再撞 main.py 的 300s 硬上限 → 仍然 504" 这条路径依然存在。
"""

import logging
import time

import pytest

from app.agent.orchestrator import Orchestrator
from app.cache import market_cache
from app.config import Settings
from app.graph.nodes.analysts.technical import TechnicalAnalystNode
from app.models.market import NormalizedDatum, ToolResult


class _RecordingGateway:
    """记录下发 deadline 的 Gateway stub。"""

    deadlines: list[float | None] = []

    def __init__(self, settings):
        pass

    async def __aenter__(self):
        class _T:
            name = "get_market_quotes"

        self.tools = [_T()]
        return self

    async def __aexit__(self, *exc):
        return None

    async def call(self, tool_name, arguments, deadline=None):
        _RecordingGateway.deadlines.append(deadline)
        return ToolResult(
            tool=tool_name,
            arguments=arguments,
            status="success",
            normalized=[NormalizedDatum(metric="m", value=1.0, tool=tool_name)],
        )


def _state(budget_deadline=None):
    state = {
        "question": "贵州茅台600519怎么样",
        "route": [
            {
                "analyst": "technical",
                "budget": 1,
                "tool_calls": [{"tool_key": "quote", "arguments": {"symbol": "600519"}}],
            }
        ],
    }
    if budget_deadline is not None:
        state["budget_deadline"] = budget_deadline
    return state


@pytest.fixture(autouse=True)
def _reset():
    _RecordingGateway.deadlines = []
    market_cache.clear()  # 全局缓存：不清会让后续用例命中缓存、根本不打网关
    yield
    market_cache.clear()


async def test_deadline_derived_from_investigation_budget_deadline():
    """state["budget_deadline"] 已快到期 → 下发的 deadline 明显早于"本节点起算 + 全额预算"。"""
    node = TechnicalAnalystNode(Settings())
    node._runtime._gateway_class = lambda: _RecordingGateway

    budget_deadline = time.monotonic() + 0.5  # 调查预算只剩 0.5s（已"过半"）
    out = await node(_state(budget_deadline))

    assert _RecordingGateway.deadlines, "deadline 未下发"
    issued = _RecordingGateway.deadlines[0]
    # 单工具均分：deadline ≈ 整次调查的截止时刻
    assert issued <= budget_deadline + 0.5
    # 旧实现（本节点起算）：deadline ≈ now + 300s —— 用这条区分
    assert issued < time.monotonic() + Settings().research_budget_seconds / 2
    assert out["errors"] == []
    assert out["findings"][0]["failed"] is False


async def test_expired_investigation_deadline_issues_immediate_deadline():
    """调查预算已耗尽 → 下发的 deadline 在过去，gateway 侧应立即以预算用尽收尾。"""
    node = TechnicalAnalystNode(Settings())
    node._runtime._gateway_class = lambda: _RecordingGateway

    out = await node(_state(budget_deadline=time.monotonic() - 1.0))

    assert _RecordingGateway.deadlines
    assert _RecordingGateway.deadlines[0] <= time.monotonic()
    # 超预算必须降级，不许炸整条链（规则 6）
    assert out["findings"][0]["failed"] is False


async def test_missing_budget_deadline_falls_back_with_warning(caplog):
    """state 没有 budget_deadline → 退回本节点起算的旧行为，且必须有 warning。"""
    node = TechnicalAnalystNode(Settings())
    node._runtime._gateway_class = lambda: _RecordingGateway

    with caplog.at_level(logging.WARNING, logger="app.graph.nodes.analysts.base"):
        out = await node(_state())

    issued = _RecordingGateway.deadlines[0]
    # 旧行为：单工具拿到接近全额 research_budget_seconds 的份额
    assert issued > time.monotonic() + Settings().research_budget_seconds * 0.9
    assert any("budget_deadline" in r.message for r in caplog.records)
    assert out["errors"] == []


async def test_orchestrator_run_injects_budget_deadline(monkeypatch):
    """Orchestrator.run 在图启动前把 budget_deadline 写进 ResearchState。"""
    orch = Orchestrator(Settings())
    captured = {}

    class _FakeGraph:
        async def ainvoke(self, state, config=None):
            captured["state"] = state
            return {}

    orch._graph = _FakeGraph()
    await orch.run("测试问题")

    state = captured["state"]
    assert state.budget_deadline is not None
    assert state.budget_deadline <= time.monotonic() + Settings().research_budget_seconds + 5
