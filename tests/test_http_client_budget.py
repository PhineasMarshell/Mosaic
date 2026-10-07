"""T23 回归：HTTP 重试必须感知总预算（deadline）。

旧实现：max_attempts 次重试 + 429 退避完全不看整次调查还剩多少时间——
单工具最坏 70s，串行 8 个工具远超 research_budget_seconds(300s)，
结果是整次调查 504，而不是"超预算就降级返回 error ToolResult"。
"""

import time

import httpx

from app.config import Settings
from app.gateway.http_client import MarketGatewayHttpClient


def _make_client(handler) -> MarketGatewayHttpClient:
    gw = MarketGatewayHttpClient(Settings(max_retry_per_tool=3))
    gw.client = httpx.AsyncClient(base_url="http://gateway.test", transport=httpx.MockTransport(handler))
    return gw


async def _close(gw: MarketGatewayHttpClient):
    if gw.client is not None:
        await gw.client.aclose()
    gw.client = None


async def test_retry_stops_when_deadline_exhausted():
    """deadline 用尽 → 不再重试，返回 status=error 且 error 写明 budget exhausted。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429)  # 每次都限流（本地 mock，无真实耗时）

    gw = _make_client(handler)
    try:
        deadline = time.monotonic() + 0.05
        result = await gw.call("get_market_quotes", {"symbol": "600519"}, deadline=deadline)
    finally:
        await _close(gw)

    # 旧实现（不看 deadline）：max_attempts=4 次请求全部打满
    assert 1 <= calls["n"] <= 2, f"预算用尽后不应继续重试，实际请求 {calls['n']} 次"
    assert result.status == "error"
    assert "budget exhausted" in (result.error or "")


async def test_already_expired_deadline_makes_no_request():
    """deadline 已过 → 一次请求都不发，直接返回 error ToolResult（不抛异常炸链）。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应被走到
        calls["n"] += 1
        return httpx.Response(200, json={"ok": 1})

    gw = _make_client(handler)
    try:
        result = await gw.call("get_market_quotes", {"symbol": "600519"}, deadline=time.monotonic() - 1)
    finally:
        await _close(gw)

    assert calls["n"] == 0
    assert result.status == "error"
    assert "budget exhausted" in (result.error or "")


async def test_generous_budget_keeps_retry_behaviour():
    """预算充足时重试行为不变：第一次 500 → 重试 → 成功。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500)
        return httpx.Response(200, json={"data": {"price": 1.0}})

    gw = _make_client(handler)
    try:
        result = await gw.call(
            "get_market_quotes",
            {"symbol": "600519"},
            deadline=time.monotonic() + 60,
        )
    finally:
        await _close(gw)

    assert calls["n"] == 2  # max_retry_per_tool=1 → 恰好重试一次
    assert result.status == "success"


async def test_backoff_capped_by_remaining_budget():
    """429 的退避时间不得超过剩余预算（本用例退避被裁到 <1s，整体远快于旧实现）。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429)

    gw = _make_client(handler)
    try:
        started = time.monotonic()
        result = await gw.call("get_market_quotes", {"symbol": "600519"}, deadline=time.monotonic() + 0.3)
        elapsed = time.monotonic() - started
    finally:
        await _close(gw)

    assert result.status == "error"
    assert "budget exhausted" in (result.error or "")
    # 旧实现退避 0.5+1+2 = 3.5s（4 次尝试）；新实现被 deadline 裁剪
    assert elapsed < 2.0, f"退避未被剩余预算裁剪：耗时 {elapsed:.2f}s"
