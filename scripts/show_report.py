import json, sys, io

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

with open("tmp/interface_test_results.json", encoding="utf-8") as f:
    data = json.load(f)
r = data["results"]
s = data["summary"]

print("=" * 75)
print("  MOSAIC 项目全部接口测试报告 - 按数据源分类")
print("=" * 75)
print()
print(f"总计扫描 {s['total']} 个接口")
print(f"  OK/已注册: {s['ok']}")
print(f"  警告(缺配置): {s['warning']}")
print(f"  失败: {s['error']}")
print()

# [1] iwencai official
print("=" * 75)
print("  [一] 同花顺问财官方API (iwencai) — 需 IWENCAI_API_KEY")
print("=" * 75)
for name in sorted(k for k in r if "iwencai" in k.lower()):
    rv = r[name]
    st = rv["status"]
    icon_map = {"OK": "+ ", "REGISTERED": "= ", "WARN": "~ ", "ERR": "! "}
    icon = icon_map.get(st, "? ")
    dm = ""
    for p in str(rv.get("body_preview","")).split():
        if p.startswith("domain="): dm = p
    status_text = {"OK": "可用", "REGISTERED": "已注册(内部)", "WARN": "缺API KEY", "ERR": "错误"}.get(st, st)
    print(f"  {icon} {name:<50} [{dm}] ({status_text})")
    if rv.get("error"): print(f"      -> {rv['error'][:60]}")

# [2] THS Skill Adapter route
print()
print("=" * 75)
print("  [二] THS Skill Adapter路由 (同花顺Skill走market_gateway)")
print("=" * 75)
for name in sorted(k for k in r if "[THS]" in k or "skill" in k.lower()):
    rv = r[name]
    st = rv["status"]
    icon_map = {"OK": "+ ", "REGISTERED": "= ", "WARN": "~ ", "ERR": "! ", "SKIP": "- "}
    icon = icon_map.get(st, "? ")
    clean_name = name.replace("[THS] ", "")
    status_text = {"OK": "可用", "REGISTERED": "已注册(内部)", "WARN": "缺配置", "ERR": "错误", "SKIP": "未映射"}.get(st, st)
    print(f"  {icon} {clean_name:<50} ({status_text})")
    if rv.get("error"): print(f"      -> {rv['error'][:60]}")

# [3] A-share Master / Limit-up
print()
print("=" * 75)
print("  [三] A股Master端点 (涨停家数/情绪/题材)")
print("=" * 75)
for name in sorted(k for k in r if any(x in k.lower() for x in ["sentiment", "limit_up"]) and "iwencai" not in k.lower()):
    rv = r[name]
    dm = ""
    for p in str(rv.get("body_preview","")).split():
        if p.startswith("domain="): dm = p
    print(f"  + {name:<50} [{dm}] 已注册({rv['code']})")

# [4] Eastmoney
print()
print("=" * 75)
print("  [四] 东财Eastmoney基本面数据")
print("=" * 75)
for name in sorted(k for k in r if any(x in k.lower() for x in ["company_", "overview", "concept", "finance", "survey"]) and "fundamental" not in k.lower()):
    rv = r[name]
    dm = ""
    preview = ""
    for p in str(rv.get("body_preview","")).split():
        if p.startswith("domain="): dm = p
        elif p.startswith("path="): preview = p
    print(f"  + {name:<50} [{dm}] {preview}")

# [5] Market Micro / Quotes
print()
print("=" * 75)
print("  [五] 行情通用端点 (雪球/Tencent/跨域)")
print("=" * 75)
for name in sorted(k for k in r if any(x in k.lower() for x in ["quote", "kline", "snapshot", "window", "orderbook", "trades", "longhu", "abnormal", "discuss", "search", "commodity", "exchange"]) and "http" not in k.lower() and "brief" not in k.lower()):
    rv = r[name]
    dm = ""
    preview = ""
    for p in str(rv.get("body_preview","")).split():
        if p.startswith("domain="): dm = p
        elif p.startswith("path="): preview = p
    print(f"  + {name:<50} [{dm}] {preview}")

# [6] CoinGlass/Hyperliquid
print()
print("=" * 75)
print("  [六] CoinGlass/Hyperliquid加密数据")
print("=" * 75)
for name in sorted(k for k in r if any(x in k.lower() for x in ["hyperliquid", "liqmap", "vaults", "funding_rate", "liquidation_today"])):
    rv = r[name]
    dm = ""
    for p in str(rv.get("body_preview","")).split():
        if p.startswith("domain="): dm = p
    print(f"  + {name:<50} [{dm}]")

# [7] SEC EDGAR / US fundamentals / HK Northbound
print()
print("=" * 75)
print("  [七] SEC EDGAR美股 & 港股通北向资金")
print("=" * 75)
for name in sorted(k for k in r if any(x in k.lower() for x in ["us_fundamental", "us_filings", "hk_northbound", "hk_index"])):
    rv = r[name]
    dm = ""
    preview = ""
    for p in str(rv.get("body_preview","")).split():
        if p.startswith("domain="): dm = p
        elif p.startswith("path="): preview = p
    print(f"  + {name:<50} [{dm}] {preview}")

# [8] News
print()
print("=" * 75)
print("  [八] 新闻快讯")
print("=" * 75)
for name in sorted(k for k in r if any(x in k.lower() for x in ["news", "telegraph", "digest", "symbol_news"]) and "iwencai" not in k.lower()):
    rv = r[name]
    dm = ""
    for p in str(rv.get("body_preview","")).split():
        if p.startswith("domain="): dm = p
    print(f"  + {name:<50} [{dm}]")

# [9] HTTP API endpoints
print()
print("=" * 75)
print("  [九] HTTP API端点")
print("=" * 75)
for name in sorted(k for k in r if k.startswith(("GET ", "POST "))):
    rv = r[name]
    st = rv["status"]
    http_code = rv.get("code", "-")
    err = rv.get("error", "") or ""
    detail = rv.get("body_preview", "")[:60]
    status_map = {"OK": "可用", "ERR": "错误", "WARN": "异常", "FAIL": "失败", "TIMEOUT": "超时等待"}
    print(f"  {'+ ' if st=='OK' else '~ ':2} {name:<45} [{str(http_code):>4}] {status_map.get(st, st)}")
    if err and len(err) < 100: print(f"       -> {err[:80]}")

# [10] Health checks
print()
print("=" * 75)
print("  [十] 健康检查")
print("=" * 75)
for name in sorted(k for k in r if "health" in k.lower()):
    rv = r[name]
    dm = ""
    for p in str(rv.get("body_preview","")).split():
        if p.startswith("domain="): dm = p
    print(f"  + {name:<45} [{str(rv.get('code','-'))}] {dm}")

print()
print("=" * 75)
print(f"总结: {s['total']}个接口 | 正常/{s['ok']} | 警告/{s['warning']} | 失败/{s['error']}")
print("=" * 75)
