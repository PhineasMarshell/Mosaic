"""港股通（南向资金）北向资金数据获取模块。

通过东方财富公开 KLineJSAPI 获取以下数据：
- 沪深股通净流入时序（HK.960036）
- 沪股通净流入时序（SH.960052）
- 深股通净流入时序（SZ.960053）
- 恒生指数行情（HK.800300）
- 恒生科技指数行情（HK.800505）

这些数据不经过 Market Gateway，而是 Mosaic 内部直连东方财富公开接口获取。
当 Gateway 侧接入独立工具端点后，本模块可退为 fallback 或直接移除。

使用方式：
    from app.research.hk_northbound import fetch_hk_northbound_data, fetch_hang_seng_index
    data = await fetch_hk_northbound_data()
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Eastmoney KLineJSAPI 端点                                            #
# ------------------------------------------------------------------ #

_EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/klines/get"

# secid → 指标映射
_SECID_MAP: dict[str, str] = {
    "northbound":   "HK.960036",       # 沪深股通净流入（合计）
    "sh_connect":   "SH.960052",       # 沪股通
    "sz_connect":   "SZ.960053",       # 深股通
    "hs_index":     "HK.800300",       # 恒生指数
    "hs_tech_index": "HK.800505",      # 恒生科技指数
}

# 每个 secid 需要取的数据天数
_DAILY_LOOKBACK_DAYS = 30  # 最近 30 个交易日足以覆盖研究窗口


def _build_params(secid: str, days: int = _DAILY_LOOKBACK_DAYS) -> dict[str, Any]:
    """构建东方财富 KLineJSAPI 请求参数。"""
    now = datetime.now(UTC)
    end = int(now.timestamp() * 1000)
    start = int((now - timedelta(days=days * 2)).timestamp() * 1000)  # 多取几天缓冲
    return {
        "secid": secid,
        "klt": "101",           # 日线
        "fqt": "1",             # 前复权
        "beg": 0,
        "end": "2099999999",
        "smct": "1",
        "ut": "fa5fd124367ec51bbdb959c0e198815c",  # 东方财富固定 ut token
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "cut": days,            # 截取最近 N 条
        "lmt": "100",
    }


# ------------------------------------------------------------------ #
# 数据解析                                                              #
# ------------------------------------------------------------------ #

def _parse_northbound(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """解析北向资金净流入时序数据。

    东财返回格式：
    {
      "data": {
        "hqData": [
          ["2024-09-01", "38486.19", "-15.32", ...],  // date, net_buy (北向), ...
        ]
      }
    }

    fields1 = f1(日期) f2(开) f3(收) f4(高) f5(低) f6(成交量)
    对于净流入指标，f3(收盘价) ≈ 当日北向净买入额（亿元）
    """
    if not raw or "data" not in raw:
        return []

    hq_data = (raw.get("data") or {}).get("hqData", []) or []
    results = []

    for item in hq_data:
        if len(item) < 3:
            continue
        date_str = item[0]          # "2024-09-01"
        try:
            net_buy = float(item[2])  # 第3列 = 收盘价 = 净流入金额(亿)
        except (ValueError, IndexError):
            continue
        results.append({
            "date": date_str,
            "net_inflow_billion_cny": round(net_buy, 2),
            "direction": "inflow" if net_buy > 0 else "outflow",
        })

    return results


def _parse_index(raw: dict[str, Any]) -> dict[str, Any] | None:
    """解析指数行情快照（最新一条）。"""
    if not raw or "data" not in raw:
        return None

    hq_data = (raw.get("data") or {}).get("hqData", []) or []
    if not hq_data:
        return None

    latest = hq_data[-1]  # 最新一条
    try:
        return {
            "date": latest[0],
            "close": float(latest[2]),
            "change_pct": float(latest[5]) if len(latest) > 5 else None,
        }
    except (ValueError, IndexError):
        return None


# ------------------------------------------------------------------ #
# 主接口                                                                  #
# ------------------------------------------------------------------ #

async def fetch_hk_northbound_data() -> dict[str, Any]:
    """获取港股通北向资金最近 30 日数据。

    Returns:
        {"northbound": [...], "sh_connect": [...], "sz_connect": [...]}
    """
    results: dict[str, list[dict]] = {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        for key, secid in [("northbound", "HK.960036"), ("sh_connect", "SH.960052"), ("sz_connect", "SZ.960053")]:
            try:
                params = _build_params(secid)
                resp = await client.get(_EASTMONEY_KLINE_URL, params=params)
                resp.raise_for_status()
                results[key] = _parse_northbound(resp.json())
            except Exception as exc:
                logger.warning("Failed to fetch %s (%s): %s", key, secid, exc)
                results[key] = []
    return results


async def fetch_hang_seng_index(days: int = 30) -> dict[str, Any]:
    """获取恒生指数 & 恒生科技指数 K 线数据。

    Returns:
        {"hs_index": {...}, "hs_tech_index": {...}}
    """
    results: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        for key, secid in [("hs_index", "HK.800300"), ("hs_tech_index", "HK.800505")]:
            try:
                params = _build_params(secid, days=days)
                resp = await client.get(_EASTMONEY_KLINE_URL, params=params)
                resp.raise_for_status()
                parsed = _parse_index(resp.json())
                results[key] = parsed or {}
            except Exception as exc:
                logger.warning("Failed to fetch %s (%s): %s", key, secid, exc)
                results[key] = {}
    return results


async def fetch_all_hk_context() -> dict[str, Any]:
    """一次性获取所有 HK 域上下文数据。

    这是 Detective 层调用的高速入口。
    """
    northbound, hs = await fetch_hk_northbound_data(), await fetch_hang_seng_index()
    combined = {**northbound, **hs}

    # 计算汇总统计
    nb = combined.get("northbound", [])
    if nb:
        recent_5 = nb[:5]
        combined["_summary"] = {
            "last_5d_avg_net_flow": round(sum(d["net_inflow_billion_cny"] for d in recent_5) / 5, 2),
            "total_days_fetched": len(nb),
            "net_direction_5d": "inflow" if sum(d["net_inflow_billion_cny"] for d in recent_5) > 0 else "outflow",
        }

    combined["_source"] = "eastmoney_kline_api_internal"
    return combined
