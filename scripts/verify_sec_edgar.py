"""一次性探针：实测 SEC EDGAR 三类端点可用性（SEC_EDGAR_PLAN §A0）。

用法：
    SEC_EDGAR_CONTACT="Mosaic research agent admin@example.com" \
        .venv/Scripts/python.exe scripts/verify_sec_edgar.py

说明：
- SEC 公平访问政策要求所有请求带声明身份的 User-Agent；未带合规 UA 会被 403。
  本脚本从环境变量 / .env 读 SEC_EDGAR_CONTACT；若都没有，用占位身份
  "Mosaic research agent" 跑通功能验证（不把任何真实邮箱硬编码进本文件）。
- 闸门判定（§A0）：
    通过     —— 三类端点带合规 UA 均 200 且载荷可控、≥2 家公司核心 tag 候选齐全
    部分通过 —— 个别 tag 缺失 / 载荷偏大 → 收缩工具能力
    失败     —— 合规 UA 仍 403 / 限流 / 结构不可解析 → 停止，不得换第三方源
"""

import asyncio
import logging
import os
import time
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:0>10}/us-gaap/{tag}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"

# A0 实测对象与候选 tag（SEC_EDGAR_PLAN §1.2）
COMPANIES = {"AAPL": 320193, "NVDA": 1045810}
REVENUE_TAGS = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"]
EPS_TAGS = ["EarningsPerShareDiluted", "EarningsPerShareBasic"]
OTHER_TAGS = ["NetIncomeLoss", "GrossProfit"]
ALL_TAGS = REVENUE_TAGS + OTHER_TAGS + EPS_TAGS

PLACEHOLDER_CONTACT = "Mosaic research agent"


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


async def probe(client: httpx.AsyncClient, url: str, label: str) -> tuple[int, bytes, object]:
    """GET 一个端点，返回 (状态码, 原始字节, 解析后的 JSON 或 None)。"""
    t0 = time.monotonic()
    try:
        resp = await client.get(url, timeout=30.0)
    except Exception as exc:  # noqa: BLE001 探针要吞掉一切网络异常
        logger.info("%-52s → EXC %s: %s", label, type(exc).__name__, exc)
        return -1, b"", None
    elapsed = (time.monotonic() - t0) * 1000
    size_kb = len(resp.content) / 1024
    payload = None
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        pass
    logger.info(
        "%-52s → HTTP %s  %.0f KB  %.0fms",
        label,
        resp.status_code,
        size_kb,
        elapsed,
    )
    return resp.status_code, resp.content, payload


def summarize_concept(payload: dict | None) -> str:
    """确认 units.USD 最新 10-K/10-Q 条目结构，返回简述。"""
    if not isinstance(payload, dict):
        return "结构不可解析"
    units = payload.get("units", {}).get("USD")
    if not units:
        return "存在但 units.USD 缺失/为空"
    for form in ("10-K", "10-Q"):
        entries = [u for u in units if u.get("form") == form]
        if entries:
            latest = max(entries, key=lambda u: u.get("end", ""))
            return (
                f"{form} 最新 end={latest.get('end')} val={latest.get('val')} "
                f"fy={latest.get('fy')} fp={latest.get('fp')} filed={latest.get('filed')}"
            )
    return "存在但无 10-K/10-Q 条目"


async def main() -> None:
    env = load_env()
    contact = os.environ.get("SEC_EDGAR_CONTACT") or env.get("SEC_EDGAR_CONTACT", "") or PLACEHOLDER_CONTACT
    if contact == PLACEHOLDER_CONTACT:
        logger.info("未配置 SEC_EDGAR_CONTACT，用占位身份 %r 跑功能验证", contact)
    ua = f"{contact} (Mosaic market-intelligence)"

    results: dict[str, str] = {}
    tag_table: dict[str, dict[str, str]] = {sym: {} for sym in COMPANIES}
    sizes: dict[str, float] = {}

    async with httpx.AsyncClient(headers={"User-Agent": ua}) as client:
        # ---- 1. ticker → CIK 映射 ----
        status, content, payload = await probe(client, TICKERS_URL, "company_tickers.json")
        sizes["company_tickers.json"] = len(content) / 1024
        if status == 200 and isinstance(payload, dict):
            found = {}
            for item in payload.values():
                if isinstance(item, dict) and item.get("ticker") in ("AAPL", "NVDA", "TSLA"):
                    found[item["ticker"]] = item.get("cik_str")
            results["tickers"] = "OK" if len(found) == 3 else "PARTIAL"
            logger.info("  映射条目数=%d，实测: %s", len(payload), found)
            for sym in COMPANIES:
                actual = found.get(sym)
                if actual is not None:
                    COMPANIES[sym] = actual  # 用实测 CIK，避免文档过期
        else:
            results["tickers"] = "FAIL"
            logger.info("  解析失败或非 200")

        # ---- 2. companyconcept：公司 × tag 存在性 ----
        for sym, cik in COMPANIES.items():
            for tag in ALL_TAGS:
                url = CONCEPT_URL.format(cik=cik, tag=tag)
                status, content, payload = await probe(client, url, f"companyconcept {sym} {tag}")
                sizes[f"{sym}/{tag}"] = len(content) / 1024
                key = "OK" if status == 200 and isinstance(payload, dict) else f"HTTP {status}"
                tag_table[sym][tag] = key
                if status == 200:
                    logger.info("  ↳ %s", summarize_concept(payload))

        # ---- 3. submissions（AAPL） ----
        aapl_cik = COMPANIES["AAPL"]
        status, content, payload = await probe(client, SUBMISSIONS_URL.format(cik=aapl_cik), "submissions AAPL")
        sizes["submissions/AAPL"] = len(content) / 1024
        if status == 200 and isinstance(payload, dict):
            recent = (payload.get("filings") or {}).get("recent") or []
            forms = recent.get("form") or []
            wanted = [f for f in forms if f in ("10-K", "10-Q", "8-K")][:10]
            results["submissions"] = "OK" if wanted else "PARTIAL"
            logger.info(
                "  recent 条数=%d，form 过滤(10-K/10-Q/8-K)最近 10 份=%s",
                len(forms),
                wanted,
            )
        else:
            results["submissions"] = "FAIL"

        # ---- 4. UA 对照：同一请求不带 UA ----
        async with httpx.AsyncClient() as bare_client:
            status_bare, _, _ = await probe(
                bare_client,
                CONCEPT_URL.format(cik=COMPANIES["AAPL"], tag="NetIncomeLoss"),
                "companyconcept AAPL NetIncomeLoss [无 UA 对照]",
            )
            results["ua_control"] = f"HTTP {status_bare}"

    # ---- 汇总 ----
    print()
    print("=" * 72)
    print("公司 × tag 存在性表（HTTP 200 = 存在）")
    print("=" * 72)
    header = f"{'tag':<52}" + " ".join(f"{sym:>9}" for sym in COMPANIES)
    print(header)
    for tag in ALL_TAGS:
        row = " ".join(f"{tag_table[sym].get(tag, '-'):>9}" for sym in COMPANIES)
        print(f"{tag:<52}{row}")
    print()
    print("载荷大小（KB）：")
    for k, v in sizes.items():
        print(f"  {k:<52} {v:10.1f}")
    print()
    print("UA 对照（无 UA 请求 companyconcept）：", results.get("ua_control"))

    print()
    core = {"revenue": REVENUE_TAGS, "net_income": ["NetIncomeLoss"], "eps": EPS_TAGS}
    complete = []
    for sym in COMPANIES:
        if any(tag_table[sym].get(t) == "OK" for t in core["revenue"]) and all(
            any(tag_table[sym].get(t) == "OK" for t in group) for group in core.values()
        ):
            complete.append(sym)
    endpoints_ok = results.get("tickers") in ("OK", "PARTIAL") and results.get("submissions") in ("OK", "PARTIAL")
    ua_ok = not str(results.get("ua_control", "")).endswith("200")
    print(f"核心概念（营收/净利/EPS）齐全的公司 = {complete}")
    if endpoints_ok and len(complete) >= 2 and ua_ok:
        print("→ 通过（三类端点带合规 UA 均 200，≥2 家公司核心 tag 齐全；可按计划 A1 实施）")
    elif endpoints_ok and complete and ua_ok:
        print("→ 部分通过（按实测收缩工具能力，purpose 如实描述）")
    else:
        print("→ 失败（§4 停止条件，不得换第三方源绕过）")


if __name__ == "__main__":
    asyncio.run(main())
