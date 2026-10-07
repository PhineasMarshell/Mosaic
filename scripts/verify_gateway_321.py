"""B0 探针闸门：Market Gateway 3.2.1 改名迁移的真值验证（真调上游，留档）。

用法：
    .venv/Scripts/python.exe scripts/verify_gateway_321.py

产出四张表：
  1. 旧 operationId 真调（旧路径）→ 证明迁移必要性（预期 403/404/死路）。
  2. 新 operationId 真调（新路径）→ 预期 200，记录耗时。
  3. 边界确认：/market/snapshot 的 crypto 通、xueqiu 422；/market/quotes 分隔符/批量。
  4. A 股筹码 8 条（/ashare/chips/*）→ 预期 coverage_status=not_collected（不注册依据）。

另含注册表自检（不联网）：全部新名可 resolve_tool_by_name、旧名全部不存在。
该自检在 B1 改名前运行会报 FAIL——属预期；改名后重跑应全绿。
"""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

# ------------------------------------------------------------------ #
# 旧名清单（现注册表 tool_name，含旧路径与最小入参）                     #
# ------------------------------------------------------------------ #

KLINE_BODY = {"symbol": "BTC/USDT", "exchange": "binance", "interval": "1d", "start": "2026-09-20", "end": "2026-10-05"}
WINDOW_BODY = {"symbol": "BTC/USDT", "exchange": "binance", "interval": "1d", "anchor": "2026-10-05"}
DERIV_BODY = {"symbol": "BTC/USDT", "interval": "1d"}
AS_OF = "2026-10-06T00:00:00Z"

# (旧 tool_name, method, 旧路径, args)
OLD_PROBES: list[tuple[str, str, str, dict[str, Any]]] = [
    ("public_sentiment_ashare_master_sentiment_get", "GET", "/ashare-master/sentiment", {}),
    ("public_limit_up_count_ashare_master_limit_up_count_get", "GET", "/ashare-master/limit-up/count", {}),
    ("public_limit_up_sectors_ashare_master_limit_up_sectors_get", "GET", "/ashare-master/limit-up/sectors", {}),
    ("public_limit_up_pool_ashare_master_limit_up_pool_get", "GET", "/ashare-master/limit-up/pool", {}),
    ("overview_eastmoney_overview_get", "GET", "/eastmoney/overview", {}),
    ("detail_eastmoney_detail_get", "GET", "/eastmoney/detail", {}),
    ("business_eastmoney_f10_business_get", "GET", "/eastmoney/f10/business", {"symbol": "SH600519"}),
    ("quote_tencent_quote_get", "GET", "/tencent/quote", {"symbols": "SH600519"}),
    ("longhu_xueqiu_longhu_get", "GET", "/xueqiu/longhu", {}),
    ("abnormal_reasons_xueqiu_abnormal_reasons_get", "GET", "/xueqiu/abnormal-reasons", {"symbol": "SH600519"}),
    ("orderbook_xueqiu_orderbook_get", "GET", "/xueqiu/orderbook", {"symbol": "SH600519"}),
    ("trades_xueqiu_trades_get", "GET", "/xueqiu/trades", {"symbol": "SH600519"}),
    ("timeline_xueqiu_timeline_get", "GET", "/xueqiu/timeline", {"symbol": "SH600519"}),
    ("search_xueqiu_search_get", "GET", "/xueqiu/search", {"query": "茅台"}),
    ("klines_market_klines_post", "POST", "/market/klines", KLINE_BODY),
    ("snapshot_market_snapshot_post", "POST", "/market/snapshot", {"symbol": "BTC/USDT"}),
    ("window_market_window_post", "POST", "/market/window", WINDOW_BODY),
    ("exchanges_market_exchanges_get", "GET", "/market/exchanges", {}),
    ("derivatives_history_market_derivatives_history_post", "POST", "/market/derivatives/history", DERIV_BODY),
    (
        "hyperliquid_symbols_coinglass_hyperliquid_symbols_get",
        "GET",
        "/coinglass/hyperliquid/symbols",
        {},
    ),
    ("liquidation_today_coinglass_liquidation_today_get", "GET", "/coinglass/liquidation/today", {}),
    ("funding_rate_coinglass_funding_rate_get", "GET", "/coinglass/funding-rate", {}),
    ("health_health_get", "GET", "/health", {}),
    ("health_market_health_get", "GET", "/market/health", {}),
]

# (新 tool_name, method, 新路径, args)。雪球系 4 条上游故障期照测，如实记录。
NEW_PROBES: list[tuple[str, str, str, dict[str, Any]]] = [
    ("get_ashare_sentiment", "GET", "/ashare-master/sentiment", {}),
    ("get_limit_up_count", "GET", "/ashare-master/limit-up/count", {}),
    ("list_limit_up_sectors", "GET", "/ashare-master/limit-up/sectors", {}),
    ("list_limit_up_stocks", "GET", "/ashare-master/limit-up/pool", {}),
    ("get_company_overview", "GET", "/eastmoney/overview", {}),
    ("get_company_detail", "GET", "/eastmoney/detail", {}),
    ("get_company_business", "GET", "/eastmoney/f10/business", {"symbol": "SH600519"}),
    ("get_market_quotes", "GET", "/market/quotes", {"symbols": "SH600519"}),
    ("get_stock_longhu", "GET", "/market/longhu", {}),
    ("get_stock_abnormal_reasons", "GET", "/market/abnormal-reasons", {"symbol": "SH600519"}),
    ("get_market_orderbook", "GET", "/market/orderbook", {"symbol": "SH600519"}),
    ("list_market_trades", "GET", "/market/trades", {"symbol": "SH600519"}),
    ("list_stock_discussions", "GET", "/market/discussions", {"symbol": "SH600519"}),
    ("search_stocks", "GET", "/market/search", {"query": "茅台"}),
    ("get_market_klines", "POST", "/market/klines", KLINE_BODY),
    ("get_market_snapshot", "POST", "/market/snapshot", {"symbol": "BTC/USDT"}),
    ("get_market_window", "POST", "/market/window", WINDOW_BODY),
    ("list_exchanges", "GET", "/market/exchanges", {}),
    ("get_derivatives_history", "POST", "/market/derivatives/history", DERIV_BODY),
    ("list_hyperliquid_symbols", "GET", "/coinglass/hyperliquid/symbols", {}),
    ("get_crypto_liquidation_today", "GET", "/coinglass/liquidation/today", {}),
    ("list_crypto_funding_rates", "GET", "/coinglass/funding-rate", {}),
    ("get_service_health", "GET", "/health", {}),
    ("get_market_health", "GET", "/market/health", {}),
]

# 筹码 8 条（实测预期 coverage_status=not_collected → 本轮不注册）
CHIPS_PROBES: list[tuple[str, str, dict[str, Any]]] = [
    ("get_ashare_capital_flow", "GET", {"symbol": "SH600519", "as_of": AS_OF}),
    ("list_ashare_holders", "GET", {"symbol": "SH600519", "as_of": AS_OF}),
    ("list_ashare_reductions", "GET", {"symbol": "SH600519", "as_of": AS_OF}),
    ("list_ashare_unlocks", "GET", {"symbol": "SH600519", "as_of": AS_OF}),
    ("get_latest_ashare_capital_flow", "GET", {"symbol": "SH600519"}),
    ("get_latest_ashare_holders", "GET", {"symbol": "SH600519"}),
    ("get_latest_ashare_reductions", "GET", {"symbol": "SH600519"}),
    ("get_latest_ashare_unlocks", "GET", {"symbol": "SH600519"}),
]


def load_env(path: str = ".env") -> dict[str, str]:
    env: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


async def call_one(
    client: httpx.AsyncClient,
    name: str,
    method: str,
    path: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        resp = await client.request(
            method, path, params=args if method == "GET" else None, json=args if method != "GET" else None
        )
        elapsed = (time.monotonic() - t0) * 1000
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001 探针吞掉一切解析异常
            payload = resp.text[:200]
        return {"name": name, "status": resp.status_code, "ms": elapsed, "body": payload}
    except Exception as exc:  # noqa: BLE001
        return {
            "name": name,
            "status": "EXC",
            "ms": (time.monotonic() - t0) * 1000,
            "body": f"{type(exc).__name__}: {exc}",
        }


def summarize(body: Any) -> str:
    if isinstance(body, str):
        return body[:160]
    text = json.dumps(body, ensure_ascii=False)
    return text[:160]


def coverage_of(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("coverage_status", body.get("error", "?")))
    return "?"


async def main() -> None:
    env = load_env()
    base_url = os.environ.get("MARKET_GATEWAY_HTTP_URL") or env.get("MARKET_GATEWAY_HTTP_URL", "")
    api_key = os.environ.get("MARKET_GATEWAY_API_KEY") or env.get("MARKET_GATEWAY_API_KEY", "")
    if not base_url or not api_key:
        raise SystemExit("缺少 MARKET_GATEWAY_HTTP_URL / MARKET_GATEWAY_API_KEY")

    print("=" * 78)
    print("0) 注册表自检（不联网）：新名可解析、旧名不存在")
    print("=" * 78)
    from app.gateway.tool_registry import BY_NAME, resolve_tool_by_name

    old_names = {p[0] for p in OLD_PROBES}
    new_names = {p[0] for p in NEW_PROBES}
    stale = sorted(old_names & set(BY_NAME))
    missing = sorted(n for n in new_names if n not in BY_NAME)
    for n in sorted(new_names):
        try:
            resolve_tool_by_name(n)
            print(f"  resolve OK   {n}")
        except KeyError as exc:
            print(f"  resolve FAIL {n}: {exc}")
    print(f"  旧名残留（应为空）: {stale or '无'}")
    print(f"  新名缺失（应为空）: {missing or '无'}")

    headers = {"X-API-Key": api_key}
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=60.0) as client:
        print()
        print("=" * 78)
        print("1) 旧名真调（旧路径）—— 证明迁移必要性")
        print("=" * 78)
        for name, method, path, args in OLD_PROBES:
            r = await call_one(client, name, method, path, args)
            print(f"  {r['status']:>4} {r['ms']:>7.0f}ms  {method} {path:<44} {summarize(r['body'])[:100]}")

        print()
        print("=" * 78)
        print("2) 新名真调（新路径）")
        print("=" * 78)
        for name, method, path, args in NEW_PROBES:
            r = await call_one(client, name, method, path, args)
            print(f"  {r['status']:>4} {r['ms']:>7.0f}ms  {name:<32} {summarize(r['body'])[:110]}")

        print()
        print("=" * 78)
        print("3) 边界确认：snapshot 交易所边界 + /market/quotes 分隔符/批量")
        print("=" * 78)
        r = await call_one(client, "snapshot(crypto)", "POST", "/market/snapshot", {"symbol": "BTC/USDT"})
        print(f"  snapshot BTC/USDT            → {r['status']} ({r['ms']:.0f}ms) {summarize(r['body'])[:100]}")
        r = await call_one(
            client, "snapshot(xueqiu)", "POST", "/market/snapshot", {"symbol": "AAPL", "exchange": "xueqiu"}
        )
        print(f"  snapshot AAPL exchange=xueqiu → {r['status']} ({r['ms']:.0f}ms) {summarize(r['body'])[:100]}")
        for label, params in [
            ("单值", {"symbols": "SH600519"}),
            ("逗号批量", {"symbols": "SH600519,SZ000001"}),
            ("重复键批量", [("symbols", "SH600519"), ("symbols", "SZ000001")]),
            ("空格批量", {"symbols": "SH600519 SZ000001"}),
        ]:
            t0 = time.monotonic()
            try:
                resp = await client.request("GET", "/market/quotes", params=params)
                ms = (time.monotonic() - t0) * 1000
                print(f"  quotes {label:<8} → {resp.status_code} ({ms:.0f}ms) {summarize(resp.json())[:110]}")
            except Exception as exc:  # noqa: BLE001
                print(f"  quotes {label:<8} → EXC {type(exc).__name__}: {exc}")

        print()
        print("=" * 78)
        print("4) A 股筹码 8 条（预期 coverage_status=not_collected）")
        print("=" * 78)
        for name, method, args in CHIPS_PROBES:
            path = "/ashare/chips/latest/" + name.rsplit("_", 1)[-1] if name.startswith("get_latest") else ""
            if not path:
                kind = {
                    "get_ashare_capital_flow": "capital",
                    "list_ashare_holders": "holders",
                    "list_ashare_reductions": "reductions",
                    "list_ashare_unlocks": "unlocks",
                }[name]
                path = f"/ashare/chips/{kind}"
            r = await call_one(client, name, method, path, args)
            print(
                f"  {r['status']:>4} {r['ms']:>7.0f}ms  {name:<32} coverage={coverage_of(r['body'])} {summarize(r['body'])[:90]}"
            )

    print()
    print("闸门判定：见第 2) 节 —— 全部新名可达（200/有数据语义的 4xx 除外）→ 继续 B1。")


if __name__ == "__main__":
    asyncio.run(main())
