"""一次性探针：实测 Market Gateway 雪球通道的美股可用性（US_STOCK_INTEGRATION_PLAN §A0）。

用法：
    .venv/Scripts/python.exe scripts/verify_us_market.py

A0 实测（2026-10-06）最终可用的最小 body（记档，供后续排障）：
    POST /market/snapshot  {"symbol": "AAPL", "exchange": "xueqiu"}
    POST /market/klines    {"symbol": "AAPL", "exchange": "xueqiu",
                            "interval": "1d", "start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
    POST /market/window    {"symbol": "AAPL", "exchange": "xueqiu",
                            "interval": "1d", "anchor": "YYYY-MM-DD"}
    （exchange 必须显式传 "xueqiu"，默认值是 binance）
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

SYMBOLS = ["AAPL", "NVDA", "TSLA", "SPCX"]  # SPCX 是 OpenAPI 文档原例，用于核对格式

# klines / window 的 required 字段（allowed_openapi.json requestBody schema）
KLINE_INTERVAL = "1d"
KLINE_START = "2026-09-20"
KLINE_END = "2026-10-05"
WINDOW_ANCHOR = "2026-10-02"

# 判定"返回了真实行情数据"的关键字（按端点区分）
PRICE_KEYS = {
    "/market/snapshot": ("last", "last_price", "close", "price"),
    "/market/klines": ("close", "c", "data", "candles"),
    "/market/window": ("close", "c", "data", "candles"),
}


def load_env(path: str = ".env") -> dict[str, str]:
    """极简 .env 解析（KEY=VALUE，忽略注释与空行）。"""
    env: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _looks_like_price(body: object) -> bool:
    """粗判响应里是否含行情数字（避免把错误 JSON 当成功）。"""
    text = json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body
    lowered = text.lower()
    return any(k in lowered for k in ("last", "close", "price", "candle", "open")) and any(ch.isdigit() for ch in text)


async def probe_endpoint(client: httpx.AsyncClient, url: str, path: str, body: dict) -> tuple[bool, str]:
    """POST 一个端点，返回 (是否返回真实行情, 简述)。"""
    t0 = time.monotonic()
    try:
        resp = await client.post(url + path, json=body, timeout=30.0)
    except Exception as exc:  # noqa: BLE001 探针脚本要吞掉一切网络异常
        return False, f"EXC {type(exc).__name__}: {exc}"
    elapsed = (time.monotonic() - t0) * 1000
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        payload = resp.text[:200]
    if resp.status_code == 422:
        detail = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)[:300]
        return False, f"422 {detail}"
    if resp.status_code != 200:
        detail = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)[:300]
        return False, f"HTTP {resp.status_code} {detail}"
    ok = _looks_like_price(payload)
    snippet = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)[:400]
    return ok, f"200 ({elapsed:.0f}ms) {snippet}"


async def main() -> None:
    env = load_env()
    base_url = os.environ.get("MARKET_GATEWAY_HTTP_URL") or env.get("MARKET_GATEWAY_HTTP_URL", "")
    api_key = os.environ.get("MARKET_GATEWAY_API_KEY") or env.get("MARKET_GATEWAY_API_KEY", "")
    if not base_url or not api_key:
        logger.info("缺少 MARKET_GATEWAY_HTTP_URL / MARKET_GATEWAY_API_KEY，闸门无法实测 → 按失败处理")
        raise SystemExit(1)

    headers = {"X-API-Key": api_key}
    table: dict[str, dict[str, str]] = {sym: {} for sym in SYMBOLS}

    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers) as client:
        # 三个端点对四个 symbol 全量独立实测（不因 snapshot 失败跳过 klines/window）
        bodies: dict[str, dict] = {
            "/market/snapshot": {"exchange": "xueqiu"},
            "/market/klines": {
                "exchange": "xueqiu",
                "interval": KLINE_INTERVAL,
                "start": KLINE_START,
                "end": KLINE_END,
            },
            "/market/window": {
                "exchange": "xueqiu",
                "interval": KLINE_INTERVAL,
                "anchor": WINDOW_ANCHOR,
            },
        }
        for path, base_body in bodies.items():
            name = path.rsplit("/", 1)[-1]
            for sym in SYMBOLS:
                body = {"symbol": sym, **base_body}
                ok, note = await probe_endpoint(client, base_url, path, body)
                table[sym][name] = "OK" if ok else "FAIL"
                logger.info("%-9s %-6s → %s | %s", name, sym, table[sym][name], note[:220])

    print()
    print("=" * 60)
    print("symbol x 端点 可用性表（exchange=xueqiu）")
    print("=" * 60)
    endpoints = ["snapshot", "klines", "window"]
    print(f"{'symbol':<8} " + " ".join(f"{e:>10}" for e in endpoints))
    for sym in SYMBOLS:
        row = " ".join(f"{table[sym].get(e, '-'):>10}" for e in endpoints)
        print(f"{sym:<8} {row}")

    ok_syms = [s for s in SYMBOLS if table[s].get("snapshot") == "OK" and table[s].get("klines") == "OK"]
    ok_klines = [s for s in SYMBOLS if table[s].get("klines") == "OK"]
    print()
    print(f"闸门判定：snapshot+klines 双通的 symbol = {ok_syms}")
    if len(ok_syms) >= 2:
        print("→ 通过（≥2 个主流 symbol，可按计划 §2-A1 注册）")
    elif ok_klines or any(table[s].get("snapshot") == "OK" for s in SYMBOLS):
        print("→ 部分通过（只注册验证通过的端点组合，purpose 写明限制）")
    else:
        print("→ 失败（Phase A 停止）")


if __name__ == "__main__":
    asyncio.run(main())
