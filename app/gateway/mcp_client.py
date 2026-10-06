"""MCP Client — Market Gateway 的 MCP 协议接入层。

职责：
- 管理 MCP 连接生命周期
- 暴露允许的 Tool 列表
- 统一错误处理（超时、认证、上游）
"""

import asyncio
import json
import logging
import os
import shlex
import time
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

from app.config import Settings
from app.gateway.normalizer import normalize_tool_result

logger = logging.getLogger(__name__)

#: 必须显式透传给 MCP 子进程的代理变量。
#:
#: `mcp` SDK 的 stdio transport 在 `env=None` 时用 `get_default_environment()`
#: 构造子进程环境，而它只白名单继承 APPDATA/HOMEDRIVE/HOMEPATH/LOCALAPPDATA/
#: PATH/PATHEXT/PROCESSOR_ARCHITECTURE/SYSTEMDRIVE/SYSTEMROOT/TEMP/USERNAME/
#: USERPROFILE 这 12 个变量 —— **代理变量会被剥掉**。后果：在必须走代理才能
#: 出网的机器上（如本机 Clash 127.0.0.1:7897），iiix 子进程直连
#: logto.x.iiix.dev / api.x.iiix.dev，Go dialer 先试 IPv6（本机 IPv6 不通）
#: 耗尽超时预算，握手能过但**任何真实 tool call 都报**
#: `gateway_error: context deadline exceeded`，且报错文案伪装成 OAuth 登录问题。
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
)


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

    @staticmethod
    def _child_env() -> dict[str, str]:
        """构造 MCP 子进程环境：SDK 默认白名单 + 显式透传的代理变量。

        见 `PROXY_ENV_KEYS` 的说明：`env=None` 会让 SDK 丢掉代理变量，导致
        「必须走代理才能出网」的机器上 MCP 真实调用全部超时。
        """
        env = dict(get_default_environment())
        for key in PROXY_ENV_KEYS:
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

    async def connect(self) -> None:
        args = shlex.split(self.settings.mcp_args)
        params = StdioServerParameters(
            command=self.settings.mcp_command,
            args=args,
            env=self._child_env(),
        )
        # 报错时带上实际配置：MCP 故障的排查第一步永远是「到底起的是哪个二进制、
        # 用了什么参数」（例：0.8.0 起 `mcp serve` 被删，必须 `plugin serve`）。
        launch = f"{self.settings.mcp_command} {self.settings.mcp_args}".strip()

        try:
            try:
                read_stream, write_stream = await asyncio.wait_for(
                    self.stack.enter_async_context(stdio_client(params)),
                    timeout=self.settings.research_timeout_seconds,
                )
            except TimeoutError:
                raise MCPConnectionError(
                    f"MCP server startup timed out after {self.settings.research_timeout_seconds}s (command: {launch})"
                ) from None
            except Exception as exc:
                raise MCPConnectionError(f"MCP server startup failed: {exc} (command: {launch})") from exc

            self.session = await self.stack.enter_async_context(ClientSession(read_stream, write_stream))

            # T12：mcp 2.x 的 ClientSession.__aenter__ 只启动 dispatcher、**不做握手**；
            # 服务端在未握手时（除 ping 外）会拒绝一切请求。list_tools 前必须显式
            # initialize()，并把超时 / 协议错误统一包成 MCPConnectionError。
            try:
                await asyncio.wait_for(
                    self.session.initialize(),
                    timeout=self.settings.research_timeout_seconds,
                )
            except TimeoutError:
                raise MCPConnectionError(f"MCP initialize (handshake) timed out (command: {launch})") from None
            except Exception as exc:
                raise MCPConnectionError(f"MCP initialize (handshake) failed: {exc} (command: {launch})") from exc

            try:
                listed = await asyncio.wait_for(
                    self.session.list_tools(),
                    timeout=self.settings.research_timeout_seconds,
                )
                self.tools = list(listed.tools)
                logger.info("MCP connected: %d tools available", len(self.tools))
            except TimeoutError:
                raise MCPConnectionError(f"MCP list_tools timed out (command: {launch})") from None
            except Exception as exc:
                raise MCPConnectionError(f"MCP list_tools failed: {exc} (command: {launch})") from exc
        except MCPConnectionError:
            # 任何连接阶段失败：先回收已进入的资源（含 stdio 子进程），再向上抛。
            await self.close()
            raise

    async def close(self) -> None:
        try:
            # 无条件关闭整个 exit stack（stdio 子进程 + session）。旧实现只在
            # session 非空时关闭，而 connect() 在 session 建立前失败（如启动 /
            # 握手超时）会留下 stdio 子进程不被回收。
            await self.stack.aclose()
        except Exception as exc:
            logger.warning("MCP close cleanup failed: %s", exc)
        finally:
            self.session = None
            self.tools = []
            # 重建 stack，允许同一客户端在失败后重新 connect。
            self.stack = AsyncExitStack()

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        deadline: float | None = None,
    ):
        if not self.session:
            raise MCPToolCallError("MCP client is not connected")

        last_error = None
        max_attempts = self.settings.max_retry_per_tool + 1

        for attempt in range(max_attempts):
            # T23：与 HTTP client 相同的预算语义——总预算用尽就不再重试。
            if deadline is not None and deadline - time.monotonic() <= 0:
                logger.warning("MCP call %s stopped: budget exhausted before attempt %d", tool_name, attempt + 1)
                reason = "budget exhausted: no time left for another attempt"
                if last_error:
                    reason = f"{last_error}; {reason}"
                return normalize_tool_result(tool_name, arguments, None, error=reason)
            try:
                result = await asyncio.wait_for(
                    self.session.call_tool(tool_name, arguments=arguments),
                    timeout=self.settings.research_timeout_seconds,
                )
                if getattr(result, "is_error", False):
                    # MCP 规范里工具级失败是**带内** signalling：call_tool 会返回
                    # isError=True 而不是抛异常。这个标志以前从没被读过，于是
                    # "Server Error: symbol KPEPE not supported" 这类上游错误会被
                    # 归一化成 status="success" 的证据，直接喂给 Reasoning LLM。
                    # 不重试：工具级错误通常是确定性的（symbol 错、参数非法），
                    # 重试只会白等一轮 research_timeout_seconds。
                    text = _mcp_result_to_text(result)
                    logger.warning(
                        "MCP call %s returned a tool-level error: %s",
                        tool_name,
                        text[:200],
                    )
                    return normalize_tool_result(
                        tool_name,
                        arguments,
                        None,
                        error=text or f"MCP tool {tool_name} returned an error",
                    )
                raw = _mcp_result_to_json(result)
                return normalize_tool_result(tool_name, arguments, raw)
            except TimeoutError:
                last_error = f"Timeout after {self.settings.research_timeout_seconds}s"
                logger.warning(
                    "MCP call %s attempt %d/%d timed out",
                    tool_name,
                    attempt + 1,
                    max_attempts,
                )
            except MCPConnectionError:
                # 连接级别错误不应该重试
                raise
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "MCP call %s attempt %d/%d failed: %s",
                    tool_name,
                    attempt + 1,
                    max_attempts,
                    exc,
                )

            if attempt < max_attempts - 1:
                await asyncio.sleep(0.5 * (attempt + 1))

        return normalize_tool_result(
            tool_name,
            arguments,
            None,
            error=last_error or "unknown MCP error",
        )


def _mcp_result_to_text(result: Any) -> str:
    """把 CallToolResult 的文本内容拼成一个字符串（用于错误信息）。"""
    parts = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(str(text))
    return "\n".join(parts).strip()


def _mcp_result_to_json(result: Any) -> Any:
    """将 MCP ToolResult 转换为 Python 对象。"""
    # SDK 的字段是 snake_case 的 structured_content；以前这里写的是驼峰
    # structuredContent，hasattr 永远为 False，快路径是死代码。
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured

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
