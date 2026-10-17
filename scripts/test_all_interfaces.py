"""测试项目所有接口，按来源分类输出状态。

覆盖范围：
  - HTTP API 端点 (main.py)
  - Gateway 工具 (~63+个，来自 tool_registry.py)
    - iwencai/同花顺问财系列
    - ths_skill 路由系列
    - Eastmoney 东财系列
    - 雪球/腾讯行情系列
    - CoinGlass/Hyperliquid 加密货币系列
    - OKX 永续合约系列
    - SEC EDGAR 美股基本面系列
    - 内部新闻/快讯系列
    - 健康检查系列
"""
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

# Force UTF-8 stdout for emoji support on Windows
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import httpx

BASE_URL = "http://127.0.0.1:8000"

CATEGORIES = {
    "[1] HTTP API Endpoints": [],
    "[2] Health Checks": [],
    "[3] A-share Master (ashare-master)": [],
    "[4] Eastmoney Fundamental": [],
    "[5] Xueqiu/Tencent Market Micro": [],
    "[6] iwencai Official Query Tools": [],
    "[7] iwencai Search Tools": [],
    "[8] THS Skill Adapter Route": [],
    "[9] CoinGlass/Hyperliquid": [],
    "[10] OKX Perpetual Contracts": [],
    "[11] SEC EDGAR US Fundamentals": [],
    "[12] News/Telegraph": [],
    "[13] HK Northbound/Stock Connect": [],
}


@dataclass
class TestCase:
    name: str
    category: str
    method: str
    url: str = ""
    payload: dict = field(default_factory=dict)
    requires_key: str = ""
    tool_name: str = ""
    note: str = ""


results = {}
test_cases = []


def add(name, category, method, url="", payload=None, requires_key="", tool_name="", note=""):
    tc = TestCase(
        name=name, category=category, method=method,
        url=url or "", payload=payload or {},
        requires_key=requires_key, tool_name=tool_name, note=note,
    )
    test_cases.append(tc)
    if category in CATEGORIES:
        CATEGORIES[category].append(tc.name)


# ── Register all test cases ──────────────────────────────────────

add("GET /health",                    "[1] HTTP API Endpoints", "http_get", "/health")
add("GET /api/brief/morning",         "[1] HTTP API Endpoints", "http_get", "/api/brief/morning")
add("GET /api/brief/evening",         "[1] HTTP API Endpoints", "http_get", "/api/brief/evening")
add("POST /api/ask (sync)",           "[1] HTTP API Endpoints", "http_post", "/api/ask",
    {"question": "Guizhou Maotai latest stock price"})
add("POST /api/ask/stream",           "[1] HTTP API Endpoints", "http_post", "/api/ask/stream",
    {"question": "Guizhou Maotai latest stock price"}, note="SSE streaming")
add("GET /",                          "[1] HTTP API Endpoints", "http_get", "/", note="Web UI")

add("get_service_health",             "[2] Health Checks", "tool_call", tool_name="health",
    note="Gateway process health")
add("get_market_health",              "[2] Health Checks", "tool_call", tool_name="market_health",
    note="Market module status")

add("get_ashare_sentiment",           "[3] A-share Master (ashare-master)", "tool_call", tool_name="sentiment",
    note="A-share market sentiment timeseries")
add("get_limit_up_count",             "[3] A-share Master (ashare-master)", "tool_call", tool_name="limit_up_count",
    note="Daily limit-up stock count")
add("list_limit_up_sectors",          "[3] A-share Master (ashare-master)", "tool_call", tool_name="limit_up_sectors",
    note="Limit-up sectors & themes")
add("list_limit_up_stocks",           "[3] A-share Master (ashare-master)", "tool_call", tool_name="limit_up_pool",
    note="Limit-up stocks pool")

add("get_company_overview",       "[4] Eastmoney Fundamental", "tool_call", tool_name="overview",
    note="Full market valuation/market cap")
add("get_company_detail",         "[4] Eastmoney Fundamental", "tool_call", tool_name="detail",
    note="Shareholders/financing/restrictions")
add("get_company_business",       "[4] Eastmoney Fundamental", "tool_call", tool_name="business",
    note="Main business info")
add("get_company_concepts",       "[4] Eastmoney Fundamental", "tool_call", tool_name="concept",
    note="Concept tags")
add("get_company_finance",        "[4] Eastmoney Fundamental", "tool_call", tool_name="finance",
    note="Financial information")
add("get_company_shareholders",   "[4] Eastmoney Fundamental", "tool_call", tool_name="shareholders",
    note="Shareholder information")
add("get_company_survey",         "[4] Eastmoney Fundamental", "tool_call", tool_name="survey",
    note="Survey/investigation info")

add("get_market_quotes",          "[5] Xueqiu/Tencent Market Micro", "tool_call", tool_name="quote",
    note="Multi-market realtime quotes + 5-level order book (Tencent)")
add("get_stock_longhu",           "[5] Xueqiu/Tencent Market Micro", "tool_call", tool_name="longhu",
    note="Dragon-tiger list capital flow (Xueqiu)")
add("get_stock_abnormal_reasons", "[5] Xueqiu/Tencent Market Micro", "tool_call", tool_name="abnormal_reasons",
    note="Daily K-line anomaly attribution")
add("get_market_orderbook",       "[5] Xueqiu/Tencent Market Micro", "tool_call", tool_name="orderbook",
    note="Order book hanging orders")
add("list_market_trades",         "[5] Xueqiu/Tencent Market Micro", "tool_call", tool_name="trades",
    note="Tick-by-tick trades")
add("list_stock_discussions",     "[5] Xueqiu/Tencent Market Micro", "tool_call", tool_name="timeline",
    note="Xueqiu个股 comment discussions")
add("search_stocks",              "[5] Xueqiu/Tencent Market Micro", "tool_call", tool_name="search",
    note="Security search (A-share + HK)")

_Q = "[6] iwencai Official Query Tools"
add("internal_iwencai_sector_selector",      _Q, "internal", tool_name="iwencai_sector_selector",
    note="Official sector screening (hithink-sector-selector)")
add("internal_iwencai_business_query",       _Q, "internal", tool_name="iwencai_business",
    note="Official company operations (hithink-business-query)")
add("internal_iwencai_insresearch_query",    _Q, "internal", tool_name="iwencai_insresearch",
    note="Official institutional research (hithink-insresearch-query)")
add("internal_iwencai_index_query",          _Q, "internal", tool_name="iwencai_index",
    note="Official index data (hithink-zhishu-query)")
add("internal_iwencai_astock_selector",      _Q, "internal", tool_name="iwencai_astock_selector",
    note="Official A-share stock screener (hithink-astock-selector)")
add("internal_iwencai_industry_query",       _Q, "internal", tool_name="iwencai_industry",
    note="Official industry valuation (hithink-industry-query)")
add("internal_iwencai_management_query",     _Q, "internal", tool_name="iwencai_management",
    note="Official share structure (hithink-management-query)")
add("internal_iwencai_market_query",         _Q, "internal", tool_name="iwencai_market",
    note="Official stock/ETF/index quotes (hithink-market-query)")
add("internal_iwencai_macro_query",          _Q, "internal", tool_name="iwencai_macro",
    note="Official macroeconomic data (hithink-macro-query)")
add("internal_iwencai_finance_query",        _Q, "internal", tool_name="iwencai_finance",
    note="Official financial data (hithink-finance-query)")
add("internal_iwencai_basicinfo_query",      _Q, "internal", tool_name="iwencai_basicinfo",
    note="Official basic info (hithink-basicinfo-query)")
add("internal_iwencai_event_query",          _Q, "internal", tool_name="iwencai_events",
    note="Official event data (hithink-event-query)")

_S = "[7] iwencai Search Tools"
add("internal_iwencai_announcement_search",  _S, "internal", tool_name="announcement_search",
    note="Official announcement search (announcement-search)")
add("internal_iwencai_report_search",        _S, "internal", tool_name="iwencai_report_search",
    note="Official research report search (report-search)")
add("internal_iwencai_news_search",          _S, "internal", tool_name="iwencai_news_search",
    note="Official financial news search (news-search)")

_THS = "[8] THS Skill Adapter Route"
for tc in [t for t in test_cases if t.tool_name in {
    "iwencai_sector_selector", "iwencai_business", "iwencai_insresearch",
    "iwencai_index", "iwencai_astock_selector", "iwencai_industry",
    "iwencai_management", "iwencai_market", "iwencai_macro",
    "iwencai_finance", "iwencai_basicinfo", "iwencai_events",
    "announcement_search", "iwencai_report_search", "iwencai_news_search"
}]:
    new_tc = TestCase(
        name=f"[THS] {tc.name}", category=_THS, method=tc.method,
        tool_name=tc.tool_name, note=f"via THS skill adapter -> {tc.note}",
    )
    test_cases.append(new_tc)
    CATEGORIES[_THS].append(new_tc.name)

_CG = "[9] CoinGlass/Hyperliquid"
add("list_hyperliquid_symbols",           _CG, "tool_call", tool_name="hyperliquid_symbols",
    note="Hyperliquid settlement currency list")
add("get_hyperliquid_liquidation_map",    _CG, "tool_call", tool_name="liqmap",
    note="Whale positions & liquidation levels")
add("get_hyperliquid_top_position",       _CG, "tool_call", tool_name="top_position",
    note="Cross-currency top positions")
add("get_hyperliquid_user_count",         _CG, "tool_call", tool_name="user_count",
    note="Total address count timeseries")
add("list_hyperliquid_vaults",            _CG, "tool_call", tool_name="vaults",
    note="Vault APR/drawdown")
add("get_crypto_liquidation_today",       _CG, "tool_call", tool_name="liquidation_today",
    note="Daily total liquidation stats")
add("list_crypto_funding_rates",          _CG, "tool_call", tool_name="funding_rate",
    note="Funding rate leaderboard")

_OKX = "[10] OKX Perpetual Contracts"
add("get_market_klines_commodity",  _OKX, "tool_call", tool_name="klines",
    note="Generic historical K-lines (crypto/commodities) POST")
add("get_market_snapshot_crypto",   _OKX, "tool_call", tool_name="snapshot",
    note="Generic snapshot crypto POST")
add("get_market_window_crypto",     _OKX, "tool_call", tool_name="window",
    note="Review window aggregation crypto POST")
add("list_exchanges",               _OKX, "tool_call", tool_name="exchanges",
    note="Exchange/capability query")
add("get_derivatives_history",      _OKX, "tool_call", tool_name="derivatives_history",
    note="Perpetual funding rates/OI history")

_EDGAR = "[11] SEC EDGAR US Fundamentals"
add("internal_us_fundamentals",  _EDGAR, "internal", tool_name="us_fundamentals",
    note="SEC EDGAR XBRL fundamentals (requires SEC_EDGAR_CONTACT)")
add("internal_us_filings_recent", _EDGAR, "internal", tool_name="us_filings_recent",
    note="Recent SEC filings")

_NEWS = "[12] News/Telegraph"
add("news_search",                _NEWS, "internal", tool_name="news_search",
    note="DDGS news search (lightweight fallback)")
add("internal_symbol_news",       _NEWS, "internal", tool_name="symbol_news",
    note="A-share individual news (Eastmoney + Google)")
add("internal_market_telegraph",  _NEWS, "internal", tool_name="telegraph",
    note="CLS telegraph flash news")
add("internal_news_digest",       _NEWS, "internal", tool_name="news_digest",
    note="Multi-source news (Google+DDGS dedup)")

_NB = "[13] HK Northbound/Stock Connect"
add("internal_hk_northbound",  _NB, "internal", tool_name="hk_northbound_daily",
    note="HK Stock Connect northbound flow (Eastmoney)")
add("internal_hk_index",       _NB, "internal", tool_name="hk_index_snapshot",
    note="HSI & HS Tech index snapshot")

# Enumerate extra registry tools not yet covered
from app.gateway.tool_registry import ALL_TOOLS, BY_KEY

all_registered_keys = set(t.key for t in ALL_TOOLS)
known_keys = set()
for tc in test_cases:
    if tc.tool_name and tc.tool_name in BY_KEY:
        known_keys.add(BY_KEY[tc.tool_name].key)
    elif tc.tool_name:
        known_keys.add(tc.tool_name)

extra_keys = all_registered_keys - known_keys
if extra_keys:
    print(f"INFO: Found {len(extra_keys)} extra registry keys, adding them...")
    for ek in sorted(extra_keys):
        meta = BY_KEY.get(ek)
        if meta:
            cat_map = {
                "technical": "[5] Xueqiu/Tencent Market Micro",
                "fundamental": "[4] Eastmoney Fundamental",
                "news": "[12] News/Telegraph",
                "moneyflow": "[9] CoinGlass/Hyperliquid",
                "sentiment": "[5] Xueqiu/Tencent Market Micro",
                "shared": "[2] Health Checks",
            }
            cat = cat_map.get(meta.category, f"[?] {meta.domain}/{meta.category}")
            method = "internal" if meta.http_method == "INTERNAL" else "tool_call"
            add(f"{ek} ({meta.tool_name})", cat, method, tool_name=ek, note=meta.purpose[:80])


# ── Test helpers ──────────────────────────────────────────────────

def _is_timeout(e: Exception) -> bool:
    msg = str(e).lower()
    return "timeout" in msg or "readtimeout" in type(e).__name__.lower()


async def run_test():
    """Run all tests"""
    print("=" * 80)
    print("Mosaic Interface Comprehensive Scan & Test")
    print("=" * 80)
    print(f"\nServer: {BASE_URL}")
    print(f"Test cases: {len(test_cases)}\n")

    # === PHASE 1: HTTP endpoints ===
    print("=" * 80)
    print("PHASE 1: Testing HTTP API Endpoints")
    print("-" * 80)

    # GET /health
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{BASE_URL}/health")
        results["GET /health"] = {"status": "OK", "code": resp.status_code,
                                  "elapsed_ms": f"{resp.elapsed.total_seconds()*1000:.0f}ms",
                                  "body_preview": json.dumps(resp.json(), ensure_ascii=False)[:200],
                                  "error": None}
        print(f"  GET /health               [   OK] HTTP {resp.status_code}  {results['GET /health']['elapsed_ms']}")
    except Exception as e:
        r = {"status": "TIMEOUT" if _is_timeout(e) else ("FAIL" if "timed out" in str(e).lower() else "ERR"),
             "code": "-", "elapsed_ms": "-", "body_preview": "", "error": str(e)[:150]}
        results["GET /health"] = r
        print(f"  GET /health               [{r['status']:>5}] {r['error'][:50]}")

    # Brief endpoints - these may be long-running, use shorter timeout
    for endpoint, label in [("/api/brief/morning", "brief/morning"), ("/api/brief/evening", "brief/evening")]:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(f"{BASE_URL}{endpoint}")
            r_status = "OK" if 200 <= resp.status_code < 300 else ("ERR" if resp.status_code >= 400 else "WARN")
            results[f"GET {label}"] = {"status": r_status, "code": resp.status_code,
                                       "elapsed_ms": f"{resp.elapsed.total_seconds()*1000:.0f}ms",
                                       "body_preview": json.dumps(resp.json(), ensure_ascii=False)[:200] if resp.headers.get("content-type","").startswith("application/json") else resp.text[:200],
                                       "error": None if 200 <= resp.status_code < 400 else f"HTTP {resp.status_code}"}
            print(f"  GET {label:<20}   [{r_status:>5}] HTTP {resp.status_code}  {results[f'GET {label}']['elapsed_ms']}")
        except Exception as e:
            r = {"status": "WARN" if _is_timeout(e) else ("FAIL" if "timed out" in str(e).lower() else "ERR"),
                 "code": "-", "elapsed_ms": "-", "body_preview": "", "error": str(e)[:150]}
            results[f"GET {label}"] = r
            print(f"  GET {label:<20}   [{r['status']:>5}] {r['error'][:50]}")

    # POST /api/ask - use very short timeout just to verify connectivity
    print("\n  POST /api/ask ... (testing connectivity, research runs async)")
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.post(f"{BASE_URL}/api/ask", json={"question": "test"},
                                     headers={"Content-Type": "application/json"})
        r = {"status": "OK" if 200 <= resp.status_code < 300 else ("ERR" if resp.status_code >= 400 else "WARN"),
             "code": resp.status_code, "elapsed_ms": "-",
             "body_preview": json.dumps(resp.json(), ensure_ascii=False)[:200] if resp.headers.get("content-type","").startswith("application/json") else resp.text[:200],
             "error": None if 200 <= resp.status_code < 400 else f"HTTP {resp.status_code}"}
        results["POST /api/ask"] = r
        print(f"  POST /api/ask             [{r['status']:>5}] HTTP {resp.status_code}  response received")
    except Exception as e:
        r = {"status": "OK" if _is_timeout(e) else ("FAIL" if "Connection refused" in str(e) else "ERR"),
             "code": "-", "elapsed_ms": "-", "body_preview": "",
             "error": "Research accepted but timed out waiting (expected, budget ~1000s)" if _is_timeout(e) else str(e)[:150]}
        results["POST /api/ask"] = r
        print(f"  POST /api/ask             [{r['status']:>5}] {r['error'][:60]}")

    # SSE stream - test connection
    print("\n  POST /api/ask/stream ... (testing SSE connection)")
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            async with client.stream("POST", f"{BASE_URL}/api/ask/stream",
                                     json={"question": "test"}) as resp:
                first_line = await resp.areadline()
        r = {"status": "OK" if 200 <= resp.status_code < 300 else "ERR",
             "code": resp.status_code, "elapsed_ms": "-",
             "body_preview": first_line.decode("utf-8", errors="replace")[:200],
             "error": None if 200 <= resp.status_code < 400 else f"HTTP {resp.status_code}"}
        results["POST /api/ask/stream"] = r
        print(f"  POST /api/ask/stream      [   OK] HTTP {resp.status_code}  SSE connected")
    except Exception as e:
        r = {"status": "OK" if _is_timeout(e) else ("FAIL" if "Connection refused" in str(e) else "ERR"),
             "code": "-", "elapsed_ms": "-", "body_preview": "",
             "error": "Streaming alive (expected, budget ~1000s)" if _is_timeout(e) else str(e)[:150]}
        results["POST /api/ask/stream"] = r
        print(f"  POST /api/ask/stream      [{r['status']:>5}] {r['error'][:60]}")

    # GET /
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{BASE_URL}/")
        r = {"status": "OK" if 200 <= resp.status_code < 300 else "ERR",
             "code": resp.status_code, "elapsed_ms": f"{resp.elapsed.total_seconds()*1000:.0f}ms",
             "body_preview": resp.text[:200], "error": None if 200 <= resp.status_code < 300 else f"HTTP {resp.status_code}"}
        results["GET /"] = r
        print(f"  GET /                     [{r['status']:>5}] HTTP {resp.status_code}  {r['elapsed_ms']}")
    except Exception as e:
        r = {"status": "FAIL", "code": "-", "elapsed_ms": "-", "body_preview": "", "error": str(e)[:150]}
        results["GET /"] = r
        print(f"  GET /                     [FAIL] {e}")

    # === PHASE 2: Gateway tools ===
    print("\n" + "=" * 80)
    print("PHASE 2: Scanning & Classifying Gateway Tools")
    print("-" * 80)

    seen_tools = set()
    from app.gateway.tool_registry import BY_NAME, ALL_TOOLS

    required_env = {"IWENCAI_API_KEY": ["internal_iwencai_", "iwencai"],
                    "SEC_EDGAR_CONTACT": ["internal_us_"],
                    "MARKET_GATEWAY_API_KEY": ["get_market_", "get_stock_", "get_company_", "list_"]}

    for tc in test_cases:
        if tc.method not in ("tool_call", "internal"):
            continue
        key = tc.tool_name or tc.name
        if key in seen_tools:
            continue
        seen_tools.add(key)

        meta = None
        for t in ALL_TOOLS:
            if t.tool_name == key or t.key == key:
                meta = t
                break

        if not meta:
            r = {"status": "SKIP", "code": "-", "elapsed_ms": "0ms",
                 "body_preview": "", "error": f"'{key}' not in registry"}
            results[tc.name] = r
            print(f"  [ SKIP] {tc.name:<50} | not found")
            continue

        # Check env vars
        missing_env = []
        for env_key, prefixes in required_env.items():
            for prefix in prefixes:
                if key.startswith(prefix):
                    if not os.environ.get(env_key, ""):
                        missing_env.append(env_key)

        status_display = {"REGISTERED": "REGD", "WARN": "WARN", "SKIP": "SKIP", "OK": "OK"}
        if missing_env:
            status = "WARN"
            err_str = f"Missing: {', '.join(set(missing_env))}"
        elif meta.http_method == "INTERNAL":
            status = "REGISTERED"
            err_str = None
        else:
            status = "REGISTERED"
            err_str = None

        preview = f"path={meta.http_path or '(INTERNAL)'} domain={meta.domain} priority={meta.priority} category={meta.category}"
        r = {"status": status, "code": f"[{meta.http_method}]", "elapsed_ms": "0ms",
             "body_preview": preview, "error": err_str,
             "details": {"key": meta.key, "domain": meta.domain, "priority": meta.priority,
                         "http_method": meta.http_method, "path": meta.http_path,
                         "category": meta.category}}
        results[tc.name] = r
        icon = {"OK": "OK ", "REGISTERED": "REGD", "WARN": "WARN", "SKIP": "SKIP",
                "FAIL": "FAIL", "ERR": "ERR "}[status]
        print(f"  [{icon}{status:<6}] {tc.name:<45} | {err_str or preview[:80]}")

    print(f"\n  Total registered tools: {len(ALL_TOOLS)}")
    covered = len([tc for tc in test_cases if tc.method in ('tool_call','internal') and tc.tool_name in seen_tools])
    print(f"  Covered by scan: {covered}")

    # Also show uncovered tools
    all_names = set(t.tool_name for t in ALL_TOOLS)
    uncovered = all_names - seen_tools
    if uncovered:
        print(f"  Not yet mapped: {len(uncovered)}")
        for tn in sorted(uncovered)[:10]:
            m = next((t for t in ALL_TOOLS if t.tool_name == tn), None)
            if m:
                print(f"    - {tn}  [{m.domain}/{m.category}]")


def print_summary():
    """Print categorized summary report"""
    print("\n" + "=" * 80)
    print("CATEGORY SUMMARY REPORT")
    print("=" * 80)

    total = len(results)
    ok = sum(1 for r in results.values() if r['status'] in ('OK', 'REGISTERED'))
    warn = sum(1 for r in results.values() if r['status'] == 'WARN')
    fail = sum(1 for r in results.values() if r['status'] == 'FAIL')
    pending = sum(1 for r in results.values() if r['status'] in ('TIMEOUT', 'SKIP', 'ERR'))

    print(f"\n  TOTAL: {total} interfaces scanned")
    print(f"  OK/Registered: {ok}")
    print(f"  Warnings (missing config/API keys): {warn}")
    print(f"  Failed/Errors: {fail}")
    print(f"  Pending/Skipped: {pending}")

    # Category breakdown table
    print("\n" + "-" * 80)
    header = f"{'Category':<45} {'Total':>6} {'OK':>5} {'WARN':>5} {'FAIL':>5} {'Other':>6}"
    print(header)
    print("-" * 80)

    for cat_name in CATEGORIES:
        items = CATEGORIES.get(cat_name, [])
        if not items:
            continue
        c_total = len(items)
        c_ok = c_warn = c_fail = c_other = 0
        for name in items:
            s = results.get(name, {}).get('status', '?')
            if s in ('OK', 'REGISTERED'): c_ok += 1
            elif s == 'WARN': c_warn += 1
            elif s == 'FAIL': c_fail += 1
            else: c_other += 1
        print(f"  {cat_name:<43} {c_total:>6} {c_ok:>5} {c_warn:>5} {c_fail:>5} {c_other:>6}")

    # Detailed per-category report
    print("\n" + "=" * 80)
    print("DETAILED CATEGORY BREAKDOWN")
    print("=" * 80)

    for cat_name in CATEGORIES:
        items = CATEGORIES.get(cat_name, [])
        if not items:
            continue

        print(f"\n{'='*60}")
        print(f"  {cat_name}  ({len(items)} items)")
        print(f"{'='*60}")

        for item_name in items:
            r = results.get(item_name, {})
            status = r.get('status', '?')
            error = r.get('error', '') or ''
            details = r.get('details', {})

            icon_map = {"OK": "+ ", "REGISTERED": "= ", "WARN": "~ ",
                       "FAIL": "! ", "ERR": "! ", "SKIP": "- ", "TIMEOUT": "- "}
            icon = icon_map.get(status, "? ")

            line = f"  {icon}{item_name}"
            if details:
                d_part = f"  [domain={details.get('domain','?')}]"
                line += d_part
            if error:
                line += f"\n     -> {error[:120]}"
            print(line)


if __name__ == "__main__":
    asyncio.run(run_test())
    print_summary()

    # Save detailed results
    output_path = "tmp/interface_test_results.json"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "summary": {
                "total": len(results),
                "ok": sum(1 for r in results.values() if r['status'] in ('OK', 'REGISTERED')),
                "warning": sum(1 for r in results.values() if r['status'] == 'WARN'),
                "error": sum(1 for r in results.values() if r['status'] == 'FAIL'),
                "pending": sum(1 for r in results.values() if r['status'] in ('TIMEOUT', 'SKIP', 'ERR')),
            },
            "by_category": {k: v for k, v in CATEGORIES.items()},
            "results": {k: v for k, v in results.items()},
        }, f, ensure_ascii=False, indent=2)
    print(f"\nDetailed results saved to: {output_path}")
