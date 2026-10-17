"""端到端实测所有 Gateway 接口 — 真正调用每个 HTTP path，检查是否有数据返回。"""
import asyncio
import json
import os
import sys
import time

if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import httpx

for line in open(".env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

API_KEY = os.environ.get("MARKET_GATEWAY_API_KEY", "")
GATEWAY_URL = os.environ.get("MARKET_GATEWAY_HTTP_URL", "")

print("=" * 75)
print("Mosaic ALL-Gateway Endpoint - Real Data Verification")
print(f"GATEWAY: {GATEWAY_URL}")
print(f"KEY: {'set' if API_KEY else 'EMPTY'}")
print("=" * 75)

from app.gateway.tool_registry import ALL_TOOLS

test_items = []
for t in ALL_TOOLS:
    if t.http_method == "INTERNAL":
        continue
    params = {}
    if t.key in ("quote", "hk_quote"):
        params = {"symbols": ["SH600519"]}
    elif t.key in ("search", "hk_search"):
        params = {"keyword": "茅台"}
    elif t.key in ("overview",):
        params = {"symbol": "SH600519"}
    elif t.key in ("detail",):
        params = {"symbol": "SH600519"}
    elif t.key in ("finance", "business", "concept", "shareholders", "survey"):
        params = {"symbol": "SH600519"}
    elif t.key in ("longhu",):
        params = {"symbol": "SH600519"}
    elif t.key in ("abnormal_reasons",):
        params = {"symbol": "SH600519"}
    elif t.key in ("orderbook",):
        params = {"symbol": "SH600519"}
    elif t.key in ("trades",):
        params = {"symbol": "SH600519"}
    elif t.key in ("timeline",):
        params = {"symbol": "SH600519"}
    elif t.key in ("klines", "commodity_gold"):
        params = {"symbol": "BTC/USDT:USDT", "interval": "1d", "start": "2024-01-01", "end": "2024-01-02"}
    elif t.key in ("snapshot", "commodity_silver", "commodity_platinum"):
        params = {"symbol": "XAU/USDT:USDT"}
    elif t.key in ("window", "us_window"):
        params = {"symbol": "BTC/USDT:USDT", "anchor": "2024-01-15"}
    elif t.key in ("derivatives_history",):
        params = {"symbol": "BTC/USDT:USDT"}
    test_items.append({
        "key": t.key,
        "tool_name": t.tool_name,
        "http_method": t.http_method,
        "http_path": t.http_path,
        "domain": t.domain,
        "category": t.category,
        "purpose": t.purpose[:60],
        "params": params,
    })


def safe_status(val):
    """Ensure status code is an integer."""
    try:
        return int(val)
    except:
        return 0


def analyze(raw, key):
    """Analyze raw response text"""
    if not raw or len(raw) < 5:
        return {"structure": "empty", "length": 0}

    # Try JSON
    try:
        data = json.loads(raw)
    except:
        # Might be JSONP — try to strip callback wrapper
        stripped = raw.strip().lstrip(");").lstrip("(jsonp_callback(")
        stripped = stripped.rstrip(");").rstrip(")").lstrip("(")
        try:
            data = json.loads(stripped)
        except:
            return {"structure": "raw-nonjson", "length": len(raw), "preview": raw[:150]}

    if isinstance(data, dict):
        keys = list(data.keys())
        for k in keys:
            kl = k.lower()
            if "error" in kl or ("fail" in kl and len(kl) > 4):
                val = data[k]
                if isinstance(val, str):
                    return {"structure": "error-response", "preview": val[:100]}

        if "source" in data and "type" in data:
            src = data.get("source", "?")
            dtype = data.get("type", "?")
            series = data.get("series", {})
            items = data.get("items", [])
            count = data.get("count", 0)

            if series:
                vals = list(series.values())
                return {"structure": f"data-series ({src}/{dtype})", "values": len(vals),
                        "has_data": True, "preview": str(list(vals)[:10])[:120]}
            if items:
                return {"structure": f"data-items ({src}/{dtype})", "count": len(items),
                        "has_data": True, "preview": str(items[:2])[:150]}
            if count:
                return {"structure": f"data-count ({src}/{dtype})", "count": count,
                        "has_data": True, "preview": raw[:150]}
            return {"structure": f"meta-data ({src}/{dtype})", "keys": keys[:5],
                    "has_data": False, "preview": raw[:150]}

        if "datas" in data:
            rows = data["datas"]
            cols = data.get("columns", [])
            return {"structure": f"query2data ({len(rows)} rows)", "has_data": len(rows) > 0,
                    "col_count": len(cols), "preview": str(rows[:2])[:150] if rows else "(no rows)"}
        if "data" in data:
            arr = data["data"]
            return {"structure": f"search ({len(arr)} items)", "has_data": len(arr) > 0,
                    "preview": str(arr[:2])[:150] if arr else "(no items)"}
        if "result" in data:
            return {"structure": "result-wrapper", "preview": str(data["result"])[:150]}

        return {"structure": f"dict({keys[:5]})"}
    elif isinstance(data, list):
        return {"structure": f"array({len(data)} items)", "has_data": len(data) > 0,
                "preview": str(data[:2])[:150]}
    return {"structure": type(data).__name__, "preview": str(data)[:150]}


async def call_endpoint(item):
    start = time.time()
    method = item["http_method"].upper()
    url = f"{GATEWAY_URL}{item['http_path']}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            kwargs = {"headers": {"X-API-Key": API_KEY}}
            if method == "GET":
                resp = await client.get(url, params=item["params"] or None, **kwargs)
            else:
                resp = await client.post(url, json=item["params"] or {}, **kwargs)

        elapsed = round((time.time() - start) * 1000, 0)
        hc = resp.status_code
        raw_text = resp.text[:2000]
        result = analyze(raw_text, item["key"])
        r = {
            "status": hc,
            "elapsed_ms": f"{elapsed:.0f}ms",
            "result": result,
            "error": None if 200 <= hc < 400 else f"HTTP {hc}",
        }
        return r
    except Exception as e:
        elapsed = round((time.time() - start) * 1000, 0)
        err_str = str(e)[:200]
        r = {
            "status": 0,
            "elapsed_ms": f"{elapsed:.0f}ms",
            "result": {"structure": "error", "preview": err_str[:100]},
            "error": err_str,
        }
        return r


async def run_all():
    results = []
    print(f"\nTesting {len(test_items)} endpoints...")
    print("-" * 75)

    for i, item in enumerate(test_items):
        r = await call_endpoint(item)
        r["item"] = item
        results.append(r)

        hc = safe_status(r["status"])
        summary = r.get("result", {})
        structure = summary.get("structure", "?")
        has_data = summary.get("has_data", False)
        data_info = " [有数据]" if has_data else ""

        mark = "+ " if 200 <= hc < 400 else ("! " if hc >= 400 else "~ ")
        elapsed = r.get("elapsed_ms", "?")
        domain = item["domain"]
        purpose = item["purpose"]

        line = f"  {mark} [{domain}] {item['key']:<30} HTTP {str(hc):>3} {elapsed} | {structure}{data_info}"
        print(line)
        if structure == "error-response" and not has_data:
            preview = summary.get("preview", "")
            if preview: print(f"      → {preview[:100]}")

    return results


if __name__ == "__main__":
    all_results = asyncio.run(run_all())

    # Group and summarize
    groups = {}
    for r in all_results:
        item = r["item"]
        domain = item["domain"]
        cat = item["category"]
        key = f"{domain}/{cat}"
        if key not in groups:
            groups[key] = {"ok": 0, "err": 0, "total": 0, "details": []}
        groups[key]["total"] += 1
        hc = safe_status(r["status"])
        if 200 <= hc < 400:
            groups[key]["ok"] += 1
        else:
            groups[key]["err"] += 1
        groups[key]["details"].append(r)

    print("\n" + "=" * 75)
    print("SUMMARY BY CATEGORY")
    print("=" * 75)

    total_ok = 0
    total_err = 0

    for gkey in sorted(groups.keys()):
        g = groups[gkey]
        ok = g["ok"]
        err = g["err"]
        total = g["total"]
        total_ok += ok
        total_err += err

        bar = "+" * ok + "!" * err
        print(f"  [{gkey:>30}] {ok:>3}/{total} OK  {bar}")

    print(f"\n{'='*75}")
    print(f"TOTAL: {len(all_results)} endpoints | OK={total_ok} | ERR={total_err}")
    print("=" * 75)

    # Detailed breakdown
    print("\nDETAILED RESULTS:")
    print("-" * 75)

    for gkey in sorted(groups.keys()):
        g = groups[gkey]
        print(f"\n[{gkey}] ({g['ok']}/{g['total']} OK)")
        for r in g["details"]:
            item = r["item"]
            hc = safe_status(r["status"])
            summary = r.get("result", {})
            struct = summary.get("structure", "?")
            has_data = summary.get("has_data", False)
            preview = summary.get("preview", "")

            mark = "+" if 200 <= hc < 400 else "!"
            info = " [有数据]" if has_data else ""
            print(f"  {mark} {item['key']:<30} HTTP {hc:>3} | {struct}{info}")
            if preview and has_data:
                print(f"     preview: {preview[:120]}")
