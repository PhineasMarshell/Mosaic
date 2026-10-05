"""T27 回归：called_signatures 的去重契约必须成立。

旧实现两个缺陷：
- 签名只在**缓存命中**时登记 → 同签名重复调用阻止不了；
- 更糟：同签名第 3 次调用因"签名已见过"返回 None，被调用方当成"无缓存"
  → **绕过缓存**再打一次真实网关（3 次调用 2 次真实请求）。
"""

import pytest

from app.cache import market_cache
from app.config import Settings
from app.graph.nodes.analysts.technical import TechnicalAnalystNode
from app.graph.tool_runtime import ToolRuntime
from app.models.market import NormalizedDatum, ToolResult


class CountingGateway:
    instances: list["CountingGateway"] = []
    calls = 0

    def __init__(self, settings):
        CountingGateway.instances.append(self)

    async def __aenter__(self):
        class _T:
            name = "quote_tencent_quote_get"

        self.tools = [_T()]
        return self

    async def __aexit__(self, *exc):
        return None

    async def call(self, tool_name, arguments, deadline=None):
        CountingGateway.calls += 1
        return ToolResult(
            tool=tool_name,
            arguments=arguments,
            status="success",
            normalized=[NormalizedDatum(metric="m", value=1.0, tool=tool_name)],
        )


@pytest.fixture(autouse=True)
def _reset():
    CountingGateway.instances = []
    CountingGateway.calls = 0
    market_cache.clear()
    yield
    market_cache.clear()


async def test_same_signature_called_three_times_hits_gateway_once():
    """同一 (tool, arguments) 连调 3 次 → 真实网关只发生 1 次。"""
    rt = ToolRuntime(Settings())
    rt._gateway_class = lambda: CountingGateway
    sigs: set[str] = set()

    results = []
    async with rt.gateway_session():
        for _ in range(3):
            results.append(
                await rt.execute("quote_tencent_quote_get", {"symbol": "600519"}, sigs)
            )

    # 旧实现这里是 2（第 3 次绕过缓存再打一次真实网关）
    assert CountingGateway.calls == 1
    assert len(CountingGateway.instances) == 1
    # 三次返回都不是"静默成功"：第 1 次真实成功，第 2/3 次明确跳过（partial + note）
    assert results[0].status == "success"
    for r in results[1:]:
        assert r.status == "partial"
        assert r.partial is True
        assert r.note, "跳过必须带说明，不允许静默"
        assert r.normalized == []


async def test_failed_call_signature_not_registered_allows_retry():
    """失败的调用不登记签名 → 同签名可以重试（登记只发生在真实成功之后）。"""
    rt = ToolRuntime(Settings())
    rt._gateway_class = lambda: CountingGateway

    async def failing_call(self, tool_name, arguments, deadline=None):
        return ToolResult(tool=tool_name, arguments=arguments, status="error", error="upstream boom")

    failing_call_original = CountingGateway.call
    CountingGateway.call = failing_call
    try:
        sigs: set[str] = set()
        r1 = await rt.execute("quote_tencent_quote_get", {"symbol": "600519"}, sigs)
        assert r1.status == "error"
        assert sigs == set(), "失败的调用不应登记签名"
        CountingGateway.call = failing_call_original  # 第二次恢复成功
        r2 = await rt.execute("quote_tencent_quote_get", {"symbol": "600519"}, sigs)
        assert r2.status == "success"
        assert len(sigs) == 1
    finally:
        CountingGateway.call = failing_call_original


async def test_duplicate_tool_calls_in_route_executed_once():
    """route 里重复的 (tool, arguments) 在 _execute_tools 层真正跳过。"""
    node = TechnicalAnalystNode(Settings())
    node._runtime._gateway_class = lambda: CountingGateway
    state = {
        "question": "贵州茅台600519怎么样",
        "route": [
            {
                "analyst": "technical",
                "budget": 3,
                "tool_calls": [
                    {"tool_key": "quote", "arguments": {"symbol": "600519"}},
                    {"tool_key": "quote", "arguments": {"symbol": "600519"}},  # 完全重复
                    {"tool_key": "quote", "arguments": {"symbol": "000001"}},
                ],
            }
        ],
    }
    out = await node(state)

    assert len(out["results"]) == 2  # 重复那条被跳过
    assert CountingGateway.calls == 2
    assert out["findings"][0]["failed"] is False
    assert out["errors"] == []
