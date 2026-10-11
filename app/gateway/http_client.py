"""HTTP Client — Market Gateway 的 HTTP API 接入层。

职责：
- 通过 REST HTTP API 调用 Market Gateway Tools
- 只允许调用 Registry 白名单中的工具
- 区分不同 HTTP 错误的处理策略
"""

import asyncio
import logging
import time
from typing import Any

import httpx

from app.config import Settings
from app.gateway.arguments import ArgumentValidationError, canonicalize_tool_arguments, summarize_arguments
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

        meta = resolve_tool_by_name(tool_name)
        last_error = None
        max_attempts = self.settings.max_retry_per_tool + 1

        def _remaining() -> float | None:
            return None if deadline is None else deadline - time.monotonic()

        def _budget_exhausted():
            reason = "budget exhausted: no time left for another attempt"
            if last_error:
                reason = f"{last_error}; {reason}"
            logger.warning("HTTP call %s stopped: %s", tool_name, reason)
            # 规则 6：超预算必须以 error ToolResult 收尾，不能抛出去炸整条链
            return normalize_tool_result(tool_name, arguments, None, error=reason)

        for attempt in range(max_attempts):
            remaining = _remaining()
            if remaining is not None and remaining <= 0:
                return _budget_exhausted()

            try:
                attempt_timeout = self.settings.research_timeout_seconds
                if remaining is not None:
                    attempt_timeout = min(attempt_timeout, max(remaining, 0.0))
                request_arguments = dict(arguments)
                if tool_name == "get_market_quotes":
                    # allowed_openapi.json models the HTTP query field as a
                    # comma separated string; keep list[str] canonical inside
                    # Mosaic and encode only at this transport boundary.
                    request_arguments["symbols"] = ",".join(arguments["symbols"])
                logger.info(
                    "HTTP call %s attempt=%d attempt_timeout_ms=%.0f remaining_budget_ms=%s "
                    "canonical_argument_summary=%s transport_argument_summary=%s",
                    tool_name,
                    attempt + 1,
                    attempt_timeout * 1000,
                    "none" if remaining is None else f"{remaining * 1000:.0f}",
                    summarize_arguments(arguments),
                    summarize_arguments(request_arguments),
                )
                # HTTPX's timeout is phase based; a server that continually
                # sends small chunks can otherwise keep resetting the read
                # timer forever.  The outer wait_for enforces the wall-clock
                # deadline for the complete response.
                response = await asyncio.wait_for(
                    self.client.request(
                        meta.http_method,
                        meta.http_path,
                        params=request_arguments if meta.http_method == "GET" else None,
                        json=request_arguments if meta.http_method != "GET" else None,
                        timeout=attempt_timeout,
                    ),
                    timeout=attempt_timeout,
                )

                # --------------------------------------------------
                # 错误分类（不重试的情况）
                # --------------------------------------------------
                error_cls = _classify_http_error(response.status_code)

                if response.status_code in (401, 403, 413):
                    # 以前这三个分支 raise 之后由下面的 except 兜住，而 except 用的
                    # 是 last_error —— 首次尝试时它还是 None。于是 error=None 传进
                    # normalize_tool_result，被归一化成 status="success"、
                    # value="None" 的"证据"：API key 过期时，每个工具都"成功"返回
                    # 一条 None，has_evidence=True，LLM 就着一堆 None 写报告。
                    reason = {
                        401: "Authentication failed",
                        403: "Permission denied",
                        413: "Request too large or malformed",
                    }[response.status_code]
                    raise error_cls(f"{reason} ({response.status_code})")

                if response.status_code == 422:
                    # 记录错误详情供后续报告使用
                    # Keep upstream bodies out of logs and downstream traces;
                    # some gateways echo the original query in their detail.
                    error_msg = "Invalid parameters (422)"
                    return normalize_tool_result(
                        tool_name,
                        arguments,
                        None,
                        error=error_msg,
                    )

                if response.status_code == 429:
                    # 限流：短暂等待后重试。T23：退避不得超过剩余预算。
                    backoff = min(2**attempt, 10)
                    remaining = _remaining()
                    if remaining is not None:
                        if remaining <= 0:
                            last_error = "Rate limited (429)"
                            return _budget_exhausted()
                        if backoff > remaining:
                            logger.info(
                                "HTTP call %s retry skipped retry_skipped_reason=backoff_exceeds_remaining "
                                "remaining_budget_ms=%.0f",
                                tool_name,
                                remaining * 1000,
                            )
                            return _budget_exhausted()
                        backoff = min(backoff, remaining)
                    logger.warning(
                        "Rate limited (%d), backing off %ds",
                        response.status_code,
                        backoff,
                    )
                    last_error = "Rate limited (429)"
                    await asyncio.sleep(backoff)
                    continue

                response.raise_for_status()
                raw = response.json()
                return normalize_tool_result(tool_name, arguments, raw)

            except TimeoutError:
                last_error = (
                    f"Timeout on {tool_name} attempt {attempt + 1} after {attempt_timeout:.3f}s "
                    f"(remaining budget was {'unknown' if remaining is None else f'{remaining:.3f}'}s)"
                )
                logger.warning(
                    "HTTP call %s attempt %d/%d timed out attempt_timeout_ms=%.0f remaining_budget_ms=%s",
                    tool_name,
                    attempt + 1,
                    max_attempts,
                    attempt_timeout * 1000,
                    "none" if remaining is None else f"{max(remaining, 0) * 1000:.0f}",
                )
            except (httpx.HTTPStatusError, httpx.TimeoutException) as exc:
                if isinstance(exc, httpx.TimeoutException):
                    last_error = (
                        f"Timeout on {tool_name} attempt {attempt + 1} after {attempt_timeout:.3f}s "
                        f"(remaining budget was {'unknown' if remaining is None else f'{remaining:.3f}'}s)"
                    )
                else:
                    last_error = f"{type(exc).__name__}"
                logger.warning(
                    "HTTP call %s attempt %d/%d failed error_type=%s",
                    tool_name,
                    attempt + 1,
                    max_attempts,
                    type(exc).__name__,
                )
            except HTTPGatewayError as exc:
                # 致命错误（认证/权限/请求非法），不再重试。
                # 必须用 str(exc) —— last_error 在首次尝试时还是 None。
                logger.warning(
                    "HTTP call %s failed permanently error_type=%s", tool_name, type(exc).__name__
                )
                return normalize_tool_result(
                    tool_name,
                    arguments,
                    None,
                    error=str(exc) or "HTTP gateway error",
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}"
                logger.warning(
                    "HTTP call %s attempt %d/%d failed error_type=%s",
                    tool_name,
                    attempt + 1,
                    max_attempts,
                    type(exc).__name__,
                )

            if attempt < max_attempts - 1:
                backoff = 0.5 * (attempt + 1)
                remaining = _remaining()
                if remaining is not None:
                    if remaining <= 0:
                        return _budget_exhausted()
                    if backoff > remaining:
                        logger.info(
                            "HTTP call %s retry skipped retry_skipped_reason=backoff_exceeds_remaining "
                            "remaining_budget_ms=%.0f",
                            tool_name,
                            remaining * 1000,
                        )
                        return _budget_exhausted()
                    backoff = min(backoff, remaining)
                await asyncio.sleep(backoff)

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
