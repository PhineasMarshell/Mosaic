"""HTTP Client — Market Gateway 的 HTTP API 接入层。

职责：
- 通过 REST HTTP API 调用 Market Gateway Tools
- 只允许调用 Registry 白名单中的工具
- 区分不同 HTTP 错误的处理策略
"""

import logging
from typing import Any

import httpx

from app.config import Settings
from app.gateway.arguments import (
    ArgumentValidationError,
    canonicalize_tool_arguments,
    gateway_transport_arguments,
    quote_transport_symbols,
)
from app.gateway.normalizer import normalize_tool_result

logger = logging.getLogger(__name__)


class _RedactRequestURL(logging.Filter):
    """HTTPX INFO logs include query strings even when our logs are redacted."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg == 'HTTP Request: %s %s "%s %d %s"' and isinstance(record.args, tuple):
            args = list(record.args)
            args[1] = "<redacted-url>"
            record.args = tuple(args)
        return True


logging.getLogger("httpx").addFilter(_RedactRequestURL())


class HTTPGatewayError(RuntimeError):
    """HTTP 网关调用失败基类。"""


class HTTPAuthError(HTTPGatewayError):
    """401/403 认证/权限错误 — 不应重试。"""


class HTTPRateLimitError(HTTPGatewayError):
    """429 限流 — 应等待后重试。"""


class HTTPUpstreamError(HTTPGatewayError):
    """502/503/504 上游错误 — 可短暂重试。"""


def _classify_http_error(status_code: int) -> type[HTTPGatewayError]:
    """根据 HTTP 状态码分类错误类型。"""
    if status_code in (401, 403):
        return HTTPAuthError
    if status_code == 429:
        return HTTPRateLimitError
    if status_code >= 500:
        return HTTPUpstreamError
    return HTTPGatewayError


class MarketGatewayHttpClient:
    """Market Gateway HTTP client.

    这个 Client 故意只允许调用 Registry 中的工具，
    不允许 Planner 直接拼接任意 URL。
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        if not self.settings.market_gateway_api_key:
            raise RuntimeError("MARKET_GATEWAY_API_KEY is required when MARKET_GATEWAY_MODE=http")

        self.client = httpx.AsyncClient(
            base_url=self.settings.market_gateway_http_url.rstrip("/"),
            headers={
                "X-API-Key": self.settings.market_gateway_api_key,
                "Accept": "application/json",
            },
            timeout=self.settings.research_timeout_seconds,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        deadline: float | None = None,
    ):
        """执行一次工具调用。

        Args:
            tool_name: registry 中的 operationId
            arguments: 调用参数
            deadline: T23 — 本工具允许用到的最后时刻（``time.monotonic()`` 秒）。
                由 analyst 按"剩余总预算 ÷ 剩余工具数"均分下发；None 表示无预算约束
                （行为与旧实现完全一致）。重试前检查剩余时间：不够就不再重试，
                退避时间也不得超过剩余预算；超预算以 error ToolResult 返回，不抛异常。
        """
        if self.client is None:
            raise HTTPGatewayError("HTTP client is not connected")

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

        from app.gateway.policy import observed_call, response_diagnostics

        meta = resolve_tool_by_name(tool_name)

        async def request(timeout):
            encoded = gateway_transport_arguments(tool_name, arguments)
            if tool_name == "get_market_quotes":
                encoded["symbols"] = ",".join(quote_transport_symbols(arguments["symbols"]))
            response = await self.client.request(
                meta.http_method, meta.http_path,
                params=encoded if meta.http_method == "GET" else None,
                json=encoded if meta.http_method != "GET" else None,
                timeout=timeout,
            )
            diagnostics = response_diagnostics(response.text, response.status_code)
            if response.status_code >= 400:
                from app.gateway.normalizer import _reason_codes_for_error

                codes = _reason_codes_for_error(f"HTTP {response.status_code} {response.text}")
                if response.status_code >= 500:
                    codes.append("http_5xx")
                if response.status_code >= 500 and "gateway_upstream_error" not in codes:
                    codes.append("gateway_upstream_error")
                normalized = normalize_tool_result(tool_name, arguments, None,
                                                   error=f"HTTP Gateway error ({response.status_code})",
                                                   reason_codes=codes)
            elif not response.content:
                normalized = normalize_tool_result(tool_name, arguments, None)
            else:
                try:
                    raw = response.json()
                except ValueError:
                    from app.gateway.normalizer import _reason_codes_for_error

                    codes = _reason_codes_for_error(response.text)
                    normalized = normalize_tool_result(tool_name, arguments, None,
                                                       error="Invalid Gateway JSON response", reason_codes=codes)
                else:
                    normalized = normalize_tool_result(tool_name, arguments, raw)
            return normalized, diagnostics

        return await observed_call(self.settings, tool_name, arguments, deadline, request)


def resolve_tool_by_name(tool_name: str):
    """通过 operationId 找到 Registry 元数据。"""
    from app.gateway.tool_registry import BY_NAME

    if tool_name not in BY_NAME:
        raise KeyError(f"Tool is not allowed by registry: {tool_name}")
    return BY_NAME[tool_name]
