"""SEC EDGAR 美股基本面 — 内部直连工具。

不经过 Market Gateway（MCP / HTTP），直接调用 SEC EDGAR 公开 API 获取美股
公司的 XBRL 结构化财务数据与申报文件元信息。Planner 通过 tool_registry 发现
此工具，ToolRuntime 走 _execute_internal 路径执行。

使用方式：
    - ToolMeta.http_method = "INTERNAL", tool_name = "internal_us_fundamentals" /
      "internal_us_filings_recent"
    - Planner 生成步骤时传入 {symbol}（美股裸代码，如 AAPL）
    - 结果经 tool_runtime 的 _normalize_us_* 展开为 NormalizedDatum 条目

SEC 公平访问政策（硬性要求）：
    - 所有请求必须带声明身份的 User-Agent（SEC_EDGAR_CONTACT 配置项，
      格式如 "YourName your@email.com"）；无合规 UA 会被 403
    - 限速 10 次/秒（本模块低频使用，不触及）

A0 实测（2026-10-06，scripts/verify_sec_edgar.py）：
    - 三类端点带合规 UA 均 200；无 UA 403
    - AAPL/NVDA 六个候选 tag 全部存在（HTTP 200），但 EPS 的单位是
      "USD/shares" 而非 "USD"；营收 tag 有新旧之分 —— AAPL 现行是
      RevenueFromContractWithCustomerExcludingAssessedTax（Revenues 停在
      2018-09-29），NVDA 相反（Revenues 现行，RFCWC 停在 2022-01-30）。
      因此候选 tag 全部并发拉取后按最新 end 日期选取，而不是顺序回退。
"""

import asyncio
import logging
import time
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# SEC EDGAR 端点                                                       #
# ------------------------------------------------------------------ #

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:0>10}/us-gaap/{tag}.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"
_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{primary_doc}"

#: UA 模板 —— SEC 要求声明访问身份；contact 来自 settings.sec_edgar_contact
_EDGAR_UA_BASE = "{contact} (Mosaic market-intelligence research)"

#: ticker → CIK 映射的模块级缓存 TTL（全量约 780KB，不能每次调用都拉）
_TICKER_CACHE_TTL = 24 * 3600.0

#: 申报文件过滤的 form 集合
_RECENT_FORMS = ("10-K", "10-Q", "8-K")

_ANNUAL_FORM = "10-K"
_QUARTERLY_FORM = "10-Q"

# 候选 tag —— A0 实测排序见模块 docstring。同一指标组内所有候选并发拉取，
# 按"最新 10-K/10-Q 条目的 end 日期"选优（tag 存在但数据陈旧也要让位）。
_METRIC_TAG_CANDIDATES: dict[str, list[str]] = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"],
    "net_income": ["NetIncomeLoss"],
    "gross_profit": ["GrossProfit"],
    "eps": ["EarningsPerShareDiluted", "EarningsPerShareBasic"],
}

# XBRL 单位键优先级：金额类是 "USD"，EPS 类是 "USD/shares"（A0 实测）
_UNIT_KEYS = ("USD", "USD/shares")

# 模块级 ticker → CIK 缓存（不要放 market_cache——1MB 全量映射表不适合共享行情缓存）
_ticker_map_cache: dict[str, int] | None = None
_ticker_map_loaded_at: float = 0.0


# ------------------------------------------------------------------ #
# 请求构建（纯函数）                                                   #
# ------------------------------------------------------------------ #


def _build_headers(contact: str) -> dict[str, str]:
    """构建带 SEC 合规 User-Agent 的请求头。"""
    return {"User-Agent": _EDGAR_UA_BASE.format(contact=contact)}


def _concept_url(cik: int, tag: str) -> str:
    return _CONCEPT_URL.format(cik=cik, tag=tag)


# ------------------------------------------------------------------ #
# 数据解析（纯函数）                                                   #
# ------------------------------------------------------------------ #


def _parse_ticker_map(payload: Any) -> dict[str, int]:
    """解析 company_tickers.json → {TICKER: cik_str}。

    结构：{"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    """
    if not isinstance(payload, dict):
        return {}
    mapping: dict[str, int] = {}
    for item in payload.values():
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker", "")).strip().upper()
        cik = item.get("cik_str")
        if ticker and isinstance(cik, int):
            mapping[ticker] = cik
    return mapping


def _pick_concept(units: dict[str, Any], form: str) -> dict[str, Any] | None:
    """从 companyconcept 的 units 里取指定 form 的最新一条。

    按 _UNIT_KEYS 优先级选单位键（金额 USD、EPS USD/shares），过滤
    form 匹配且 end/val 有效的条目，按 (end, filed) 降序取最新。

    Returns:
        {"value", "end", "filed", "form", "fy", "fp"} 或 None（无有效条目）。
    """
    if not isinstance(units, dict):
        return None
    entries: list[dict[str, Any]] = []
    for unit_key in _UNIT_KEYS:
        arr = units.get(unit_key)
        if isinstance(arr, list) and arr:
            entries = [e for e in arr if isinstance(e, dict)]
            break
    rows = [e for e in entries if e.get("form") == form and e.get("end") and isinstance(e.get("val"), (int, float))]
    if not rows:
        return None
    latest = max(rows, key=lambda e: (str(e.get("end", "")), str(e.get("filed", ""))))
    return {
        "value": latest["val"],
        "end": latest.get("end"),
        "filed": latest.get("filed"),
        "form": latest.get("form"),
        "fy": latest.get("fy"),
        "fp": latest.get("fp"),
    }


def _summarize_metric(concepts: dict[str, Any]) -> dict[str, Any] | None:
    """把同一指标组的多个候选 tag 载荷归并为 {"tag", "annual", "quarterly"}。

    候选 tag 之间按"最新年报条目的 end 日期"选优（A0 实测：tag 存在但可能
    陈旧，如 AAPL 的 Revenues 停在 2018）。选中的 tag 决定 annual/quarterly
    的取数来源，避免混用不同口径的两个 tag。

    Args:
        concepts: {tag: companyconcept 载荷或 None（拉取失败）}
    """
    usable: dict[str, dict[str, Any]] = {}
    for tag, payload in concepts.items():
        if not isinstance(payload, dict):
            continue
        units = payload.get("units")
        if not isinstance(units, dict):
            continue
        annual = _pick_concept(units, _ANNUAL_FORM)
        if annual is not None:
            usable[tag] = units
    if not usable:
        return None
    best_tag = max(
        usable,
        key=lambda t: str(_pick_concept(usable[t], _ANNUAL_FORM)["end"]),
    )
    units = usable[best_tag]
    return {
        "tag": best_tag,
        "annual": _pick_concept(units, _ANNUAL_FORM),
        "quarterly": _pick_concept(units, _QUARTERLY_FORM),
    }


def _parse_recent_filings(payload: Any, cik: int, limit: int = 10) -> list[dict[str, Any]]:
    """解析 submissions 的 filings.recent，过滤 form 并取最近 limit 份。

    recent 是等长并行数组（form / filingDate / accessionNumber / primaryDocument）。
    """
    if not isinstance(payload, dict):
        return []
    recent = (payload.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accessions = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocument") or []

    filings: list[dict[str, Any]] = []
    for i, form in enumerate(forms):
        if form not in _RECENT_FORMS:
            continue
        if i >= len(accessions) or i >= len(dates):
            continue
        accession = str(accessions[i] or "")
        primary_doc = str(docs[i]) if i < len(docs) and docs[i] else ""
        accession_nodash = accession.replace("-", "")
        filings.append(
            {
                "form": form,
                "filing_date": dates[i],
                "accession_no": accession,
                "document_url": (
                    _ARCHIVES_URL.format(
                        cik=cik,
                        accession_nodash=accession_nodash,
                        primary_doc=primary_doc,
                    )
                    if primary_doc
                    else None
                ),
                "primary_doc": primary_doc or None,
            }
        )
        if len(filings) >= limit:
            break
    return filings


# ------------------------------------------------------------------ #
# 拉取（async）                                                        #
# ------------------------------------------------------------------ #


def _edgar_contact() -> str:
    """读取 SEC_EDGAR_CONTACT（调用方负责空值时的门闩，这里不默认占位）。"""
    return get_settings().sec_edgar_contact


async def _load_ticker_map(contact: str) -> dict[str, int]:
    """惰性加载 ticker → CIK 映射（模块级缓存，24h TTL）。"""
    global _ticker_map_cache, _ticker_map_loaded_at
    now = time.monotonic()
    if _ticker_map_cache is not None and (now - _ticker_map_loaded_at) < _TICKER_CACHE_TTL:
        return _ticker_map_cache
    async with httpx.AsyncClient(timeout=30.0, headers=_build_headers(contact)) as client:
        resp = await client.get(_TICKERS_URL)
        resp.raise_for_status()
        mapping = _parse_ticker_map(resp.json())
    if not mapping:
        raise RuntimeError("SEC EDGAR company_tickers.json 解析结果为空")
    _ticker_map_cache = mapping
    _ticker_map_loaded_at = now
    return mapping


async def _resolve_cik(symbol: str, contact: str) -> int:
    """裸代码 → CIK。未收录的代码抛 KeyError。"""
    mapping = await _load_ticker_map(contact)
    cik = mapping.get(symbol.upper())
    if cik is None:
        raise KeyError(f"SEC EDGAR 未收录 ticker: {symbol}")
    return cik


async def _fetch_concept(client: httpx.AsyncClient, cik: int, tag: str) -> dict[str, Any] | None:
    """拉取单个 us-gaap 概念序列；失败（含 404 概念不存在）返回 None 不抛出。"""
    try:
        resp = await client.get(_concept_url(cik, tag))
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001 单概念缺失/网络抖动不整体失败
        logger.info("SEC EDGAR concept %s fetch failed: %s", tag, exc)
        return None


async def fetch_us_fundamentals(symbol: str) -> dict[str, Any]:
    """获取美股公司基本面（营收/净利/EPS/毛利的最新年报与季报值）。

    Returns:
        {
          "symbol", "cik",
          "revenue" | "net_income" | "eps" | "gross_profit":
              {"tag", "annual": {...}|None, "quarterly": {...}|None} | None,
          "caveats": [...], "source_urls": [...]
        }
        单指标缺失 → 该键置 None + caveat，不整体失败。
    """
    contact = _edgar_contact()
    if not contact:
        raise RuntimeError("SEC_EDGAR_CONTACT 未配置（SEC 公平访问政策要求声明访问身份）")
    sym = symbol.strip().upper()
    cik = await _resolve_cik(sym, contact)

    tags = [t for group in _METRIC_TAG_CANDIDATES.values() for t in group]
    async with httpx.AsyncClient(timeout=30.0, headers=_build_headers(contact)) as client:
        payloads = await asyncio.gather(*(_fetch_concept(client, cik, t) for t in tags))
    by_tag = dict(zip(tags, payloads, strict=True))

    result: dict[str, Any] = {"symbol": sym, "cik": cik}
    caveats: list[str] = []
    source_urls: list[str] = []
    for metric, candidates in _METRIC_TAG_CANDIDATES.items():
        concepts = {t: by_tag.get(t) for t in candidates}
        summarized = _summarize_metric(concepts)
        result[metric] = summarized
        if summarized is None:
            caveats.append(f"{metric}: 候选 tag {candidates} 均无有效 10-K/10-Q 数据")
        else:
            source_urls.append(_concept_url(cik, summarized["tag"]))
            if summarized["quarterly"] is None:
                caveats.append(f"{metric}: 无最新季报（10-Q）数据，仅有年报值")
    result["caveats"] = caveats
    result["source_urls"] = source_urls
    return result


async def fetch_us_recent_filings(symbol: str, limit: int = 10) -> list[dict[str, Any]]:
    """获取美股公司近期申报文件元信息（form ∈ {10-K, 10-Q, 8-K}）。"""
    contact = _edgar_contact()
    if not contact:
        raise RuntimeError("SEC_EDGAR_CONTACT 未配置（SEC 公平访问政策要求声明访问身份）")
    sym = symbol.strip().upper()
    cik = await _resolve_cik(sym, contact)
    async with httpx.AsyncClient(timeout=30.0, headers=_build_headers(contact)) as client:
        resp = await client.get(_SUBMISSIONS_URL.format(cik=cik))
        resp.raise_for_status()
        return _parse_recent_filings(resp.json(), cik, limit=limit)
