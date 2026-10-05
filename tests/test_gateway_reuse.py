"""T18 回归：analyst 一次运行只创建一个 Gateway 客户端，结束/异常时关闭。

旧实现 `_do_execute` 每个工具调用都 `async with gateway_cls(...)` —— MCP 模式下
每次调用 = spawn 子进程 + initialize 握手 + list_tools，一轮 12 个工具 12 次握手。

T18b：gateway_session 按需连接（只用内部工具的 analyst 不建连）。
T18c：会话状态在 ContextVar（每 asyncio task 一份），并发请求不串台。
"""

import asyncio

import pytest

from app.cache import market_cache
from app.config import Settings
from app.graph.nodes.analysts.technical import TechnicalAnalystNode
from app.graph.tool_runtime import ToolRuntime
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

    async def call(self, tool_name, arguments, deadline=None):
        return ToolResult(
            tool=tool_name,
            arguments=arguments,
            status="success",
            normalized=[NormalizedDatum(metric="m", value=1.0, tool=tool_name)],
        )


class ExplodingGateway(FakeGateway):
    async def call(self, tool_name, arguments, deadline=None):
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


# ------------------------------------------------------------------ #
# T18b 回归：gateway_session 按需连接                                   #
# ------------------------------------------------------------------ #


def _news_only_state():
    return {
        "question": "最近有什么新闻",
        "route": [
            {
                "analyst": "technical",
                "budget": 1,
                "tool_calls": [
                    {"tool_key": "news_search", "arguments": {"query": "q"}},
                ],
            }
        ],
    }


def _mixed_state():
    return {
        "question": "贵州茅台600519怎么样",
        "route": [
            {
                "analyst": "technical",
                "budget": 2,
                "tool_calls": [
                    {"tool_key": "news_search", "arguments": {"query": "q"}},
                    {"tool_key": "quote", "arguments": {"symbol": "600519"}},
                ],
            }
        ],
    }


class _BoomOnEnterGateway:
    """__aenter__ 立即抛错 —— 防止有人把建连挪回会话进入点（T18b 反向防线）。"""

    constructed = 0

    def __init__(self, settings):
        _BoomOnEnterGateway.constructed += 1

    async def __aenter__(self):
        raise RuntimeError("eager connection must not happen")

    async def __aexit__(self, *exc):
        return None

    async def call(self, name, args, deadline=None):  # pragma: no cover - 不应被走到
        raise AssertionError("gateway should never be called")


async def test_internal_only_route_never_touches_gateway(monkeypatch):
    """只用内部工具（news_search）的 analyst 不得创建/连接 Gateway。"""
    from app.graph.nodes.analysts.technical import TechnicalAnalystNode

    async def fake_search_news(query, *, max_results=5, time_limit="d"):
        return {"news": [{"date": "d", "title": "t", "body": "b", "url": "u"}], "meta": {"status": "ok"}}

    monkeypatch.setattr("app.graph.tool_runtime._search_news", fake_search_news)

    node = TechnicalAnalystNode(Settings())
    node._runtime._gateway_class = lambda: FakeGateway
    out = await node(_news_only_state())

    # 旧实现（T18 的急切连接）：这里 instances == ["FakeGateway"]，必红
    assert FakeGateway.instances == []
    assert out["findings"][0]["failed"] is False
    assert "news_search" in out["findings"][0]["tools_used"]
    assert out["errors"] == []


async def test_mixed_route_lazy_connects_exactly_once(monkeypatch):
    """混合 route（内部 + gateway 工具）：按需建连后仍复用同一客户端。"""
    from app.graph.nodes.analysts.technical import TechnicalAnalystNode

    async def fake_search_news(query, *, max_results=5, time_limit="d"):
        return {"news": [{"date": "d", "title": "t", "body": "b", "url": "u"}], "meta": {"status": "ok"}}

    monkeypatch.setattr("app.graph.tool_runtime._search_news", fake_search_news)

    node = TechnicalAnalystNode(Settings())
    node._runtime._gateway_class = lambda: FakeGateway
    out = await node(_mixed_state())

    assert len(FakeGateway.instances) == 1
    assert FakeGateway.entered == 1
    assert FakeGateway.exited == 1
    assert len(out["results"]) == 2
    assert out["findings"][0]["failed"] is False
    assert out["errors"] == []


async def test_exploding_gateway_not_constructed_for_internal_route(monkeypatch):
    """网关必炸 + route 只用内部工具 → 构造次数 0，analyst 照常成功。"""
    from app.graph.nodes.analysts.technical import TechnicalAnalystNode

    async def fake_search_news(query, *, max_results=5, time_limit="d"):
        return {"news": [{"date": "d", "title": "t", "body": "b", "url": "u"}], "meta": {"status": "ok"}}

    monkeypatch.setattr("app.graph.tool_runtime._search_news", fake_search_news)

    node = TechnicalAnalystNode(Settings())
    node._runtime._gateway_class = lambda: _BoomOnEnterGateway
    out = await node(_news_only_state())

    assert _BoomOnEnterGateway.constructed == 0
    assert out["findings"][0]["failed"] is False
    assert out["errors"] == []


# ------------------------------------------------------------------ #
# T18c 回归：会话状态每请求一份（ContextVar）                           #
# ------------------------------------------------------------------ #


class _SlowGateway:
    """可被 asyncio.Event 精确控速的 Gateway stub（模拟真实客户端的关闭语义）。"""

    instances: list["_SlowGateway"] = []
    entered = 0
    exited = 0
    release: dict[str, asyncio.Event] = {}  # symbol -> 放行事件

    def __init__(self, settings):
        _SlowGateway.instances.append(self)
        self.is_closed = False

    async def __aenter__(self):
        _SlowGateway.entered += 1

        class _T:
            name = "quote_tencent_quote_get"

        self.tools = [_T()]
        return self

    async def __aexit__(self, *exc):
        _SlowGateway.exited += 1
        self.is_closed = True
        return None

    async def call(self, tool_name, arguments, deadline=None):
        event = _SlowGateway.release.get(arguments.get("symbol"))
        if event is not None:
            await event.wait()
        # 强制让出事件循环：保证 gather 的两个任务真正交错，
        # 否则 A 可能在 B 启动前就整个跑完，测不到并发串台。
        await asyncio.sleep(0)
        assert not self.is_closed, f"gateway closed while {tool_name} in flight"
        return ToolResult(
            tool=tool_name,
            arguments=arguments,
            status="success",
            normalized=[NormalizedDatum(metric="m", value=1.0, tool=tool_name)],
        )


def _reset_slow(release: dict[str, asyncio.Event] | None = None):
    _SlowGateway.instances = []
    _SlowGateway.entered = 0
    _SlowGateway.exited = 0
    _SlowGateway.release = release or {}


def _symbol_state(symbol):
    return {
        "question": f"股票{symbol}怎么样",
        "route": [
            {
                "analyst": "technical",
                "budget": 1,
                "tool_calls": [{"tool_key": "quote", "arguments": {"symbol": symbol}}],
            }
        ],
    }


async def test_concurrent_requests_get_isolated_sessions():
    """并发两请求共用同一节点实例（模拟单例图）→ 每请求一个客户端，互不串台。"""
    _reset_slow()
    node = TechnicalAnalystNode(Settings())
    node._runtime._gateway_class = lambda: _SlowGateway

    out_a, out_b = await asyncio.gather(
        node(_symbol_state("600519")),
        node(_symbol_state("000001")),
    )

    # 旧实现（会话状态在 self.* 上）：B 被当成嵌套会话复用 A 的客户端 → 这里是 1
    assert len(_SlowGateway.instances) == 2
    assert _SlowGateway.exited == 2
    assert out_a["errors"] == []
    assert out_b["errors"] == []
    assert out_a["findings"][0]["failed"] is False
    assert out_b["findings"][0]["failed"] is False
    assert len(out_a["results"]) == 1
    assert len(out_b["results"]) == 1


async def test_session_close_does_not_kill_concurrent_inflight():
    """交错验证：A 结束并关闭自己的客户端后，B 的在途调用必须仍然成功。"""
    _reset_slow(release={"600519": asyncio.Event(), "000001": asyncio.Event()})
    node = TechnicalAnalystNode(Settings())
    node._runtime._gateway_class = lambda: _SlowGateway

    task_a = asyncio.create_task(node(_symbol_state("600519")))
    await asyncio.sleep(0.05)  # A 进入会话并在途等待 release
    task_b = asyncio.create_task(node(_symbol_state("000001")))
    await asyncio.sleep(0.05)  # B 进入自己的会话并在途等待 release

    _SlowGateway.release["600519"].set()  # A 完成 → A 的 finally 关闭 A 自己的客户端
    await asyncio.sleep(0.05)
    _SlowGateway.release["000001"].set()  # B 恢复：自己的客户端，不受 A 关闭影响
    out_a, out_b = await asyncio.gather(task_a, task_b)

    assert len(_SlowGateway.instances) == 2
    # 旧实现：B 在 A 已关闭的共享客户端上调用 → 'gateway closed while ... in flight' → failed=True
    assert out_b["errors"] == []
    assert out_b["findings"][0]["failed"] is False
    assert out_a["errors"] == []


async def test_two_serial_sessions_in_same_task_each_connect_and_close():
    """同一 task 内串行开两次会话：各自建连、各自关闭，状态正确复位。"""
    _reset_slow()
    rt = ToolRuntime(Settings())
    rt._gateway_class = lambda: _SlowGateway

    async with rt.gateway_session():
        await rt.execute("quote_tencent_quote_get", {"symbol": "600519"}, set())
    async with rt.gateway_session():
        await rt.execute("quote_tencent_quote_get", {"symbol": "000001"}, set())

    assert len(_SlowGateway.instances) == 2
    assert _SlowGateway.entered == 2
    assert _SlowGateway.exited == 2
