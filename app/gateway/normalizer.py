"""Data Normalizer — 跨市场数据规范化。

核心职责：
- 通过工具注册表（ToolMeta.domain）或启发式规则推断市场域
- 检测 partial / error 状态
- 统一时间戳格式
- 按域提取有意义的指标值
- 将列表型响应展开为多个 NormalizedDatum

输出格式遵循 Mosaic 统一内部数据结构：
{
    "domain": "a_share" | "crypto" | "us_stock" | "unknown",
    "instrument": "SH600519" | "BTCUSDT" | None,
    "metric": "...",
    "value": ...,
    "unit": "CNY" | "USD" | None,
    "timestamp": "...",
    "source": "tencent" | "coinglass" | ... | None,
    "tool": "...",
    "status": "success" | "partial" | "error",
    "partial": false,
    "note": ...
}

新增市场域时：
1. 在 _DOMAIN_HINTS 中添加域名提示映射
2. 在 _CRYPTO_METRICS / _A_SHARE_METRICS 中添加域特征词
3. 如需特殊解析逻辑，添加 extract_for_{domain} 钩子
"""

from typing import Any

from app.gateway.tool_registry import BY_NAME, resolve_tool_by_name
from app.models.market import NormalizedDatum, Status, ToolResult


# ------------------------------------------------------------------ #
# 域名推断规则                                                         #
# ------------------------------------------------------------------ #

#: 启发式域名判断 — 根据关键词匹配域名
_DOMAIN_HINTS: dict[str, list[str]] = {
    "a_share": [
        "ashare", "xueqiu", "tencent", "eastmoney",
        "limit_up", "limit-up", "longhu",
        "concept", "shareholders",
    ],
    "crypto": [
        "market", "coinglass", "hyperliquid",
        "klines", "snapshot", "derivatives",
        "funding_rate", "liquidation", "liqmap",
        "top_position", "user_count", "vaults",
    ],
    "hk_stock": [
        "hk_stock", "hkex", "hang_seng", "hs300",
        "southbound", "northbound", "stock_connect",
    ],
    "us_stock": [
        "yahoo", "finnhub", "alphavantage", "polygon",
        "us_market", "nasdaq", "nyse", "wall_street",
    ],
    "commodities": [
        "gold", "silver", "copper", "crude", "oil",
        "iron_ore", "soybean", "corn", "wheat",
        "aluminum", "zinc", "nickel", "commodity",
    ],
    "macro": [
        "macro", "cpi", "fed", "gdp", "treasury",
        "yield", "inflation", "unemployment",
    ],
}


def infer_domain_from_tool(tool: str) -> str:
    """从工具名推断市场域（精确优先于启发式）。

    优先查找注册表中该工具的 domain 字段；
    如果工具不在注册表中（如新接入的第三方），使用启发式规则。
    """
    try:
        meta = resolve_tool_by_name(tool)
        return meta.domain  # type: ignore[return-value]
    except KeyError:
        pass

    # 启发式回退
    tool_lower = tool.lower()
    for domain, prefixes in _DOMAIN_HINTS.items():
        if domain == "unknown":
            continue
        for prefix in prefixes:
            if prefix in tool_lower:
                return domain
    return "unknown"


# ------------------------------------------------------------------ #
# 时间戳 & 状态探测                                                     #
# ------------------------------------------------------------------ #

_TIMESTAMP_KEYS = {"timestamp", "time", "date", "datetime", "ts"}
_CONTAINER_KEYS = {"candles", "data", "items", "rows", "results", "trades"}
_METADATA_KEYS = {"partial", "status", "note", "source", "source_used"}

# Crypto 领域的时间键变体（K线常用）
_CRYPTO_TIME_KEYS = {"open_time", "close_time", "t", "T", "period", "UnixTime"}


def _deep_flatten_value(obj):
    """从任意深层嵌套中提取原始数值。

    支持的模式：
      {"data": {"indicators": {"PB": {"value": 2.5}}}}
      {"data": [{"name": "PB", "value": 2.5}]}
      {"data": {"row": [{"name": "PB", "value": 2.5}]}}

    递归规则：
      1. 优先找 value/val 键（财务指标最内层数值）
      2. 再找 data/result/info/row 等容器键
      3. 如果所有值中只有一个是非 None 的容器，继续递归
      4. 尝试递归所有子值（dict/list）
    """
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        return obj
    if isinstance(obj, str):
        try:
            return float(obj)
        except ValueError:
            return obj
    if isinstance(obj, dict):
        # 优先找 value/val 键
        for k, v in obj.items():
            if k.lower() in ("value", "val") and v is not None:
                result = _deep_flatten_value(v)
                if isinstance(result, (int, float)):
                    return result
        # 再找 data/result/info/row 等容器键
        for k, v in obj.items():
            if k.lower() in ("data", "result", "info", "row"):
                result = _deep_flatten_value(v)
                if isinstance(result, (int, float)):
                    return result
        # 如果所有值中只有一个是非 None 的容器，继续递归
        values = [v for v in obj.values() if v is not None]
        if len(values) == 1:
            return _deep_flatten_value(values[0])
        # 尝试递归所有子值
        for v in obj.values():
            if v is not None and isinstance(v, (dict, list)):
                result = _deep_flatten_value(v)
                if isinstance(result, (int, float)):
                    return result
    if isinstance(obj, list):
        if len(obj) == 1:
            return _deep_flatten_value(obj[0])
        # 对列表，尝试逐个提取
        for item in obj:
            if item is not None:
                result = _deep_flatten_value(item)
                if isinstance(result, (int, float)):
                    return result
    return obj


def _is_eastmoney_f10_tool(tool_name):
    """判断是否是 Eastmoney F10 工具（可能有嵌套数据）。"""
    f10_patterns = [
        "eastmoney_overview_get",
        "finance_eastmoney",
        "business_eastmoney",
        "concept_eastmoney",
        "shareholders_eastmoney",
        "survey_eastmoney",
        "detail_eastmoney",
        "overview_eastmoney",
    ]
    return any(p in tool_name for p in f10_patterns)


def _extract_f10_list_items(obj, parent_path, result, *, _tool, _domain, _status, _partial, _timestamp, _source):
    """从列表中提取 name/value 对（F10 常用格式）。

    例如：{"data": [{"name": "ROE", "value": 12.5}, {"name": "PE", "value": 15.2}]}
    提取为：data[0].name=ROE, data[0].value=12.5, data[1].name=PE, data[1].value=15.2
    """
    if not isinstance(obj, list):
        return

    for i, item in enumerate(obj[:50]):
        if not isinstance(item, dict):
            continue
        path = f"{parent_path}[{i}]"
        # 提取名称类字段（string value）
        for k, v in item.items():
            if k in ("name", "symbol", "code", "holder_name", "company_name", "industry", "sector"):
                if v is not None:
                    result.append(NormalizedDatum(
                        domain=_domain, metric=f"{path}.{k}", value=v,
                        tool=_tool, status=_status, partial=_partial,
                        timestamp=_timestamp, source=_source,
                    ))
            # 提取数值类字段
            elif k.lower() in ("value", "val", "amount", "count", "pct", "percentage", "shares", "holding_pct"):
                flattened = _deep_flatten_value(v)
                if isinstance(flattened, (int, float)):
                    result.append(NormalizedDatum(
                        domain=_domain, metric=f"{path}.{k}", value=flattened,
                        tool=_tool, status=_status, partial=_partial,
                        timestamp=_timestamp, source=_source,
                    ))
            # 递归处理嵌套的 dict/list
            elif isinstance(v, (dict, list)):
                _extract_metrics(
                    v, parent_path=f"{path}.{k}", result=result,
                    _tool=_tool, _domain=_domain, _status=_status,
                    _partial=_partial, _timestamp=_timestamp, _source=_source,
                )


def find_timestamp(obj: dict[str, Any]) -> str | None:
    """从对象中探测时间戳字段。

    优先级：
      1. 顶层直接时间键
      2. meta/info/header 嵌套
      3. Crypto 专用时间键
    """
    if not isinstance(obj, dict):
        return None

    # 一级直接匹配
    for key in _TIMESTAMP_KEYS:
        if key in obj and obj[key] is not None:
            return str(obj[key])

    # 检查常见嵌套结构中的时间戳
    for container_key in ("meta", "info", "header"):
        if container_key in obj and isinstance(obj[container_key], dict):
            ts = (obj[container_key].get("timestamp")
                  or obj[container_key].get("time"))
            if ts is not None:
                return str(ts)

    # Crypto K 线专用时间键
    for key in _CRYPTO_TIME_KEYS:
        if key in obj and obj[key] is not None:
            return str(obj[key])

    return None


def find_partial(obj: Any) -> bool:
    """检测 partial 标记。"""
    if isinstance(obj, dict):
        return bool(obj.get("partial", False))
    return False


def is_container_key(key: str) -> bool:
    """判断是否为一个数据容器键，其子元素应被单独提取。"""
    return key.lower() in _CONTAINER_KEYS


# ------------------------------------------------------------------ #
# 指标抽取                                                              #
# ------------------------------------------------------------------ #

#: Crypto 高价值指标特征词 — 用于生成有意义的指标名
_CRYPTO_METRICS = {
    "openInterest", "oi", "open_interest", "totalPositionValue",
    "fundingRate", "funding_rate", "premium", "indexPrice",
    "markPrice", "lastPrice", "price",
    "longShortRatio", "long_short_ratio", "topTraderLongShortRatio",
    "longAccountNum", "shortAccountNum",
    "liquidation", "liq", "totalLiqValue",
}

_ASHARE_METRICS = {
    "涨停", "跌停", "涨停家数", "跌停家数", "上涨", "下跌",
    "情绪", "sentiment", "两融余额", "融资余额", "融券余量",
    "成交额", "成交量", "换手率", "市盈率", "市净率",
    # F10 财务指标
    "ROE", "ROA", "ROIC", "营收", "收入", "revenue", "收入总额",
    "净利润", "net_profit", "净利润率", "毛利率", "净利率",
    "资产负债率", "负债率", "debt_ratio", "权益乘数",
    "现金流", "经营现金流", "自由现金流", "fcf",
    "每股收益", "eps", "每股净资产", "bvps",
    "股息率", "dividend_yield", "派息", "分红",
    "净资产", "net_assets", "总资产", "total_assets", "所有者权益",
    "增长率", "营收增长", "利润增长", "growth",
    "PB", "PE", "PS", "EV/EBITDA", "EV", "FCF_yield",
    "分红率", "派息率", "payout_ratio",
    "扣非净利润", "营业总收入", "归母净利润",
    "每股经营现金流", "每股经营现金", "经营活动现金流",
    "股东权益", "总股本", "流通股本", "流通市值", "总市值", "marketcap",
    "总股本", "shares_outstanding", "free_float",
    "roe_yoy", "net_profit_yoy", "revenue_yoy",
    "经营现金流", "经营性现金流", "经营性现金流净额",
}


def _make_summary(value: Any) -> str:
    """将复杂结构转为简短文字描述。"""
    if isinstance(value, list):
        return f"[list:{len(value)} items]"
    if isinstance(value, dict):
        keys = list(value.keys())[:5]
        return f"[object:{','.join(keys)}]"
    return str(value)


def _extract_metrics(
    obj: Any,
    *,
    parent_path: str = "",
    result: list[NormalizedDatum] | None = None,
    _tool: str = "unknown",
    _domain: str = "unknown",
    _status: Status = "success",
    _partial: bool = False,
    _timestamp: str | None = None,
    _source: str | None = None,
) -> list[NormalizedDatum]:
    """递归地从嵌套对象中提取指标。

    对于 Crypto 数据，会额外尝试提取 symbol/instrument 信息。
    """
    if result is None:
        result = []

    def _make_datum(metric: str, value: Any, note: str | None = None) -> NormalizedDatum:
        return NormalizedDatum(
            domain=_domain,
            metric=metric,
            value=value,
            unit=None,
            timestamp=_timestamp,
            source=_source,
            tool=_tool,
            status=_status,
            partial=_partial,
            note=note,
        )

    if isinstance(obj, dict):
        # Crypto: 尝试提取 instrument/symbol
        instrument = None
        for sym_key in ("symbol", "Symbol", "instrument", "coin", "pair"):
            if sym_key in obj and isinstance(obj[sym_key], str):
                instrument = obj[sym_key]
                break

        for key, value in obj.items():
            path = f"{parent_path}.{key}" if parent_path else key

            if value is None:
                continue

            if key in _METADATA_KEYS:
                continue

            if is_container_key(key):
                if isinstance(value, list):
                    for i, item in enumerate(value[:50]):
                        _extract_metrics(
                            item,
                            parent_path=f"{path}[{i}]",
                            result=result,
                            _tool=_tool, _domain=_domain,
                            _status=_status, _partial=_partial,
                            _timestamp=_timestamp, _source=_source,
                        )
                elif isinstance(value, dict):
                    _extract_metrics(
                        value, parent_path=path, result=result,
                        _tool=_tool, _domain=_domain,
                        _status=_status, _partial=_partial,
                        _timestamp=_timestamp, _source=_source,
                    )
            else:
                if isinstance(value, (dict, list)):
                    # F10 专用：列表处理（name/value 对）
                    if _is_eastmoney_f10_tool(_tool) and isinstance(value, list):
                        _extract_f10_list_items(
                            value, parent_path=path, result=result,
                            _tool=_tool, _domain=_domain, _status=_status,
                            _partial=_partial, _timestamp=_timestamp, _source=_source,
                        )
                        continue

                    # For dicts, first check if they contain nested containers
                    # (like {"PB": {"value": 2.5}} containing another dict with "value" key)
                    sub_containers = {}
                    if isinstance(value, dict):
                        sub_containers = {k: v for k, v in value.items()
                                          if isinstance(v, (dict, list))}

                    if _is_eastmoney_f10_tool(_tool) and sub_containers:
                        # Try deep flatten for F10 nested financial data
                        flattened = _deep_flatten_value(value)
                        if isinstance(flattened, (int, float)):
                            result.append(_make_datum(path, flattened))
                        else:
                            # Check if any sub-key is a metric name
                            metric_names = set.union(
                                set(_ASHARE_METRICS), set(_CRYPTO_METRICS)
                            ) if _ASHARE_METRICS or _CRYPTO_METRICS else set()
                            found = False
                            for k, v in sub_containers.items():
                                if k.lower() in ("value", "val"):
                                    dv = _deep_flatten_value(v)
                                    if isinstance(dv, (int, float)):
                                        result.append(_make_datum(f"{path}.{k}", dv))
                                        found = True
                                else:
                                    # 递归子容器提取
                                    _extract_metrics(
                                        v, parent_path=f"{path}.{k}", result=result,
                                        _tool=_tool, _domain=_domain, _status=_status,
                                        _partial=_partial, _timestamp=_timestamp, _source=_source,
                                    )
                                    found = True
                            if not found:
                                result.append(_make_datum(path, _make_summary(value)))
                    elif _is_eastmoney_f10_tool(_tool):
                        flattened = _deep_flatten_value(value)
                        if isinstance(flattened, (int, float)):
                            result.append(_make_datum(path, flattened))
                        else:
                            result.append(_make_datum(path, _make_summary(value)))
                    else:
                        result.append(_make_datum(path, _make_summary(value)))
                else:
                    result.append(_make_datum(path, value))

    elif isinstance(obj, list):
        for i, item in enumerate(obj[:50]):
            idx_path = f"{parent_path}[{i}]" if parent_path else f"item[{i}]"
            val = item if not isinstance(item, (dict, list)) else _make_summary(item)
            result.append(_make_datum(idx_path, val))
    else:
        result.append(_make_datum(parent_path or "response", obj))

    return result


# ------------------------------------------------------------------ #
# 主入口                                                                #
# ------------------------------------------------------------------ #

def normalize_tool_result(
    tool: str,
    arguments: dict[str, Any],
    raw: Any,
    *,
    error: str | None = None,
) -> ToolResult:
    """将 MCP/HTTP 返回的原始响应标准化。

    Args:
        tool: 工具名称（operationId）
        arguments: 调用参数
        raw: 原始响应数据
        error: 如果非空，表示调用失败
    """
    if error:
        return ToolResult(
            tool=tool,
            arguments=arguments,
            raw=None,
            status="error",
            partial=False,
            error=error,
        )

    partial = find_partial(raw)
    status: Status = "partial" if partial else "success"
    timestamp = find_timestamp(raw) if isinstance(raw, dict) else None
    source = None
    domain = infer_domain_from_tool(tool)

    if isinstance(raw, dict):
        source = raw.get("source_used") or raw.get("source")

    normalized = _extract_metrics(
        raw,
        _tool=tool, _domain=domain,
        _status=status, _partial=partial,
        _timestamp=timestamp, _source=source,
    ) if isinstance(raw, (dict, list)) else [
        NormalizedDatum(
            tool=tool,
            domain=domain,
            metric="response",
            value=str(raw),
            status=status,
            partial=partial,
            timestamp=timestamp,
            source=source,
        )
    ]

    return ToolResult(
        tool=tool,
        arguments=arguments,
        raw=raw,
        status=status,
        partial=partial,
        normalized=normalized,
    )


# ------------------------------------------------------------------ #
# 向后兼容别名 — 旧测试使用 _infer_domain 名称                         #
# ------------------------------------------------------------------ #

_infer_domain = infer_domain_from_tool

