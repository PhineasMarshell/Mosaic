"""HTTP Client — Market Gateway 的 HTTP API 接入层。

职责：
- 通过 REST HTTP API 调用 Market Gateway Tools
- 只允许调用 Registry 白名单中的工具
- 区分不同 HTTP 错误的处理策略
"""

import asyncio
import logging
from typing import Any

import httpx

from app.config import Settings
from app.gateway.normalizer import normalize_tool_result
from app.gateway.tool_registry import resolve_tool

logger = logging.getLogger(__name__)


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
            raise RuntimeError(
                "MARKET_GATEWAY_API_KEY is required when MARKET_GATEWAY_MODE=http"
            )

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
    ):
        if self.client is None:
            raise HTTPGatewayError("HTTP client is not connected")

        meta = resolve_tool_by_name(tool_name)
        last_error = None
        max_attempts = self.settings.max_retry_per_tool + 1

        for attempt in range(max_attempts):
            try:
                response = await self.client.request(
                    meta.http_method,
                    meta.http_path,
                    params=arguments if meta.http_method == "GET" else None,
                    json=arguments if meta.http_method != "GET" else None,
                )

                # --------------------------------------------------
                # 错误分类（不重试的情况）
                # --------------------------------------------------
                error_cls = _classify_http_error(response.status_code)

                if response.status_code == 401:
                    raise HTTPAuthError(
                        f"Authentication failed (401)"
                    )

                if response.status_code == 403:
                    raise HTTPAuthError(
                        f"Permission denied (403)"
                    )

                if response.status_code == 413:
                    raise HTTPGatewayError(
                        f"Request too large or malformed (413)"
                    )

                if response.status_code == 422:
                    # 记录错误详情供后续报告使用
                    error_msg = f"Invalid parameters (422): {response.text[:200]}"
                    return normalize_tool_result(
                        tool_name,
                        arguments,
                        None,
                        error=error_msg,
                    )

                if response.status_code == 429:
                    # 限流：短暂等待后重试
                    backoff = min(2 ** attempt, 10)
                    logger.warning(
                        "Rate limited (%d), backing off %ds",
                        response.status_code, backoff,
                    )
                    last_error = f"Rate limited (429)"
                    await asyncio.sleep(backoff)
                    continue

                response.raise_for_status()
                raw = response.json()
                return normalize_tool_result(tool_name, arguments, raw)

            except (httpx.HTTPStatusError, httpx.TimeoutException) as exc:
                last_error = str(exc)
                logger.warning(
                    "HTTP call %s attempt %d/%d failed: %s",
                    tool_name, attempt + 1, max_attempts, exc,
                )
            except (HTTPAuthError, HTTPGatewayError):
                # 这些是致命错误，不再重试
                return normalize_tool_result(
                    tool_name,
                    arguments,
                    None,
                    error=last_error,
                )
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "HTTP call %s attempt %d/%d failed: %s",
                    tool_name, attempt + 1, max_attempts, exc,
                )

            if attempt < max_attempts - 1:
                await asyncio.sleep(0.5 * (attempt + 1))

        return normalize_tool_result(
            tool_name,
            arguments,
            None,
            error=last_error or "unknown HTTP gateway error",
        )


def resolve_tool_by_name(tool_name: str):
    """通过 operationId 找到 Registry 元数据。"""
    from app.gateway.tool_registry import BY_NAME

    if tool_name not in BY_NAME:
        raise KeyError(f"Tool is not allowed by registry: {tool_name}")
    return BY_NAME[tool_name]
