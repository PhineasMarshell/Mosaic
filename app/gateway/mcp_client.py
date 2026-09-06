"""MCP Client — Market Gateway 的 MCP 协议接入层。

职责：
- 管理 MCP 连接生命周期
- 暴露允许的 Tool 列表
- 统一错误处理（超时、认证、上游）
"""

import asyncio
import json
import logging
import shlex
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.config import Settings
from app.gateway.normalizer import normalize_tool_result

logger = logging.getLogger(__name__)


class MCPConnectionError(RuntimeError):
    """MCP 连接/初始化失败。"""


class MCPToolCallError(RuntimeError):
    """单次工具调用失败。"""


class MarketGatewayClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.stack = AsyncExitStack()
        self.session: ClientSession | None = None
        self.tools: list[Any] = []

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def connect(self) -> None:
        args = shlex.split(self.settings.mcp_args)
        params = StdioServerParameters(
            command=self.settings.mcp_command,
            args=args,
            env=None,
        )

        try:
            read_stream, write_stream = await asyncio.wait_for(
                self.stack.enter_async_context(stdio_client(params)),
                timeout=self.settings.research_timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise MCPConnectionError(
                f"MCP server startup timed out after "
                f"{self.settings.research_timeout_seconds}s"
            ) from None

        self.session = await self.stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )

        try:
            listed = await asyncio.wait_for(
                self.session.list_tools(),
                timeout=self.settings.research_timeout_seconds,
            )
            self.tools = list(listed.tools)
            logger.info("MCP connected: %d tools available", len(self.tools))
        except asyncio.TimeoutError:
            raise MCPConnectionError(
                "MCP list_tools timed out"
            ) from None

    async def close(self) -> None:
        try:
            if self.session is not None:
                await self.stack.aclose()
        except Exception as exc:
            logger.debug("MCP close cleanup failed: %s", exc)
        finally:
            self.session = None

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ):
        if not self.session:
            raise MCPToolCallError("MCP client is not connected")

        last_error = None
        max_attempts = self.settings.max_retry_per_tool + 1

        for attempt in range(max_attempts):
            try:
                result = await asyncio.wait_for(
                    self.session.call_tool(tool_name, arguments=arguments),
                    timeout=self.settings.research_timeout_seconds,
                )
                raw = _mcp_result_to_json(result)
                return normalize_tool_result(tool_name, arguments, raw)
            except asyncio.TimeoutError:
                last_error = f"Timeout after {self.settings.research_timeout_seconds}s"
                logger.warning(
                    "MCP call %s attempt %d/%d timed out",
                    tool_name, attempt + 1, max_attempts,
                )
            except MCPConnectionError:
                # 连接级别错误不应该重试
                raise
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "MCP call %s attempt %d/%d failed: %s",
                    tool_name, attempt + 1, max_attempts, exc,
                )

            if attempt < max_attempts - 1:
                await asyncio.sleep(0.5 * (attempt + 1))

        return normalize_tool_result(
            tool_name,
            arguments,
            None,
            error=last_error or "unknown MCP error",
        )


def _mcp_result_to_json(result: Any) -> Any:
    """将 MCP ToolResult 转换为 Python 对象。"""
    if hasattr(result, "structuredContent") and result.structuredContent is not None:
        return result.structuredContent

    contents = getattr(result, "content", None) or []
    parsed = []
    for item in contents:
        text = getattr(item, "text", None)
        if text is not None:
            try:
                parsed.append(json.loads(text))
            except json.JSONDecodeError:
                parsed.append(text)
        else:
            parsed.append(str(item))

    if len(parsed) == 1:
        return parsed[0]
    return parsed
