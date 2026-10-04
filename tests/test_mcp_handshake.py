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
