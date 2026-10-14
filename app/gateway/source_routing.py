"""Configuration driven primary/fallback source routing for research tools.

The registry owns the operation -> Skill mapping.  This module only builds a
bounded, auditable Skill query from canonical tool arguments; it never infers
dates or instruments from retrieved time or free-form result text.
"""

from __future__ import annotations

import re
from typing import Any

from app.gateway.ths_skill_registry import THS_PRIMARY_ROUTE


def _date(arguments: dict[str, Any]) -> str:
    for key in ("as_of_date", "date", "end_date", "end", "trade_date", "trading_date"):
        value = arguments.get(key)
        if value:
            text = str(value).strip().replace("/", "-")
            match = re.search(r"20\d{2}-\d{1,2}-\d{1,2}", text)
            if match:
                return match.group(0)
    return ""


def _symbols(arguments: dict[str, Any]) -> list[str]:
    value = arguments.get("symbols") or arguments.get("symbol") or arguments.get("code") or arguments.get("codes")
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return [str(item).strip() for item in values if str(item or "").strip()]


def _existing_query(arguments: dict[str, Any]) -> str:
    for key in ("query", "q", "keyword", "text"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def skill_route_for(tool_name: str, arguments: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    """Return ``(skill_operation, capability, skill_arguments)`` if registered."""
    route = THS_PRIMARY_ROUTE.get(tool_name)
    if route is None:
        return None
    # The same Gateway operation is reused by crypto/commodity/US routes.
    # THS is an A-share research source; never send non-A-share symbols to it.
    if tool_name in {"get_market_quotes", "get_market_klines"}:
        exchange = str(arguments.get("exchange") or "").casefold()
        if exchange and exchange not in {"xueqiu", "eastmoney", "tencent", "ths", "a_share", "ashare"}:
            return None
        symbols = _symbols(arguments)
        if not symbols:
            return None
        if any("/" in symbol or not re.search(r"(?:SH|SZ|BJ)?\d{6}$", symbol.upper()) for symbol in symbols):
            return None
    skill_operation, capability = route
    query = _existing_query(arguments)
    dates = _date(arguments)
    symbols = _symbols(arguments)
    target = " ".join(symbols)
    if not query:
        labels = {
            "quote": "最新价 涨跌幅 收盘价",
            "kline": "K线 开盘价 收盘价 最高价 最低价 成交量",
            "market_breadth": "上涨家数 下跌家数 成交额",
            "limit_up_count": "涨停家数",
            "limit_up_pool": "涨停股票 涨停池",
            "limit_up_sectors": "涨停板块 涨停题材",
            "finance": "营业收入 净利润 ROE",
            "entity_lookup": "股票代码 股票简称 所属行业",
            "sector_theme": "行业 板块 主题 涨跌幅",
            "ownership": "前十大股东 持股比例",
            "events": "业绩预告 公告 事件",
            "news": "新闻 资讯",
            "announcement": "公告",
            "macro": "宏观指标",
        }
        query = " ".join(part for part in (target, dates, labels.get(capability, capability)) if part).strip()
    elif dates and dates not in query:
        query = f"{query} {dates}".strip()
    # Official query2data/search contracts accept only these bounded fields.
    if skill_operation.endswith("_search"):
        return skill_operation, capability, {"query": query, "size": int(arguments.get("size", 10) or 10)}
    return skill_operation, capability, {"query": query, "page": int(arguments.get("page", 1) or 1),
                                         "limit": int(arguments.get("limit", 10) or 10),
                                         "is_cache": bool(arguments.get("is_cache", True)),
                                         "expand_index": bool(arguments.get("expand_index", True))}


def is_primary_skill_enabled(settings: Any) -> bool:
    """Return whether production THS-first routing is usable.

    ``market_primary_source`` has a deliberate production default of
    ``ths_skill``.  Pydantic's ``model_fields_set`` only describes how a
    ``Settings`` object was constructed (and is empty for values loaded from
    defaults), so it must never gate runtime routing.  The explicit runtime
    profile flag and credential check are the boundary between the production
    settings returned by ``get_settings()`` and an offline/test double.
    """
    return (
        bool(getattr(settings, "market_source_routing_enabled", False))
        and str(getattr(settings, "market_primary_source", "")).strip().lower() == "ths_skill"
        and bool(str(getattr(settings, "iwencai_api_key", "") or "").strip())
    )


def is_mcp_fallback_enabled(settings: Any) -> bool:
    return bool(getattr(settings, "market_source_fallback_enabled", True)) and str(
        getattr(settings, "market_fallback_source", "mcp")
    ).strip().lower() == "mcp"
