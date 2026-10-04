"""T18 回归：analyst 一次运行只创建一个 Gateway 客户端，结束/异常时关闭。

旧实现 `_do_execute` 每个工具调用都 `async with gateway_cls(...)` —— MCP 模式下
每次调用 = spawn 子进程 + initialize 握手 + list_tools，一轮 12 个工具 12 次握手。
"""

import pytest

from app.cache import market_cache
from app.config import Settings
from app.graph.nodes.analysts.technical import TechnicalAnalystNode
from app.models.market import NormalizedDatum, ToolResult


class FakeTool:
    def __init__(self, name):
        self.name = name


class FakeGateway:
    instances: list["FakeGateway"] = []
    entered = 0
    exited = 0

    def __init__(self, settings):
        FakeGateway.instances.append(self)

    async def __aenter__(self):
        FakeGateway.entered += 1
        self.tools = [
            FakeTool("quote_tencent_quote_get"),
            FakeTool("overview_eastmoney_overview_get"),
            FakeTool("public_sentiment_ashare_master_sentiment_get"),
        ]
        return self

    async def __aexit__(self, *exc):
        FakeGateway.exited += 1
        return None

    async def call(self, tool_name, arguments):
        return ToolResult(
            tool=tool_name,
            arguments=arguments,
            status="success",
            normalized=[NormalizedDatum(metric="m", value=1.0, tool=tool_name)],
        )


class ExplodingGateway(FakeGateway):
    async def call(self, tool_name, arguments):
        raise RuntimeError("upstream boom")


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeGateway.instances = []
    FakeGateway.entered = 0
    FakeGateway.exited = 0
    market_cache.clear()  # 全局缓存会让下一个用例直接命中，绕过 gateway 创建
    yield
    market_cache.clear()


def _state(n_tools=3):
    return {
        "question": "贵州茅台600519怎么样",
        "route": [
            {
                "analyst": "technical",
                "budget": n_tools,
                "tool_calls": [
                    {"tool_key": "quote", "arguments": {"symbol": "600519"}},
                    {"tool_key": "overview", "arguments": {}},
                    {"tool_key": "sentiment", "arguments": {}},
                ][:n_tools],
            }
        ],
    }


async def test_analyst_run_creates_single_gateway():
    node = TechnicalAnalystNode(Settings())
    node._runtime._gateway_class = lambda: FakeGateway
    out = await node(_state())

    assert len(FakeGateway.instances) == 1  # 旧实现这里是 3（每个工具一个）
    assert FakeGateway.entered == 1
    assert FakeGateway.exited == 1  # 运行结束必须关闭
    assert len(out["results"]) == 3
    assert out["findings"][0]["failed"] is False


async def test_gateway_closed_on_tool_exception():
    node = TechnicalAnalystNode(Settings())
    node._runtime._gateway_class = lambda: ExplodingGateway
    out = await node(_state())

    # 异常被 analyst 骨架吞掉（errors 降级），但客户端必须被关闭，不得泄漏子进程
    assert FakeGateway.instances, "gateway should have been created"
    assert FakeGateway.exited == 1
    assert out["findings"][0]["failed"] is True
    assert any("technical analysis failed" in e for e in out["errors"])
