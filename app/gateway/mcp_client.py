"""MCP Client — Market Gateway 的 MCP 协议接入层。

职责：
- 管理 MCP 连接生命周期
- 暴露允许的 Tool 列表
- 统一错误处理（超时、认证、上游）
"""

import asyncio
import contextlib
import json
import logging
import os
import shlex
import subprocess
import tempfile
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import _parse_line, get_default_environment, stdio_client
from mcp.shared.message import SessionMessage

from app.config import Settings
from app.gateway.arguments import ArgumentValidationError, canonicalize_tool_arguments, summarize_arguments
from app.gateway.normalizer import normalize_tool_result
from app.gateway.tool_registry import resolve_tool_by_name

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

    def _input_schema(self, tool_name: str) -> dict[str, Any] | None:
        """Return the discovered MCP input schema for ``tool_name``.

        MCP SDK versions expose the field as either ``inputSchema`` or
        ``input_schema``.  Keep this lookup at the transport boundary so the
        planner/state always retain the canonical list form.
        """
        for tool in self.tools or []:
            name = getattr(tool, "name", None)
            if name != tool_name and isinstance(tool, dict):
                name = tool.get("name")
            if name != tool_name:
                continue
            schema = getattr(tool, "inputSchema", None)
            if schema is None:
                schema = getattr(tool, "input_schema", None)
            if schema is None and isinstance(tool, dict):
                schema = tool.get("inputSchema") or tool.get("input_schema")
            return schema if isinstance(schema, dict) else None
        return None

    def _encode_transport_arguments(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Encode canonical arguments according to the discovered MCP schema."""
        encoded = dict(arguments)
        if tool_name != "get_market_quotes" or "symbols" not in encoded:
            return encoded
        schema = self._input_schema(tool_name) or {}
        properties = schema.get("properties") if isinstance(schema, dict) else None
        symbol_schema = properties.get("symbols") if isinstance(properties, dict) else None
        schema_type = symbol_schema.get("type") if isinstance(symbol_schema, dict) else None
        if schema_type == "array":
            return encoded
        # Current Gateway contract is a string; unknown schemas use that
        # contract for backwards compatibility.  The canonical list remains
        # untouched in ToolResult/logging.
        encoded["symbols"] = ",".join(encoded["symbols"])
        return encoded

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    @staticmethod
    def _child_env(settings: Settings | None = None) -> dict[str, str]:
        """构造 MCP 子进程环境：SDK 默认白名单 + 显式透传的代理变量。

        见 `PROXY_ENV_KEYS` 的说明：`env=None` 会让 SDK 丢掉代理变量，导致
        「必须走代理才能出网」的机器上 MCP 真实调用全部超时。
        """
        env = dict(get_default_environment())
        configured = str(getattr(settings, "mcp_appdata_dir", "") or "").strip() if settings else ""
        if not configured:
            configured = str(Path.cwd() / ".tmp" / "iiix-appdata")
        appdata = Path(configured).expanduser().resolve()
        try:
            appdata.mkdir(parents=True, exist_ok=True)
        except OSError:
            # A packaged/read-only cwd must still have a narrow writable
            # fallback.  Never fall back to the user's roaming profile.
            appdata = Path(tempfile.gettempdir()) / "mosaic-iiix-appdata"
            appdata.mkdir(parents=True, exist_ok=True)
            logger.warning("Configured MCP APPDATA is not writable; using temp log root=%s", appdata)
        # iiix derives its log file from APPDATA.  Keep the override narrow to
        # the spawned process and record the effective path for diagnostics.
        env["APPDATA"] = str(appdata)
        # iiix stores plugin bundles and cross-process locks under LOCALAPPDATA
        # (APPDATA only controls its roaming log/auth root).  Keep installation,
        # authorization locks, logs, and the spawned runtime inside the same
        # project-writable directory.
        env["LOCALAPPDATA"] = str(appdata)
        logger.info("MCP child APPDATA/log root=%s", appdata / "iiix" / "logs")
        for key in PROXY_ENV_KEYS:
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

    @staticmethod
    @contextlib.asynccontextmanager
    async def _blocking_stdio_client(params: StdioServerParameters):
        """Fallback stdio bridge for Windows hosts whose asyncio pipe API is denied.

        Some managed Windows runtimes reject ``CreateNamedPipe`` from
        ``asyncio``/AnyIO (WinError 5), while ordinary ``subprocess.Popen``
        pipes remain available.  Preserve the MCP wire protocol and lifecycle
        by moving only blocking pipe reads/writes to worker threads.  This is
        used strictly after the native MCP transport reports PermissionError.
        """
        env = get_default_environment() | (params.env or {})
        proc = subprocess.Popen(
            [params.command, *params.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=params.cwd,
            env=env,
            bufsize=0,
        )
        assert proc.stdin is not None and proc.stdout is not None
        read_send, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
        write_stream, write_recv = anyio.create_memory_object_stream[SessionMessage](0)
        stop = asyncio.Event()

        async def reader() -> None:
            try:
                while not stop.is_set():
                    line = await asyncio.to_thread(proc.stdout.readline)
                    if not line:
                        break
                    try:
                        text = line.decode(params.encoding, errors=params.encoding_error_handler)
                        await read_send.send(_parse_line(text.rstrip("\r\n")))
                    except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                        break
            finally:
                await read_send.aclose()

        async def writer() -> None:
            try:
                async for message in write_recv:
                    payload = (
                        message.message.model_dump_json(by_alias=True, exclude_unset=True) + "\n"
                    ).encode(params.encoding, errors=params.encoding_error_handler)
                    await asyncio.to_thread(proc.stdin.write, payload)
                    await asyncio.to_thread(proc.stdin.flush)
            except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
                pass

        reader_task = asyncio.create_task(reader())
        writer_task = asyncio.create_task(writer())
        try:
            yield read_stream, write_stream
        finally:
            stop.set()
            await write_stream.aclose()
            await write_recv.aclose()
            with contextlib.suppress(OSError):
                proc.stdin.close()
            if proc.poll() is None:
                proc.terminate()
            await asyncio.to_thread(proc.wait)
            reader_task.cancel()
            writer_task.cancel()
            await asyncio.gather(reader_task, writer_task, return_exceptions=True)
            await read_stream.aclose()
            await read_send.aclose()

    async def connect(self) -> None:
        args = shlex.split(self.settings.mcp_args)
        params = StdioServerParameters(
            command=self.settings.mcp_command,
            args=args,
            env=self._child_env(self.settings),
        )
        # 报错时带上实际配置：MCP 故障的排查第一步永远是「到底起的是哪个二进制、
        # 用了什么参数」（例：0.8.0 起 `mcp serve` 被删，必须 `plugin serve`）。
        launch = f"{self.settings.mcp_command} {self.settings.mcp_args}".strip()

        try:
            try:
                try:
                    read_stream, write_stream = await asyncio.wait_for(
                        self.stack.enter_async_context(stdio_client(params)),
                        timeout=self.settings.research_timeout_seconds,
                    )
                except PermissionError as exc:
                    # Managed Windows hosts may deny asyncio's named-pipe
                    # creation even though ordinary Popen pipes are allowed.
                    # Retry the exact same MCP command through the blocking
                    # bridge; protocol, auth, and tool validation stay real.
                    if os.name != "nt":
                        raise
                    logger.warning("Native MCP stdio pipe denied; using blocking bridge: %s", exc)
                    read_stream, write_stream = await asyncio.wait_for(
                        self.stack.enter_async_context(self._blocking_stdio_client(params)),
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
        except (MCPConnectionError, asyncio.CancelledError):
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
            logger.warning("MCP close cleanup failed error_type=%s", type(exc).__name__)
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

        try:
            arguments = canonicalize_tool_arguments(tool_name, arguments)
        except ArgumentValidationError as exc:
            try:
                logical_key = resolve_tool_by_name(tool_name).key
            except KeyError:
                logical_key = None
            return normalize_tool_result(
                tool_name,
                dict(arguments or {}),
                None,
                error=f"Invalid arguments: {exc}",
                tool_key=logical_key,
            )

        last_error = None
        max_attempts = self.settings.max_retry_per_tool + 1

        for attempt in range(max_attempts):
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                logger.warning(
                    "MCP call %s stopped: attempt=%d remaining_budget_ms=0 retry_skipped_reason=deadline_exhausted",
                    tool_name,
                    attempt + 1,
                )
                reason = f"budget exhausted: no time left for attempt {attempt + 1} of {tool_name}"
                if last_error:
                    reason = f"{last_error}; {reason}"
                return normalize_tool_result(tool_name, arguments, None, error=reason)
            try:
                attempt_timeout = self.settings.research_timeout_seconds
                if remaining is not None:
                    attempt_timeout = min(attempt_timeout, max(remaining, 0.0))
                transport_arguments = self._encode_transport_arguments(tool_name, arguments)
                logger.info(
                    "MCP call %s attempt=%d attempt_timeout_ms=%.0f remaining_budget_ms=%s "
                    "canonical_argument_summary=%s transport_argument_summary=%s",
                    tool_name,
                    attempt + 1,
                    attempt_timeout * 1000,
                    "none" if remaining is None else f"{remaining * 1000:.0f}",
                    summarize_arguments(arguments),
                    summarize_arguments(transport_arguments),
                )
                result = await asyncio.wait_for(
                    self.session.call_tool(tool_name, arguments=transport_arguments),
                    timeout=attempt_timeout,
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
                        "MCP call %s returned a tool-level error payload_len=%d",
                        tool_name,
                        len(text),
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
                last_error = (
                    f"Timeout on {tool_name} attempt {attempt + 1} after "
                    f"{attempt_timeout:.3f}s (remaining budget was "
                    f"{'unknown' if remaining is None else f'{remaining:.3f}'}s)"
                )
                logger.warning(
                    "MCP call %s attempt %d/%d timed out attempt_timeout_ms=%.0f remaining_budget_ms=%s",
                    tool_name,
                    attempt + 1,
                    max_attempts,
                    attempt_timeout * 1000,
                    "none" if remaining is None else f"{max(remaining, 0) * 1000:.0f}",
                )
            except MCPConnectionError:
                # 连接级别错误不应该重试
                raise
            except Exception as exc:
                last_error = f"{type(exc).__name__}"
                logger.warning(
                    "MCP call %s attempt %d/%d failed error_type=%s",
                    tool_name,
                    attempt + 1,
                    max_attempts,
                    type(exc).__name__,
                )

            if attempt < max_attempts - 1:
                backoff = 0.5 * (attempt + 1)
                remaining_after = None if deadline is None else deadline - time.monotonic()
                if remaining_after is not None:
                    if remaining_after <= 0:
                        logger.warning(
                            "MCP call %s retry skipped retry_skipped_reason=deadline_exhausted remaining_budget_ms=0",
                            tool_name,
                        )
                        return normalize_tool_result(
                            tool_name,
                            arguments,
                            None,
                            error=f"{last_error or 'MCP call failed'}; budget exhausted before retry",
                        )
                    if backoff > remaining_after:
                        logger.info(
                            "MCP call %s retry skipped retry_skipped_reason=backoff_exceeds_remaining "
                            "remaining_budget_ms=%.0f",
                            tool_name,
                            remaining_after * 1000,
                        )
                        return normalize_tool_result(
                            tool_name,
                            arguments,
                            None,
                            error=f"{last_error or 'MCP call failed'}; budget exhausted before retry",
                        )
                await asyncio.sleep(backoff)

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
