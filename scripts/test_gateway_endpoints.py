"""端到端接口实测 — 真正调 iiix market-gateway HTTP API，看返回数据。"""
import asyncio
import json
import os
import sys
import time

if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import httpx

# Read env from .env file with explicit UTF-8 encoding
env_path = "d:/Mosaic/.env"
for line in open(env_path, encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

GATEWAY_URL = os.environ.get("MARKET_GATEWAY_HTTP_URL", "https://api.x.iiix.dev/v1/d/market-gateway")
API_KEY = os.environ.get("MARKET_GATEWAY_API_KEY", "")
IWENCAI_KEY = os.environ.get("IWENCAI_API_KEY", "")

print("=" * 70)
print("Mosaic Gateway Endpoint - Actual Data Test")
print(f"GATEWAY: {GATEWAY_URL}")
print(f"IWENCAI_API_KEY: {'set' if IWENCAI_KEY else 'NOT SET'}")
print(f"MARKET_GATEWAY_API_KEY: {'set' if API_KEY else 'NOT SET'}")
print("=" * 70)

results = {}


def analyze_response(op: str, data: dict, elapsed_ms: float) -> dict:
    """Analyze the response structure and content"""
    if data is None:
        return {"structure": "null-response", "status": "empty"}
    # Check for error fields
    for key in ("error", "message", "detail"):
        if key in data and isinstance(data[key], str):
            msg = data[key][:100]
            if "error" in msg.lower() or "fail" in msg.lower():
                return {"structure": "error-response", "status": msg[:80]}
    # Common success patterns
    if "datas" in data:
        rows = data["datas"]
        cols = data.get("columns", [])
        return {
            "structure": f"query2data ({len(rows)} rows)",
            "has_data": len(rows) > 0,
            "preview": str(rows[:2])[:200] if rows else "(no rows)",
        }
    elif "data" in data:
        arr = data["data"]
        return {"structure": f"search ({len(arr)} items)", "has_data": len(arr) > 0,
                "preview": str(arr[:2])[:200] if arr else "(no items)"}
    elif "result" in data:
        return {"structure": "result-wrapper", "preview": str(data["result"])[:200]}
    elif isinstance(data, dict) and "code" in data and "msg" in data:
        return {"structure": "code-msg", "status": f"{data['code']}: {str(data['msg'])[:60]}"}
    elif isinstance(data, dict):
        keys = list(data.keys())[:5]
        return {"structure": f"dict({keys})", "keys": keys}
    elif isinstance(data, list):
        return {"structure": f"array({len(data)} items)", "preview": str(data[:2])[:200]}
    else:
        return {"structure": type(data).__name__, "preview": str(data)[:200]}


async def call_gateway(op: str, params: dict, label: str, timeout=30) -> dict:
    """Call the iiix market-gateway HTTP endpoint directly"""
    start = time.time()
    body_str = ""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                GATEWAY_URL,
                json={"operation": op, "parameters": params},
                headers={"X-API-Key": API_KEY, "Accept": "application/json"},
            )
        elapsed = round((time.time() - start) * 1000, 0)
        status_code = resp.status_code
        body_str = resp.text[:3000]
        try:
            data_json = resp.json()
            summary = analyze_response(op, data_json, elapsed)
        except:
            summary = {"structure": "non-json", "status": body_str[:200]}

        r = {
            "http_status": status_code,
            "elapsed_ms": f"{elapsed:.0f}ms",
            "summary": summary,
            "raw_preview": body_str[:500],
        }
        err_msg = None if 200 <= status_code < 400 else f"HTTP {status_code}: {body_str[:200]}"
        r["error"] = err_msg
        results[op] = r

        if 200 <= status_code < 400:
            mark = "+ "
        elif status_code >= 400:
            mark = "! "
        else:
            mark = "~ "
        print(f"  {mark} {label:<45} HTTP {status_code:>3} {r['elapsed_ms']} | {summary.get('structure','?')}")
        if summary.get("preview"):
            print(f"      preview: {summary['preview'][:120]}")
        return r
    except Exception as e:
        elapsed = round((time.time() - start) * 1000, 0)
        err_msg = str(e)
        results[op] = {
            "http_status": "-",
            "elapsed_ms": f"{elapsed:.0f}ms",
            "summary": {"structure": "error", "status": err_msg[:100]},
            "raw_preview": "",
            "error": err_msg,
        }
        mark = "~ " if "timeout" in err_msg.lower() else "! "
        print(f"  {mark} {label:<45} ERROR | {err_msg[:100]}")
        return results[op]


async def run_tests():
    # --- Test 1: Direct health check ---
    print("\n[Test 1] Health Check (direct)")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{GATEWAY_URL}/health", headers={"X-API-Key": API_KEY})
        print(f"  GET /health -> HTTP {resp.status_code}")
        print(f"      {resp.text[:300]}")
    except Exception as e:
        print(f"  Health check failed: {e}")

    # --- Try listing tools ---
    print("\n[Test 1b] List available tools via OpenAPI spec")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{GATEWAY_URL}/openapi.json", headers={"X-API-Key": API_KEY})
        if resp.status_code == 200:
            spec = resp.json()
            paths = spec.get("paths", {})
            ops = list(paths.keys())[:20]
            print(f"  Found {len(paths)} endpoints:")
            for p in sorted(ops):
                methods = list(paths[p].keys())
                print(f"    {p} [{','.join(methods)}]")
        else:
            print(f"  Failed: HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"  Error: {e}")

    # --- Test 2: Market Quotes ---
    print("\n[Test 2] Market Quotes (腾讯行情)")
    await call_gateway("quote", {"symbols": ["SH600519"]}, "get_market_quotes(SH600519)", timeout=30)
    await call_gateway("quote", {"symbols": ["SZ000001"]}, "get_market_quotes(SZ000001)", timeout=30)

    # --- Test 3: A-share Master ---
    print("\n[Test 3] A-share Master (涨停情绪)")
    await call_gateway("limit_up_count", {}, "get_limit_up_count", timeout=30)
    await call_gateway("sentiment", {}, "get_ashare_sentiment", timeout=30)
    await call_gateway("limit_up_sectors", {}, "list_limit_up_sectors", timeout=30)
    await call_gateway("limit_up_pool", {}, "list_limit_up_stocks", timeout=30)

    # --- Test 4: Eastmoney ---
    print("\n[Test 4] Eastmoney 东财基本面")
    await call_gateway("overview", {"symbol": "SH600519"}, "get_company_overview(SH600519)", timeout=30)
    await call_gateway("detail", {"symbol": "SH600519"}, "get_company_detail(SH600519)", timeout=30)

    # --- Test 5: CoinGlass/Hyperliquid ---
    print("\n[Test 5] CoinGlass/Hyperliquid")
    await call_gateway("liqmap", {}, "get_hyperliquid_liquidation_map", timeout=30)
    await call_gateway("top_position", {}, "get_hyperliquid_top_position", timeout=30)
    await call_gateway("funding_rate", {}, "list_crypto_funding_rates", timeout=30)

    # --- Test 6: News ---
    print("\n[Test 6] 新闻快讯")
    await call_gateway("telegraph", {}, "internal_market_telegraph", timeout=30)

    # --- Test 7: THS/iwencai Skills ---
    print("\n[Test 7] THS Skill: iwencai")
    await call_gateway("iwencai_market_query",
                       {"query": "贵州茅台 最新价 涨跌幅 2026-10-16"},
                       "iwencai_market_query(茅台行情)", timeout=30)
    await call_gateway("iwencai_basicinfo_query",
                       {"query": "贵州茅台 股票简称 所属行业"},
                       "iwencai_basicinfo_query(茅台基本信息)", timeout=30)

    # --- Test 8: Company fundamentals ---
    print("\n[Test 8] Company Fundamentals (东财)")
    await call_gateway("finance", {"symbol": "SH600519"}, "get_company_finance(SH600519)", timeout=30)
    await call_gateway("concept", {"symbol": "SH600519"}, "get_company_concepts(SH600519)", timeout=30)


def final_report():
    print("\n" + "=" * 70)
    print("FINAL RESULTS BY CATEGORY")
    print("=" * 70)

    groups = [
        ("Health Check", ["health"]),
        ("腾讯行情 (Tencent Quotes)", ["quote-sh600519", "quote-sz000001"]),
        ("A股Master (涨停情绪)", ["limit_up_count", "sentiment", "limit_up_sectors", "limit_up_pool"]),
        ("东财基本面 (Eastmoney)", ["overview-sh600519", "detail-sh600519", "finance-sh600519", "concept-sh600519"]),
        ("CoinGlass/Hyperliquid", ["liqmap", "top_position", "funding_rate"]),
        ("新闻快讯", ["telegraph"]),
        ("THS/iwencai Skills", ["iwencai_market_query", "iwencai_basicinfo_query"]),
    ]

    total_ok = 0
    total_fail = 0

    for cat_name, ops in groups:
        cat_ok = 0
        cat_fail = 0
        print(f"\n[{cat_name}]")
        for op in ops:
            r = results.get(op)
            if not r:
                print(f"  ! {op} [NO RESULT]")
                total_fail += 1
                continue
            hc = r.get("http_status")
            if isinstance(hc, str):
                try:
                    hc = int(hc)
                except:
                    hc = 0
            summary = r.get("summary", {})
            structure = summary.get("structure", "?")
            preview = summary.get("preview", "")

            if 200 <= hc < 400:
                mark = "+"
                has_data = summary.get("has_data", False)
                data_info = " [有数据]" if has_data else ""
                cat_ok += 1
                total_ok += 1
            else:
                mark = "!"
                cat_fail += 1
                total_fail += 1

            print(f"  {mark} {op} -> HTTP {hc} | {structure}{data_info}")
            if preview: print(f"     {preview[:150]}")

    print(f"\n总计: OK={total_ok} Fail={total_fail}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_tests())
    final_report()
