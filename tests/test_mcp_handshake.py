"""T12 回归：MCP connect() 必须先握手（initialize）再 list_tools，失败要清理子进程。

使用 mcp SDK 的真实 lowlevel Server + 内存传输（不 spawn 子进程），
monkeypatch 仅替换 ``stdio_client`` 传输层；``connect()`` / ``ClientSession`` /
握手 / list_tools 全部走真实代码路径（不沿用直接注入 FakeSession 的旧模式）。
"""

import asyncio
from contextlib import asynccontextmanager

import pytest
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import Tool

from app.config import Settings
from app.gateway import mcp_client as mcp_client_mod
from app.gateway.mcp_client import MarketGatewayClient, MCPConnectionError


def _good_server() -> Server:
    async def list_tools_handler(ctx, params):
        from mcp.types import ListToolsResult

        return ListToolsResult(tools=[Tool(name="ping_tool", description="d", inputSchema={"type": "object"})])

    return Server(name="test-server", on_list_tools=list_tools_handler)


def _bad_server() -> Server:
    async def list_tools_handler(ctx, params):
        raise RuntimeError("boom")

    return Server(name="bad-server", on_list_tools=list_tools_handler)


def _patch_stdio(monkeypatch, server: Server, closed: dict | None = None) -> None:
    def fake_stdio_client(params):
        @asynccontextmanager
        async def cm():
            async with create_client_server_memory_streams() as (client_streams, server_streams):
                init_options = server.create_initialization_options()
                server_task = asyncio.create_task(server.run(server_streams[0], server_streams[1], init_options))
                try:
                    yield client_streams
                finally:
                    server_task.cancel()
                    await asyncio.gather(server_task, return_exceptions=True)
                    if closed is not None:
                        closed["value"] = True

        return cm()

    monkeypatch.setattr(mcp_client_mod, "stdio_client", fake_stdio_client)


async def test_connect_handshakes_and_lists_tools(monkeypatch):
    _patch_stdio(monkeypatch, _good_server())
    client = MarketGatewayClient(Settings())

    await client.connect()

    assert len(client.tools) == 1
    assert client.tools[0].name == "ping_tool"
    await client.close()


async def test_connect_failure_cleans_up_stdio(monkeypatch):
    closed = {"value": False}
    _patch_stdio(monkeypatch, _bad_server(), closed=closed)
    client = MarketGatewayClient(Settings())

    with pytest.raises(MCPConnectionError):
        await client.connect()

    # 失败时必须退出 stdio 上下文（真实场景=回收子进程）。
    assert closed["value"] is True


def _capture_stdio(monkeypatch, captured: dict, server: Server | None = None) -> None:
    """替换 stdio 传输层，记录 ``StdioServerParameters``，并接上真实 server 完成握手。"""

    def fake_stdio_client(params):
        captured["params"] = params

        @asynccontextmanager
        async def cm():
            async with create_client_server_memory_streams() as (client_streams, server_streams):
                if server is None:
                    yield client_streams
                    return
                init_options = server.create_initialization_options()
                server_task = asyncio.create_task(server.run(server_streams[0], server_streams[1], init_options))
                try:
                    yield client_streams
                finally:
                    server_task.cancel()
                    await asyncio.gather(server_task, return_exceptions=True)

        return cm()

    monkeypatch.setattr(mcp_client_mod, "stdio_client", fake_stdio_client)


async def test_connect_passes_proxy_env_to_child(monkeypatch):
    """回归：代理变量必须显式透传给 MCP 子进程。

    mcp SDK 的 ``get_default_environment()`` 只白名单继承 12 个变量，不含代理。
    在「必须走代理才能出网」的机器上，丢掉代理会让 iiix 直连上游超时，表现为
    ``gateway_error: context deadline exceeded``（还会伪装成 OAuth 登录失效）。
    """
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.delenv("ALL_PROXY", raising=False)

    captured: dict = {}
    _capture_stdio(monkeypatch, captured, server=_good_server())
    client = MarketGatewayClient(Settings())
    await client.connect()

    env = captured["params"].env
    assert env is not None, "env 必须显式构造，不能是 None（否则 SDK 会剥掉代理变量）"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert env["HTTP_PROXY"] == "http://127.0.0.1:7897"
    assert env["NO_PROXY"] == "localhost,127.0.0.1"
    assert "ALL_PROXY" not in env
    # 原有白名单变量不能丢（PATH 丢了子进程根本起不来）。
    from mcp.client.stdio import get_default_environment

    assert env["PATH"] == get_default_environment()["PATH"]
    await client.close()


async def test_connect_child_env_has_no_proxy_when_unset(monkeypatch):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(key, raising=False)

    captured: dict = {}
    _capture_stdio(monkeypatch, captured, server=_good_server())
    client = MarketGatewayClient(Settings())
    await client.connect()

    env = captured["params"].env
    assert env is not None
    assert not any(k.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"} for k in env)
    await client.close()


async def test_connection_errors_report_launch_command(monkeypatch):
    """报错必须带实际 command/args —— 排查 MCP 故障的第一步。"""

    def fake_stdio_client(params):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(mcp_client_mod, "stdio_client", fake_stdio_client)
    client = MarketGatewayClient(Settings())

    with pytest.raises(MCPConnectionError) as excinfo:
        await client.connect()

    message = str(excinfo.value)
    assert client.settings.mcp_command in message
    assert client.settings.mcp_args in message
