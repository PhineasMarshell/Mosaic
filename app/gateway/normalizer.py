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

from app.gateway.tool_registry import resolve_tool_by_name
from app.models.market import NormalizedDatum, Status, ToolResult

# ------------------------------------------------------------------ #
# 域名推断规则                                                         #
# ------------------------------------------------------------------ #

#: 启发式域名判断 — 根据关键词匹配域名
_DOMAIN_HINTS: dict[str, list[str]] = {
    "a_share": [
        "ashare",
        "xueqiu",
        "tencent",
        "eastmoney",
        "limit_up",
        "limit-up",
        "longhu",
        "concept",
        "shareholders",
    ],
    "crypto": [
        "market",
        "coinglass",
        "hyperliquid",
        "klines",
        "snapshot",
        "derivatives",
        "funding_rate",
        "liquidation",
        "liqmap",
        "top_position",
        "user_count",
        "vaults",
    ],
    "hk_stock": [
        "hk_stock",
        "hkex",
        "hang_seng",
        "hs300",
        "southbound",
        "northbound",
        "stock_connect",
    ],
    "us_stock": [
        "yahoo",
        "finnhub",
        "alphavantage",
        "polygon",
        "us_market",
        "nasdaq",
        "nyse",
        "wall_street",
    ],
    "commodities": [
        "gold",
        "silver",
        "copper",
        "crude",
        "oil",
        "iron_ore",
        "soybean",
        "corn",
        "wheat",
        "aluminum",
        "zinc",
        "nickel",
        "commodity",
    ],
    "macro": [
        "macro",
        "cpi",
        "fed",
        "gdp",
        "treasury",
        "yield",
        "inflation",
        "unemployment",
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

#: 单个列表最多逐条展开多少项。超出会被截断 —— 以前是静默截断，
#: 现在会置 partial=True 并写 note（见 _find_truncations）。
_LIST_CAP = 50

#: 阶段 3 ⑤：空返回必须给出**可引用的原因**，不能只是"没有数据"。
_EMPTY_PAYLOAD_NOTE = "上游返回空载荷（200 + 空 body / 空容器）：本次调用没有拿到任何数据"

#: 阶段 3 ⑤：市场级聚合工具（情绪 / 涨停家数 / 涨停题材 / 全市场快讯）的
#: 数据点代表"整个市场在某一天的横截面"。上游没给时间戳时，数据点就失去了
#: 日期锚点——报告于是可能拿一段旧数据声称"今日大盘上涨"（验收要求禁止）。
#: 因此这里显式标注，让 Reasoning / Critic 都不能无视它。
_MARKET_LEVEL_NO_TIMESTAMP_NOTE = "上游未提供时间戳：无法确认该数据是否为当日（不得据此断言「今日」）"

#: 升序时间序列：截断时必须保留**末尾**（最新），否则会把几周前的数据
#: 当成当前值。实测 90 天日线只保留前 50 根时，"最新收盘"比真实值低 21%。
_SERIES_KEYS = {"candles", "candle", "hqdata", "klines", "kline", "series"}
_SERIES_TOOL_HINTS = (
    "klines",
    "window",
    "derivatives_history",
    "user_count",
    "sentiment",
    "abnormal_reasons",
)
# 刻意**不含** timeline / trades（新→旧的信息流，头部才是最新）、
# limit_up_pool / overview / top_position（排行榜或名单，头部最相关）。


def _is_ordered_series(key: str | None, tool: str | None) -> bool:
    """该列表是否按时间升序排列（→ 截断时保留末尾）。"""
    if key and key.lower() in _SERIES_KEYS:
        return True
    t = (tool or "").lower()
    return any(h in t for h in _SERIES_TOOL_HINTS)


def _slice_list(key: str | None, value: list, tool: str | None) -> tuple[list, int]:
    """按 _LIST_CAP 截断列表：时间序列留末尾，其余留开头。

    返回 ``(保留的切片, 该切片在原始列表中的起始下标)``。带上起始下标是为了
    让 metric 路径（``candles[89].c``）继续指向原始 payload 里的真实位置，
    而不是把最新的 50 根重新编号成 0-49。
    """
    if len(value) <= _LIST_CAP:
        return value, 0
    if _is_ordered_series(key, tool):
        return value[-_LIST_CAP:], len(value) - _LIST_CAP
    return value[:_LIST_CAP], 0


def _find_truncations(obj, tool, parent_path: str = "", depth: int = 0) -> list[str]:
    """找出会被 _slice_list 截断的列表，返回人类可读的说明。

    只统计提取器**真正会逐条展开**的列表（容器键下的列表、F10 的 name/value
    列表、顶层列表）。被 _make_summary 压成字符串的列表不算截断 —— 那是另一个
    问题（嵌套 payload 丢失），口径不同，混在一起会让 note 失真。
    """
    notes: list[str] = []
    if depth > 6:
        return notes

    if isinstance(obj, list):
        if len(obj) > _LIST_CAP:
            kept = "newest" if _is_ordered_series(parent_path.rsplit(".", 1)[-1], tool) else "first"
            notes.append(f"{parent_path or 'root'}: truncated {len(obj)}->{_LIST_CAP} items, kept {kept}")
    elif isinstance(obj, dict):
        f10 = _is_eastmoney_f10_tool(tool)
        for k, v in obj.items():
            path = f"{parent_path}.{k}" if parent_path else k
            if isinstance(v, list) and (is_container_key(k) or f10):
                if len(v) > _LIST_CAP:
                    kept = "newest" if _is_ordered_series(k, tool) else "first"
                    notes.append(f"{path}: truncated {len(v)}->{_LIST_CAP} items, kept {kept}")
            elif isinstance(v, dict):
                notes.extend(_find_truncations(v, tool, path, depth + 1))
    return notes


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
        "get_company_overview",
        "get_company_finance",
        "get_company_business",
        "get_company_concepts",
        "get_company_shareholders",
        "get_company_survey",
        "get_company_detail",
    ]
    return any(p in tool_name for p in f10_patterns)


def _dict_has_value_sibling(obj: dict) -> bool:
    """T2：dict 中是否存在非 None 的 value/val 键。

    用于决定同级 ``unit`` 是合并进 value datum，还是作为一条独立指标保留。
    """
    return any(k.lower() in ("value", "val") and obj[k] is not None for k in obj)


def _extract_f10_list_items(obj, parent_path, result, *, _tool, _domain, _status, _partial, _timestamp, _source):
    """从列表中提取 name/value 对（F10 常用格式）。

    例如：{"data": [{"name": "ROE", "value": 12.5}, {"name": "PE", "value": 15.2}]}
    提取为：data[0].name=ROE, data[0].value=12.5, data[1].name=PE, data[1].value=15.2
    """
    if not isinstance(obj, list):
        return

    _items, _start = _slice_list(parent_path.rsplit(".", 1)[-1] if parent_path else None, obj, _tool)
    for i, item in enumerate(_items, _start):
        if not isinstance(item, dict):
            continue
        path = f"{parent_path}[{i}]"
        # 提取名称类字段（string value）
        for k, v in item.items():
            if k in ("name", "symbol", "code", "holder_name", "company_name", "industry", "sector"):
                if v is not None:
                    result.append(
                        NormalizedDatum(
                            domain=_domain,
                            metric=f"{path}.{k}",
                            value=v,
                            tool=_tool,
                            status=_status,
                            partial=_partial,
                            timestamp=_timestamp,
                            source=_source,
                        )
                    )
            # 提取数值类字段
            elif k.lower() in ("value", "val", "amount", "count", "pct", "percentage", "shares", "holding_pct"):
                flattened = _deep_flatten_value(v)
                if isinstance(flattened, (int, float)):
                    result.append(
                        NormalizedDatum(
                            domain=_domain,
                            metric=f"{path}.{k}",
                            value=flattened,
                            tool=_tool,
                            status=_status,
                            partial=_partial,
                            timestamp=_timestamp,
                            source=_source,
                        )
                    )
            # 递归处理嵌套的 dict/list
            elif isinstance(v, (dict, list)):
                _extract_metrics(
                    v,
                    parent_path=f"{path}.{k}",
                    result=result,
                    _tool=_tool,
                    _domain=_domain,
                    _status=_status,
                    _partial=_partial,
                    _timestamp=_timestamp,
                    _source=_source,
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
            ts = obj[container_key].get("timestamp") or obj[container_key].get("time")
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
    "openInterest",
    "oi",
    "open_interest",
    "totalPositionValue",
    "fundingRate",
    "funding_rate",
    "premium",
    "indexPrice",
    "markPrice",
    "lastPrice",
    "price",
    "longShortRatio",
    "long_short_ratio",
    "topTraderLongShortRatio",
    "longAccountNum",
    "shortAccountNum",
    "liquidation",
    "liq",
    "totalLiqValue",
}

_ASHARE_METRICS = {
    "涨停",
    "跌停",
    "涨停家数",
    "跌停家数",
    "上涨",
    "下跌",
    "情绪",
    "sentiment",
    "两融余额",
    "融资余额",
    "融券余量",
    "成交额",
    "成交量",
    "换手率",
    "市盈率",
    "市净率",
    # F10 财务指标
    "ROE",
    "ROA",
    "ROIC",
    "营收",
    "收入",
    "revenue",
    "收入总额",
    "净利润",
    "net_profit",
    "净利润率",
    "毛利率",
    "净利率",
    "资产负债率",
    "负债率",
    "debt_ratio",
    "权益乘数",
    "现金流",
    "经营现金流",
    "自由现金流",
    "fcf",
    "每股收益",
    "eps",
    "每股净资产",
    "bvps",
    "股息率",
    "dividend_yield",
    "派息",
    "分红",
    "净资产",
    "net_assets",
    "总资产",
    "total_assets",
    "所有者权益",
    "增长率",
    "营收增长",
    "利润增长",
    "growth",
    "PB",
    "PE",
    "PS",
    "EV/EBITDA",
    "EV",
    "FCF_yield",
    "分红率",
    "派息率",
    "payout_ratio",
    "扣非净利润",
    "营业总收入",
    "归母净利润",
    "每股经营现金流",
    "每股经营现金",
    "经营活动现金流",
    "股东权益",
    "总股本",
    "流通股本",
    "流通市值",
    "总市值",
    "marketcap",
    "shares_outstanding",
    "free_float",
    "roe_yoy",
    "net_profit_yoy",
    "revenue_yoy",
    "经营性现金流",
    "经营性现金流净额",
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

    def _make_datum(metric: str, value: Any, note: str | None = None, unit: str | None = None) -> NormalizedDatum:
        return NormalizedDatum(
            domain=_domain,
            metric=metric,
            value=value,
            unit=unit,
            timestamp=_timestamp,
            source=_source,
            tool=_tool,
            status=_status,
            partial=_partial,
            note=note,
        )

    if isinstance(obj, dict):
        # T2：本 dict 是否含 value/val 兄弟；决定同级 unit 是否合并进 value datum。
        _has_value_pair = _dict_has_value_sibling(obj)
        for key, value in obj.items():
            path = f"{parent_path}.{key}" if parent_path else key

            if value is None:
                continue

            if key in _METADATA_KEYS:
                continue

            # T2：value/val 的同级 unit 合并进该 value datum（在下方附加），
            # 不再作为一条独立的字符串指标。仅当确实存在 value 兄弟且 unit 为字符串。
            if key.lower() == "unit" and _has_value_pair and isinstance(value, str):
                continue

            if is_container_key(key):
                if isinstance(value, list):
                    _items, _start = _slice_list(key, value, _tool)
                    for i, item in enumerate(_items, _start):
                        _extract_metrics(
                            item,
                            parent_path=f"{path}[{i}]",
                            result=result,
                            _tool=_tool,
                            _domain=_domain,
                            _status=_status,
                            _partial=_partial,
                            _timestamp=_timestamp,
                            _source=_source,
                        )
                elif isinstance(value, dict):
                    _extract_metrics(
                        value,
                        parent_path=path,
                        result=result,
                        _tool=_tool,
                        _domain=_domain,
                        _status=_status,
                        _partial=_partial,
                        _timestamp=_timestamp,
                        _source=_source,
                    )
            else:
                if isinstance(value, (dict, list)):
                    # F10 专用：列表处理（name/value 对）
                    if _is_eastmoney_f10_tool(_tool) and isinstance(value, list):
                        _extract_f10_list_items(
                            value,
                            parent_path=path,
                            result=result,
                            _tool=_tool,
                            _domain=_domain,
                            _status=_status,
                            _partial=_partial,
                            _timestamp=_timestamp,
                            _source=_source,
                        )
                        continue

                    # For dicts, first check if they contain nested containers
                    # (like {"PB": {"value": 2.5}} containing another dict with "value" key)
                    sub_containers = {}
                    if isinstance(value, dict):
                        sub_containers = {k: v for k, v in value.items() if isinstance(v, (dict, list))}

                    if _is_eastmoney_f10_tool(_tool) and sub_containers:
                        # T2：绝不能对整块 value 做 _deep_flatten_value——该函数遇到
                        # 多个兄弟指标（{"ROE":{...},"PE":{...}}）只返回第一个数值，
                        # 其余指标（PE/PB…）永久丢失，且存活 datum 的 metric 是父路径
                        # （如 data.indicators），无法辨识指标名。改为逐子键递归，使
                        # metric 保持 data.indicators.ROE.value 这种可读形式；单个子
                        # 容器也递归进去以保留其名称与同级 unit。
                        _extract_metrics(
                            value,
                            parent_path=path,
                            result=result,
                            _tool=_tool,
                            _domain=_domain,
                            _status=_status,
                            _partial=_partial,
                            _timestamp=_timestamp,
                            _source=_source,
                        )
                    elif _is_eastmoney_f10_tool(_tool):
                        # T2：没有子容器时也递归展开（而非 flatten 后挂在父路径上），
                        # 让内层 {"value":..,"unit":..} 走 value/unit 合并逻辑，保留单位。
                        _extract_metrics(
                            value,
                            parent_path=path,
                            result=result,
                            _tool=_tool,
                            _domain=_domain,
                            _status=_status,
                            _partial=_partial,
                            _timestamp=_timestamp,
                            _source=_source,
                        )
                    else:
                        # 通用路径：遇到嵌套 dict/list → 递归展开，而不是压成字符串。
                        # 审计发现的 CRITICAL 问题（snapshot 的 ticker.last 变成 "[object:base_volume,...]"）
                        # 就是这里引起的：_make_summary 把整个嵌套对象丢掉了。
                        if isinstance(value, dict):
                            _extract_metrics(
                                value,
                                parent_path=path,
                                result=result,
                                _tool=_tool,
                                _domain=_domain,
                                _status=_status,
                                _partial=_partial,
                                _timestamp=_timestamp,
                                _source=_source,
                            )
                        elif isinstance(value, list):
                            for i, item in enumerate(value):
                                item_path = f"{path}[{i}]"
                                if isinstance(item, dict):
                                    _extract_metrics(
                                        item,
                                        parent_path=item_path,
                                        result=result,
                                        _tool=_tool,
                                        _domain=_domain,
                                        _status=_status,
                                        _partial=_partial,
                                        _timestamp=_timestamp,
                                        _source=_source,
                                    )
                                else:
                                    result.append(_make_datum(item_path, item))
                        else:
                            result.append(_make_datum(path, value))
                else:
                    # T2：value/val 标量若有同级 unit，把 unit 合并进该 datum。
                    _unit = None
                    if key.lower() in ("value", "val"):
                        _candidate_unit = obj.get("unit")
                        _unit = _candidate_unit if isinstance(_candidate_unit, str) else None
                    result.append(_make_datum(path, value, unit=_unit))

    elif isinstance(obj, list):
        _items, _start = _slice_list(parent_path.rsplit(".", 1)[-1] if parent_path else None, obj, _tool)
        for i, item in enumerate(_items, _start):
            idx_path = f"{parent_path}[{i}]" if parent_path else f"item[{i}]"
            # 列表元素是 dict/list 时递归展开，不是总结
            if isinstance(item, dict):
                _extract_metrics(
                    item,
                    parent_path=idx_path,
                    result=result,
                    _tool=_tool,
                    _domain=_domain,
                    _status=_status,
                    _partial=_partial,
                    _timestamp=_timestamp,
                    _source=_source,
                )
            elif isinstance(item, list):
                # 嵌套列表也递归
                for j, sub in enumerate(item):
                    sub_path = f"{idx_path}[{j}]"
                    if isinstance(sub, dict):
                        _extract_metrics(
                            sub,
                            parent_path=sub_path,
                            result=result,
                            _tool=_tool,
                            _domain=_domain,
                            _status=_status,
                            _partial=_partial,
                            _timestamp=_timestamp,
                            _source=_source,
                        )
                    else:
                        result.append(_make_datum(sub_path, sub))
            else:
                result.append(_make_datum(idx_path, item))
    else:
        result.append(_make_datum(parent_path or "response", obj))

    return result


def _market_level_datum_note(tool: str, timestamp: str | None) -> str | None:
    """市场级工具缺时间戳时要追加到每条 datum 的 note（阶段 3 ⑤）。"""
    if timestamp:
        return None
    try:
        meta = resolve_tool_by_name(tool)
    except KeyError:
        return None
    return _MARKET_LEVEL_NO_TIMESTAMP_NOTE if getattr(meta, "market_level", False) else None


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

    # T1：空载荷 / 裸标量都不是"数据"，必须判失败，而不是被归一化成成功证据。
    #   - None / {} / []：上游用「200 + 空 body」表示查不到数据。历史上这里只把
    #     None 当失败（另有两条路径：HTTP 401/403 在 last_error 为 None 时 return、
    #     MCP 工具级失败从不检查 result.is_error），空容器则走正常路径产出 0 条 datum。
    #   - str/int/bool/float 标量：通常是上游报错/繁忙文本（如 "Server busy"），
    #     以前在 else 分支被 str() 包成 metric="response" 的"证据"，Evidence Gate
    #     于是报 has_evidence=True，Reasoning LLM 在零数据点下撰写报告。
    if raw is None or raw == [] or (isinstance(raw, dict) and not raw):
        return ToolResult(
            tool=tool,
            arguments=arguments,
            raw=None,
            status="error",
            partial=False,
            normalized=[],
            note=_EMPTY_PAYLOAD_NOTE,
            error="Gateway returned no data (empty response)",
        )

    if not isinstance(raw, (dict, list)):
        snippet = str(raw)[:200]
        return ToolResult(
            tool=tool,
            arguments=arguments,
            raw=raw,
            status="error",
            partial=False,
            normalized=[],
            error=f"Gateway returned a non-data scalar response: {snippet}",
        )

    partial = find_partial(raw)

    # 截断必须说出来：以前 [:50] 静默丢掉记录，status 还是 success，
    # 于是"今天怎么样"会拿六周前的数据作答。
    truncations = _find_truncations(raw, tool)
    if truncations:
        partial = True

    # gateway 契约：partial=true 时另有 note 说明原因。_METADATA_KEYS 会把它
    # 从数据点里滤掉，所以在这里捞出来挂到 ToolResult 上。
    upstream_note = raw.get("note") if isinstance(raw, dict) else None
    note = "; ".join(str(n) for n in ([upstream_note] if upstream_note else []) + truncations) or None

    status: Status = "partial" if partial else "success"
    timestamp = find_timestamp(raw) if isinstance(raw, dict) else None
    source = None
    domain = infer_domain_from_tool(tool)

    if isinstance(raw, dict):
        source = raw.get("source_used") or raw.get("source")

    normalized = (
        _extract_metrics(
            raw,
            _tool=tool,
            _domain=domain,
            _status=status,
            _partial=partial,
            _timestamp=timestamp,
            _source=source,
        )
        if isinstance(raw, (dict, list))
        else [
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
    )

    # 阶段 3 ⑤：市场级工具的数据点必须能说清"这是哪一天的市场"。
    # 上游没给时间戳时逐条标注，避免报告拿无日期的旧数据声称"今日"。
    market_note = _market_level_datum_note(tool, timestamp)
    if market_note:
        for datum in normalized:
            datum.note = "; ".join(part for part in (datum.note, market_note) if part)

    # T1 兜底不变式：任何分支只要产出 0 条 datum，就绝不能报 success/partial，
    # 防止以后新增的分支再把"无数据"当成证据（例如整块 dict 只含元数据键）。
    if not normalized:
        return ToolResult(
            tool=tool,
            arguments=arguments,
            raw=raw,
            status="error",
            partial=False,
            note=note or _EMPTY_PAYLOAD_NOTE,
            normalized=[],
            error="Gateway returned no extractable data (normalization produced zero items)",
        )

    return ToolResult(
        tool=tool,
        arguments=arguments,
        raw=raw,
        status=status,
        partial=partial,
        note=note,
        normalized=normalized,
    )


# ------------------------------------------------------------------ #
# 向后兼容别名 — 旧测试使用 _infer_domain 名称                         #
# ------------------------------------------------------------------ #

_infer_domain = infer_domain_from_tool
