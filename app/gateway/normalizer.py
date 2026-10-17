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

import re
from datetime import UTC, datetime
from typing import Any

from app.gateway.arguments import canonicalize_tool_arguments
from app.gateway.mcp_semantics import annotate_mcp_payload
from app.gateway.tool_registry import get_semantic_contract, resolve_tool_by_name
from app.models.market import NormalizedDatum, Status, ToolResult
from app.research.trading_calendar import normalize_date

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
_CONTAINER_KEYS = {"candles", "data", "datas", "items", "rows", "results", "trades"}
_METADATA_KEYS = {"partial", "status", "completeness", "note", "source", "source_used", "provider", "metadata", "meta", "transport"}

#: 单个列表最多逐条展开多少项。超出会被截断 —— 以前是静默截断，
#: 现在会置 partial=True 并写 note（见 _find_truncations）。
_LIST_CAP = 50

#: 阶段 3 ⑤：空返回必须给出**可引用的原因**，不能只是"没有数据"。
_EMPTY_PAYLOAD_NOTE = "上游返回空载荷（200 + 空 body / 空容器）：本次调用没有拿到任何数据"


def _reason_codes_for_error(error: str | None) -> list[str]:
    """Map transport errors to stable audit codes without exposing payloads."""
    text = str(error or "").casefold()
    codes: list[str] = []
    if "budget exhausted" in text or "deadline" in text:
        codes.append("deadline_exhausted")
    if "timeout" in text or "timed out" in text or "context deadline exceeded" in text:
        codes.append("gateway_timeout")
    if "authentication" in text or "permission" in text or "401" in text or "403" in text:
        codes.append("gateway_auth_error")
    if "429" in text or "rate limit" in text:
        codes.append("gateway_rate_limited")
    if "upstream" in text or "502" in text or "503" in text or "504" in text:
        codes.append("gateway_upstream_error")
    # Cloudflare's 502 body can explicitly say that the origin response was
    # incomplete without exposing an HTTP status to the MCP caller. Keep this
    # distinct from a generic transport failure so route/finalize telemetry can
    # explain the real upstream condition while still producing zero evidence.
    if any(token in text for token in ("cloudflare", "origin", "incomplete response", "incomplete")):
        codes.append("gateway_upstream_incomplete")
    # 200 信封里的业务级拒绝（例如 iwencai_industry 回 gateway_business_error）与
    # 传输错误一样是"本次拿不到数据"，必须保留成可回退的独立 code，不能塌成
    # 泛化的 gateway_error——否则 controlled fallback 无法识别它可回退。
    if any(token in text for token in ("gateway_business_error", "business_error", "business error", "业务错误", "业务异常")):
        codes.append("gateway_business_error")
    if not codes:
        codes.append("gateway_error")
    return codes


def _stable_reason_codes(tool: str, codes: list[str]) -> list[str]:
    """Add the optional-evidence marker without changing data semantics."""
    ordered = list(dict.fromkeys(codes))
    if tool == "get_stock_abnormal_reasons" and "optional_evidence_unavailable" not in ordered:
        ordered.append("optional_evidence_unavailable")
    return ordered

#: 阶段 3 ⑤：市场级聚合工具（情绪 / 涨停家数 / 涨停题材 / 全市场快讯）的
#: 数据点代表"整个市场在某一天的横截面"。上游没给时间戳时，数据点就失去了
#: 日期锚点——报告于是可能拿一段旧数据声称"今日大盘上涨"（验收要求禁止）。
#: 因此这里显式标注，让 Reasoning / Critic 都不能无视它。
_MARKET_LEVEL_NO_TIMESTAMP_NOTE = "上游未提供时间戳：无法确认该数据是否为当日（不得据此断言「今日」）"

# ------------------------------------------------------------------ #
# Provenance：source / authority（provider，不是 transport）            #
# ------------------------------------------------------------------ #

#: 明确的上游 provider 标识。authority 只能来自响应体里声明的 provider
#: （source_used / source），并且必须命中这个白名单；否则保持 None。
#: 传输层名称（"mcp" / "http_api" / "ths_skill"）与技能包装名（"同花顺问财"）
#: 都不是 provider —— 拿它们充当 authority 会把"谁说的"变成"怎么拿到的"。
_PROVIDER_SOURCE_PATTERN = re.compile(
    r"(?:"
    r"tencent|qq[_-]?quote"
    r"|ashare[_-]?master|a[_-]?share[_-]?master"
    r"|eastmoney|东方财富"
    r"|xueqiu|xueqiao|雪球"
    r"|sina|新浪"
    r"|cninfo|巨潮"
    r"|hkex|港交所"
    r"|coinglass|binance|hyperliquid|okx|coinbase|bybit"
    r"|exchange|交易所"
    r")",
    re.IGNORECASE,
)


def provider_authority(value: Any) -> str | None:
    """Return the provider name declared by the upstream payload, or None.

    Only explicit provider sources qualify. A transport name or a skill/transport
    label must never be promoted to authority.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text if _PROVIDER_SOURCE_PATTERN.search(text) else None


#: 顶层信封元数据键。它们描述"这一批响应"（条数 / 总数 / 分页 / 明细标记），
#: 本身不是某条业务事实，因此不参与 provenance 的逐条校验。注意：只在载荷
#: **根**层生效，容器内部的同名字段（如 total_share 这类真实业务值）照常要求
#: provenance，绝不放过。
_STRUCTURAL_METADATA_KEYS = frozenset(
    {
        "count",
        "total",
        "total_count",
        "row_count",
        "code_count",
        "item_count",
        "record_count",
        "page",
        "page_size",
        "page_num",
        "page_no",
        "current_page",
        "per_page",
        "limit",
        "offset",
        "has_more",
        "has_next",
        "next_page",
        "more",
        "success",
        "code",
        "error",
        "message",
        "msg",
        "is_detail",
    }
)
_INDEXED_PATH_RE = re.compile(r"^(.*)\[(\d+)\]$")


def is_structural_metadata_datum(datum: Any) -> bool:
    """True only for envelope-level count/total/paging metadata at payload root."""
    # ``count`` is ambiguous: at the payload root it may be the envelope row
    # count, but ``{"count": 59, "date": ...}`` is also a legitimate
    # limit-up-count business response.  The extractor therefore records the
    # structural decision explicitly; never infer it from the metric name
    # after the fact.
    return bool(getattr(datum, "structural_metadata", False))


#: 市场级聚合能力：这些能力的数据点代表"某一天的市场横截面"，因此必须有日期，
#: 并且要按各自的必需字段判断可回退性（见 controlled_fallback）。
_MARKET_CAPABILITIES = frozenset(
    {
        "quote",
        "kline",
        "sentiment",
        "market_breadth",
        "limit_up",
        "limit_up_count",
        "limit_up_pool",
        "limit_up_sectors",
        "sector_theme",
        "abnormal_reasons",
    }
)


def is_market_capability(capability: str) -> bool:
    return str(capability or "") in _MARKET_CAPABILITIES


def _container_key(path: str) -> str:
    """Return the container key a datum path lives in ("" for payload root)."""
    head = path.split(".", 1)[0] if path else ""
    match = _INDEXED_PATH_RE.match(head)
    return match.group(1).casefold() if match else head.casefold()


#: 本次调用中被截断的列表容器路径。`_find_truncations` 生成、`_make_datum` 读取，
#: 使 datum.partial 只描述"这一条自己是否被截断"，而不再把整批 partial 复制给
#: 每一条完整 datum。每次 normalize_tool_result 开始时重设，避免跨调用串味。
_TRUNCATED_CONTAINER_PATHS: frozenset[str] = frozenset()


def _truncation_paths(truncations: list[str]) -> frozenset[str]:
    """Extract the容器 path from `"<path>: truncated N->M items, kept X"` notes."""
    paths: set[str] = set()
    for note in truncations:
        path = str(note).split(": truncated", 1)[0].strip()
        if path and path != "root":
            paths.add(path)
    return frozenset(paths)


def _container_was_truncated(path: str) -> bool:
    if not path:
        return False
    return path in _TRUNCATED_CONTAINER_PATHS


#: 日期键的传播优先级。容器条目自带日期优先；否则用载荷根 / 所属容器声明的日期。
_DATUM_DATE_KEYS = ("as_of_date", "trade_date", "trading_date", "日期", "交易日期", "交易日", "date", "time", "datetime", "timestamp", "t")


def _first_date(obj: Any, keys: tuple[str, ...]) -> str | None:
    if not isinstance(obj, dict):
        return None
    for key in keys:
        value = obj.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _date_index(raw: Any) -> dict[str, str]:
    """Map container key -> declared date string, plus "" for the payload root.

    items/data/rows 里的日期要能传播到"对应"的数据点：条目自己的日期最优先，
    其次是它所属容器声明的日期，最后才是载荷根声明的日期。
    """
    index: dict[str, str] = {}
    if not isinstance(raw, dict):
        return index
    root = _first_date(raw, _DATUM_DATE_KEYS)
    if root:
        index[""] = root
    for key, value in raw.items():
        if is_container_key(key) and isinstance(value, dict):
            declared = _first_date(value, _DATUM_DATE_KEYS)
            if declared:
                index[str(key).casefold()] = declared
    return index


def _declared_date_for(path: str, index: dict[str, str]) -> str | None:
    if not index:
        return None
    key = _container_key(path)
    if key and key in index:
        return index[key]
    return index.get("")


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
    _date_index: dict[str, str] | None = None,
) -> list[NormalizedDatum]:
    """递归地从嵌套对象中提取指标。

    对于 Crypto 数据，会额外尝试提取 symbol/instrument 信息。
    """
    if result is None:
        result = []

    declared_dates = _date_index or {}
    # Response-set partial does not taint complete independent rows. Once inside
    # a business row, its own incomplete marker must survive recursive children.
    is_envelope = isinstance(obj, dict) and any(
        is_container_key(key) and isinstance(value, (dict, list)) for key, value in obj.items()
    ) and not re.search(r"\[\d+\]", parent_path)
    declared_partial = isinstance(obj, dict) and (
        obj.get("partial") is True
        or str(obj.get("status", "")).casefold() in {"partial", "incomplete", "error"}
        or str(obj.get("completeness", "")).casefold() in {"partial", "incomplete", "truncated"}
    )
    _partial = bool((parent_path and _partial) or (declared_partial and not is_envelope))
    _status = "partial" if _partial else "success"

    def _make_datum(metric: str, value: Any, note: str | None = None, unit: str | None = None) -> NormalizedDatum:
        # 信封 partial 与"这条 datum 自己是否完整"是两件事：只有确实被截断的
        # 容器（_TRUNCATED_CONTAINER_PATHS）才把 partial 记到 datum 上，整批
        # partial 不再强加给每一条完整 datum。
        datum_partial = bool(_partial or _container_was_truncated(parent_path))
        timestamp = _timestamp
        if not timestamp:
            timestamp = _declared_date_for(parent_path, declared_dates)
        return NormalizedDatum(
            domain=_domain,
            metric=metric,
            value=value,
            unit=unit,
            timestamp=timestamp,
            source=_source,
            structural_metadata=bool(
                not parent_path
                and metric.casefold() in _STRUCTURAL_METADATA_KEYS
                and isinstance(obj, dict)
                and (
                    metric.casefold() != "count"
                    or _tool == "list_limit_up_sectors"
                    or any(
                        is_container_key(key) and isinstance(item, (dict, list)) and bool(item)
                        for key, item in obj.items()
                    )
                )
            ),
            tool=_tool,
            status="partial" if datum_partial else "success",
            partial=datum_partial,
            note=note,
        )

    if isinstance(obj, dict):
        # T2：本 dict 是否含 value/val 兄弟；决定同级 unit 是否合并进 value datum。
        _has_value_pair = _dict_has_value_sibling(obj)
        for key, value in obj.items():
            path = f"{parent_path}.{key}" if parent_path else key

            if value is None:
                continue

            if key in _METADATA_KEYS and not (key == "status" and parent_path and value in ("涨停", "跌停")):
                continue

            # T2：value/val 的同级 unit 合并进该 value datum（在下方附加），
            # 不再作为一条独立的字符串指标。仅当确实存在 value 兄弟且 unit 为字符串。
            if key.lower() == "unit" and _has_value_pair and isinstance(value, str):
                continue

            if is_container_key(key):
                if isinstance(value, list):
                    _items, _start = _slice_list(key, value, _tool)
                    for i, item in enumerate(_items, _start):
                        item_timestamp = _timestamp
                        if isinstance(item, dict):
                            for date_key in ("date", "trade_date", "trading_date", "as_of_date", "日期", "交易日期", "交易日", "time", "datetime", "timestamp", "t"):
                                if item.get(date_key) is not None:
                                    item_timestamp = str(item[date_key])
                                    break
                        _extract_metrics(
                            item,
                            parent_path=f"{path}[{i}]",
                            result=result,
                            _tool=_tool,
                            _domain=_domain,
                            _status=_status,
                            _partial=_partial,
                            _timestamp=item_timestamp,
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
                item_timestamp = _timestamp
                for date_key in ("date", "trade_date", "trading_date", "as_of_date", "日期", "交易日期", "交易日", "time", "datetime", "timestamp", "t"):
                    if item.get(date_key) is not None:
                        item_timestamp = str(item[date_key])
                        break
                _extract_metrics(
                    item,
                    parent_path=idx_path,
                    result=result,
                    _tool=_tool,
                    _domain=_domain,
                    _status=_status,
                    _partial=_partial,
                    _timestamp=item_timestamp,
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
    tool_key: str | None = None,
    reason_codes: list[str] | None = None,
    declared_provider: str | None = None,
    requested_as_of_date: str | None = None,
) -> ToolResult:
    """将 MCP/HTTP 返回的原始响应标准化。

    Args:
        tool: 工具名称（operationId）
        arguments: 调用参数
        raw: 原始响应数据
        error: 如果非空，表示调用失败
        requested_as_of_date: 独立传入的研究目标日期（planned_as_of_date）。
            它不属于 wire 参数；只有契约声明且日期实际随请求发送时才可用，
            否则只会得到 ``mcp_request_date_ignored`` 诊断。
    """
    try:
        arguments = canonicalize_tool_arguments(tool, arguments)
    except Exception:
        # Boundary validation is performed by MCP/HTTP clients.  The
        # normalizer remains tolerant for legacy/internal callers so an
        # upstream error can still be represented with its original args.
        arguments = dict(arguments or {})
    if tool_key is None:
        try:
            tool_key = resolve_tool_by_name(tool).key
        except KeyError:
            tool_key = None

    if error:
        return ToolResult(
            tool=tool,
            operation_id=tool,
            tool_key=tool_key,
            arguments=arguments,
            raw=None,
            status="error",
            partial=False,
            error=error,
            reason_codes=_stable_reason_codes(tool, list(dict.fromkeys(reason_codes or _reason_codes_for_error(error)))),
        )

    # T1：空载荷 / 裸标量都不是"数据"，必须判失败，而不是被归一化成成功证据。
    #   - None / {} / []：上游用「200 + 空 body」表示查不到数据。历史上这里只把
    #     None 当失败（另有两条路径：HTTP 401/403 在 last_error 为 None 时 return、
    #     MCP 工具级失败从不检查 result.is_error），空容器则走正常路径产出 0 条 datum。
    #   - str/int/bool/float 标量：通常是上游报错/繁忙文本（如 "Server busy"），
    #     以前在 else 分支被 str() 包成 metric="response" 的"证据"，Evidence Gate
    #     于是报 has_evidence=True，Reasoning LLM 在零数据点下撰写报告。
    empty_collection = (
        tool in {"get_market_quotes", "get_stock_abnormal_reasons"}
        and isinstance(raw, dict)
        and any(raw.get(key) == [] for key in ("items", "data", "rows") if key in raw)
        and not any(raw.get(key) for key in ("items", "data", "rows"))
    )
    if raw is None or raw == "" or raw == [] or (isinstance(raw, dict) and not raw) or empty_collection:
        return ToolResult(
            tool=tool,
            operation_id=tool,
            tool_key=tool_key,
            arguments=arguments,
            raw=None,
            status="error",
            partial=False,
            normalized=[],
            note=_EMPTY_PAYLOAD_NOTE,
            error="Gateway returned no data (empty response)",
            reason_codes=_stable_reason_codes(tool, ["empty_gateway_response"]),
        )

    # Some HTTP/MCP adapters return a 200 envelope containing the upstream
    # error instead of using an HTTP error status. Never flatten that envelope
    # into ``code``/``message`` evidence; classify it at the transport boundary
    # with the same stable reasons as an exception path.
    if isinstance(raw, dict):
        raw_code = str(raw.get("code") or raw.get("error_code") or "").casefold()
        raw_message = str(raw.get("message") or raw.get("error") or "").casefold()
        if raw_code in {"gateway_error", "upstream_error", "origin_error", "bad_gateway"} or any(
            token in raw_message for token in ("cloudflare", "origin", "incomplete response", "upstream")
        ):
            text = str(raw.get("message") or raw.get("error") or raw_code or "gateway upstream error")
            return ToolResult(
                tool=tool,
                operation_id=tool,
                tool_key=tool_key,
                arguments=arguments,
                raw=None,
                status="error",
                partial=False,
                normalized=[],
                error="Gateway upstream returned an error envelope",
                note=text[:200],
                reason_codes=_stable_reason_codes(tool, _reason_codes_for_error(text)),
            )

    if not isinstance(raw, (dict, list)):
        snippet = str(raw)[:200]
        return ToolResult(
            tool=tool,
            operation_id=tool,
            tool_key=tool_key,
            arguments=arguments,
            raw=raw,
            status="error",
            partial=False,
            normalized=[],
            error=f"Gateway returned a non-data scalar response: {snippet}",
            reason_codes=_stable_reason_codes(tool, ["invalid_gateway_payload"]),
        )

    # MCP 语义补标注（执行单 mcp-semantic-annotation-execution-plan）：对登记了
    # 显式契约的工具（get_limit_up_count / list_limit_up_sectors），由适配器在
    # 深拷贝上完成字段映射、日期 lineage 与结构/业务分类。适配器是唯一权威，
    # 通用 flatten 与 row_identity 传播对这些工具跳过。
    semantic_normalized: list[NormalizedDatum] | None = None
    semantic_sidecar: dict[str, Any] | None = None
    if get_semantic_contract(tool) is not None:
        annotated = annotate_mcp_payload(
            tool,
            arguments,
            raw,
            declared_provider=declared_provider,
            requested_as_of_date=requested_as_of_date,
        )
        if annotated is not None:
            semantic_normalized, semantic_sidecar = annotated

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
    provider_candidates: list[str] = []
    domain = infer_domain_from_tool(tool)

    if isinstance(raw, dict):
        provider_candidates.extend(
            value for value in (raw.get("source_used"), raw.get("source"), raw.get("provider"))
            if isinstance(value, str) and value.strip()
        )
        # MCP envelopes commonly put the provider beside the business payload.
        # Read only explicit contract fields; never infer it from notes or transport.
        for envelope in (raw.get("metadata"), raw.get("meta"), raw.get("result")):
            if isinstance(envelope, dict):
                provider_candidates.extend(
                    value for value in (envelope.get("provider"), envelope.get("source_used"), envelope.get("source"))
                    if isinstance(value, str) and value.strip()
                )
    source = next((value.strip() for value in provider_candidates if provider_authority(value)), None)
    source = source or (declared_provider.strip() if isinstance(declared_provider, str) and declared_provider.strip() else None)
    if source is None:
        source = next(
            (value.strip() for value in provider_candidates if value.strip().casefold() not in {"mcp", "http", "http_api", "ths_skill"}),
            None,
        )

    # provider provenance：source_used/source 只在命中 provider 白名单时才能成为
    # authority。transport 名称与技能包装名都不是 provider，绝不允许冒用；无法
    # 归因到具体 provider 时保持 None，让下游按"无权威来源"处理。
    # datum.source 仍保留上游声明的原始标签（审计可读），但不冒充 authority。
    effective_source = provider_authority(source)
    declared_source = source.strip() if isinstance(source, str) and source.strip() else None
    global _TRUNCATED_CONTAINER_PATHS
    _TRUNCATED_CONTAINER_PATHS = _truncation_paths(truncations)

    if semantic_normalized is not None:
        # 适配器已完成字段映射、结构/业务分类、日期 lineage 与来源标注。
        normalized = semantic_normalized
        # 适配器按契约白名单计算 authority；与 root provider 解析保持一致。
        effective_source = semantic_sidecar.get("authority") if semantic_sidecar else effective_source
        declared_source = semantic_sidecar.get("source") if semantic_sidecar else declared_source
    else:
        normalized = (
            _extract_metrics(
                raw,
                _tool=tool,
                _domain=domain,
                _status=status,
                _partial=partial,
                _timestamp=timestamp,
                _source=effective_source,
                _date_index=_date_index(raw),
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
                    source=effective_source,
                )
            ]
        )

    if semantic_normalized is None:
        # Flat metric paths retain the original list row index. Propagate each
        # row's returned code/name to its sibling datums so a batched quote cannot
        # attribute one security's price to another requested security.
        row_identity: dict[str, dict[str, str]] = {}
        for datum in normalized:
            if "." not in datum.metric:
                continue
            parent, field_name = datum.metric.rsplit(".", 1)
            key = field_name.lower()
            if key in {"symbol", "code", "stock_code", "security_code", "ticker"} and datum.value:
                row_identity.setdefault(parent, {})["instrument"] = str(datum.value)
            elif key in {"name", "stock_name", "company_name", "security_name"} and datum.value:
                row_identity.setdefault(parent, {})["instrument_name"] = str(datum.value)
            elif key in {"date", "trade_date", "trading_date", "as_of_date", "日期", "交易日期", "交易日", "time", "datetime", "timestamp"}:
                stamp = str(datum.value)
                if tool == "get_market_quotes" and key == "time" and re.fullmatch(r"\d{14}", stamp):
                    try:
                        stamp = datetime.strptime(stamp, "%Y%m%d%H%M%S").isoformat()
                    except ValueError:
                        stamp = ""
                if observed_date := normalize_date(stamp):
                    row_identity.setdefault(parent, {})["as_of_date"] = observed_date
                    row_identity[parent]["timestamp"] = stamp
        for datum in normalized:
            if "." not in datum.metric:
                continue
            parent = datum.metric.rsplit(".", 1)[0]
            identity = row_identity.get(parent, {})
            # Quote book fields live below the returned security row. Resolve
            # their own ancestor, never another row or a batched request target.
            ancestor = parent
            while not identity and "." in ancestor:
                ancestor = ancestor.rsplit(".", 1)[0]
                identity = row_identity.get(ancestor, {})
            if not datum.instrument and identity.get("instrument"):
                datum.instrument = identity["instrument"]
            if not datum.instrument_name and identity.get("instrument_name"):
                datum.instrument_name = identity["instrument_name"]
            if not datum.as_of_date and identity.get("as_of_date"):
                datum.as_of_date = identity["as_of_date"]
            if not datum.timestamp and identity.get("timestamp"):
                datum.timestamp = identity["timestamp"]

    # 阶段 3 ⑤：市场级工具的数据点必须能说清"这是哪一天的市场"。
    # 上游没给时间戳时逐条标注，避免报告拿无日期的旧数据声称"今日"。
    market_note = _market_level_datum_note(tool, timestamp)
    if market_note:
        for datum in normalized:
            datum.note = "; ".join(part for part in (datum.note, market_note) if part)

    business_count = sum(1 for datum in normalized if not datum.structural_metadata)
    structural_count = len(normalized) - business_count

    # T1 兜底不变式：任何分支只要产出 0 条业务 datum，就绝不能报 success/partial，
    # 防止以后新增的分支再把"无数据"当成证据（例如整块 dict 只含元数据键）。
    if not normalized or business_count == 0:
        error_reason_codes = _stable_reason_codes(tool, ["empty_normalized"])
        return ToolResult(
            tool=tool,
            operation_id=tool,
            tool_key=tool_key,
            arguments=arguments,
            raw=raw,
            status="error",
            partial=False,
            note=note or _EMPTY_PAYLOAD_NOTE,
            normalized=[],
            error="Gateway returned no extractable data (normalization produced zero items)",
            reason_codes=error_reason_codes,
            # 保留适配器诊断：即使零业务 datum，也要能看到是缺日期/缺家数/空容器。
            date_annotation=semantic_sidecar,
            business_datum_count=0,
            structural_metadata_count=structural_count,
            semantic_diagnostics=list(semantic_sidecar.get("diagnostics", [])) if semantic_sidecar else [],
            evidence_created=False,
        )

    retrieved_at = datetime.now(UTC).isoformat()
    declared_dates = _date_index(raw)
    for datum in normalized:
        datum.retrieved_at = retrieved_at
        # items/data/rows 声明的日期要传播到对应 datum（条目自身日期优先）。
        # 语义适配器已在契约分支里显式设置日期/lineage，这里不覆盖已解析日期。
        if not datum.timestamp and not datum.as_of_date:
            declared = _declared_date_for(datum.metric, declared_dates)
            if declared:
                datum.timestamp = declared
        datum.as_of_date = datum.as_of_date or normalize_date(datum.timestamp)
        # 凭证只来自明确 provider；无法证明日期时 datum 自身记为 partial。
        if declared_source:
            datum.source = datum.source or declared_source
        if effective_source:
            datum.authority = datum.authority or effective_source
        # ``ToolResult.partial`` describes the coverage of the response set;
        # it does not make every returned row partial.  A datum is partial only
        # when that row/container is truncated or lacks the date required by a
        # market observation.  This lets a dated narrow fact survive a
        # result-level partial envelope.
        # Missing values and dates belong to this datum, while a coverage
        # limitation on a sibling/result belongs to the envelope.
        datum.partial = bool(datum.partial or _container_was_truncated(_container_key(datum.metric)))
        requires_date = is_market_capability(str(tool_key or tool)) or partial
        if (requires_date and not datum.as_of_date and not datum.structural_metadata) or datum.value in (None, "", [], {}):
            datum.partial = True
        datum.status = "partial" if datum.partial else "success"
        datum.completeness = "partial" if datum.partial else "complete"

    business_datums = [datum for datum in normalized if not datum.structural_metadata]
    observed_dates = {datum.as_of_date for datum in business_datums if datum.as_of_date}
    any_business_partial = any(datum.partial for datum in business_datums)
    # evidence_created：至少一条业务 datum 通过完整性、日期与来源检查。
    evidence_created = any(
        (not datum.partial)
        and datum.as_of_date
        and datum.source
        and datum.authority
        for datum in business_datums
    )

    # 结果级 partial：信封声明 partial，或任一业务 datum 缺身份/结构/日期。
    result_partial = bool(partial or any_business_partial)
    result_status: Status = "partial" if result_partial else "success"
    result_date = None
    if semantic_sidecar is not None and semantic_sidecar.get("as_of_date"):
        result_date = semantic_sidecar["as_of_date"]
    elif len(observed_dates) == 1:
        result_date = next(iter(observed_dates))

    return ToolResult(
        tool=tool,
        operation_id=tool,
        tool_key=tool_key,
        arguments=arguments,
        raw=raw,
        status=result_status if semantic_sidecar is not None else status,
        partial=result_partial if semantic_sidecar is not None else partial,
        note=note,
        normalized=normalized,
        retrieved_at=retrieved_at,
        as_of_date=result_date,
        completeness="partial" if result_partial else "complete",
        authority=effective_source,
        date_annotation=semantic_sidecar,
        business_datum_count=sum(1 for datum in normalized if not datum.structural_metadata),
        structural_metadata_count=sum(1 for datum in normalized if datum.structural_metadata),
        semantic_diagnostics=list(semantic_sidecar.get("diagnostics", [])) if semantic_sidecar else [],
        evidence_created=evidence_created,
    )


# ------------------------------------------------------------------ #
# 向后兼容别名 — 旧测试使用 _infer_domain 名称                         #
# ------------------------------------------------------------------ #

_infer_domain = infer_domain_from_tool
