"""Mosaic 多域工具注册表。

核心设计原则：
- ToolMeta.domain 标识该工具所属市场域
- Planner 通过 domain 过滤候选工具，而不是硬编码操作 ID
- 新增市场域只需添加新 ToolMeta 条目并更新 DEFAULT_DOMAINS
- 兼容旧版 resolve_tool(key) → ToolMeta 查询

所有 33 个 Market Gateway Tool 均在此声明，按域分组。
后续 Tool 名变化时，优先修改这里，而不是 Planner。
"""

from typing import Any, Literal

from app.models.research import DEFAULT_DOMAINS, MarketDomain


# ------------------------------------------------------------------ #
# 工具元数据                                                          #
# ------------------------------------------------------------------ #

class ToolMeta:
    """单个工具的逻辑描述。"""

    __slots__ = (
        "key",
        "tool_name",
        "purpose",
        "domain",
        "priority",
        "http_method",
        "http_path",
    )

    def __init__(
        self,
        key: str,
        tool_name: str,
        purpose: str,
        domain: MarketDomain = "a_share",
        priority: Literal["high", "medium", "low"] = "medium",
        http_method: str = "GET",
        http_path: str = "",
    ):
        self.key = key
        self.tool_name = tool_name
        self.purpose = purpose
        self.domain = domain
        self.priority = priority
        self.http_method = http_method
        self.http_path = http_path

    def model_dump(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "tool_name": self.tool_name,
            "purpose": self.purpose,
            "domain": self.domain,
            "priority": self.priority,
            "http_method": self.http_method,
            "http_path": self.http_path,
        }


# ------------------------------------------------------------------ #
# A 股市场生态（涨停、情绪）                                           #
# ------------------------------------------------------------------ #

_ASHARE_MASTERTOOLS = [
    ToolMeta(
        "sentiment",
        "public_sentiment_ashare_master_sentiment_get",
        "A股市场情绪时间序列（风险偏好、涨跌比等）",
        domain="a_share",
        priority="high",
        http_method="GET",
        http_path="/ashare-master/sentiment",
    ),
    ToolMeta(
        "limit_up_count",
        "public_limit_up_count_ashare_master_limit_up_count_get",
        "A股当日涨停家数（活跃指标）",
        domain="a_share",
        priority="high",
        http_method="GET",
        http_path="/ashare-master/limit-up/count",
    ),
    ToolMeta(
        "limit_up_sectors",
        "public_limit_up_sectors_ashare_master_limit_up_sectors_get",
        "A股当日涨停题材与板块分布（题材强度）",
        domain="a_share",
        priority="high",
        http_method="GET",
        http_path="/ashare-master/limit-up/sectors",
    ),
    ToolMeta(
        "limit_up_pool",
        "public_limit_up_pool_ashare_master_limit_up_pool_get",
        "A股当日涨停股票明细（深入调查用）",
        domain="a_share",
        priority="medium",
        http_method="GET",
        http_path="/ashare-master/limit-up/pool",
    ),
]

# ------------------------------------------------------------------ #
# A 股基本面 & 深度                                                   #
# ------------------------------------------------------------------ #

_ASHARE_FUNDAMENTALS = [
    ToolMeta(
        "overview",
        "overview_eastmoney_overview_get",
        "A股全市场估值/市值、涨跌、两融、未来解禁的一览",
        domain="a_share",
        priority="high",
        http_method="GET",
        http_path="/eastmoney/overview",
    ),
    ToolMeta(
        "detail",
        "detail_eastmoney_detail_get",
        "A股公司股东、两融、解禁、减持时间表线",
        domain="a_share",
        priority="medium",
        http_method="GET",
        http_path="/eastmoney/detail",
    ),
    ToolMeta(
        "business",
        "business_eastmoney_f10_business_get",
        "A股公司主营业务信息",
        domain="a_share",
        priority="low",
        http_method="GET",
        http_path="/eastmoney/f10/business",
    ),
    ToolMeta(
        "concept",
        "concept_eastmoney_f10_concept_get",
        "A股公司概念标签",
        domain="a_share",
        priority="low",
        http_method="GET",
        http_path="/eastmoney/f10/concept",
    ),
    ToolMeta(
        "finance",
        "finance_eastmoney_f10_finance_get",
        "A股公司财务信息",
        domain="a_share",
        priority="low",
        http_method="GET",
        http_path="/eastmoney/f10/finance",
    ),
    ToolMeta(
        "shareholders",
        "shareholders_eastmoney_f10_shareholders_get",
        "A股公司股东信息",
        domain="a_share",
        priority="low",
        http_method="GET",
        http_path="/eastmoney/f10/shareholders",
    ),
    ToolMeta(
        "survey",
        "survey_eastmoney_f10_survey_get",
        "A股公司资料/调查信息",
        domain="a_share",
        priority="low",
        http_method="GET",
        http_path="/eastmoney/f10/survey",
    ),
]

# ------------------------------------------------------------------ #
# A 股实时行情 & 微观结构（雪球/腾讯）                                 #
# ------------------------------------------------------------------ #

_ASHARE_MICRO = [
    ToolMeta(
        "quote",
        "quote_tencent_quote_get",
        "多市场实时行情与五档盘口（腾讯 API，支持 A 股 / 港股 / US Stock 代码）",
        domain="a_share",
        priority="medium",
        http_method="GET",
        http_path="/tencent/quote",
    ),
    ToolMeta(
        "longhu",
        "longhu_xueqiu_longhu_get",
        "A 股龙虎榜资金流向",
        domain="a_share",
        priority="medium",
        http_method="GET",
        http_path="/xueqiu/longhu",
    ),
    ToolMeta(
        "abnormal_reasons",
        "abnormal_reasons_xueqiu_abnormal_reasons_get",
        "雪球日K异常涨跌与趋势区间归因",
        domain="a_share",
        priority="medium",
        http_method="GET",
        http_path="/xueqiu/abnormal-reasons",
    ),
    ToolMeta(
        "orderbook",
        "orderbook_xueqiu_orderbook_get",
        "盘口挂单数据",
        domain="a_share",
        priority="low",
        http_method="GET",
        http_path="/xueqiu/orderbook",
    ),
    ToolMeta(
        "trades",
        "trades_xueqiu_trades_get",
        "逐笔成交数据",
        domain="a_share",
        priority="low",
        http_method="GET",
        http_path="/xueqiu/trades",
    ),
    ToolMeta(
        "timeline",
        "timeline_xueqiu_timeline_get",
        "分时/时间线数据",
        domain="a_share",
        priority="low",
        http_method="GET",
        http_path="/xueqiu/timeline",
    ),
    ToolMeta(
        "search",
        "search_xueqiu_search_get",
        "证券搜索（支持 A 股 + 港股，仅用于候选筛选，不替代身份确认）",
        domain="a_share",
        priority="low",
        http_method="GET",
        http_path="/xueqiu/search",
    ),
]

# ------------------------------------------------------------------ #
# 港股 — 当前复用 quote + search（腾讯行情支持 HK 代码）               #
# 后续 Market Gateway 接入独立港股 API 时替换为专用工具                 #
# ------------------------------------------------------------------ #

_HK_STOCK_PLACEHOLDERS = [
    # TODO: 接入独立港股数据源（如 Wind、Bloomberg、Yahoo Finance API）
    # 当前通过 quote_tencent_quote_get 传入 HK 股票代码（如 HK00700）获取行情
    # 后续专用工具: hk_quote, hk_realtime, hk_flow_southbound 等
    ToolMeta(
        "hk_quote",
        "quote_tencent_quote_get",
        "港股实时行情（腾讯 API，传入 HK 代码如 HK03400/00700）",
        domain="a_share",
        priority="high",
        http_method="GET",
        http_path="/tencent/quote",
    ),
    ToolMeta(
        "hk_search",
        "search_xueqiu_search_get",
        "港股证券搜索（雪球，支持 HK 股票代码和名称）",
        domain="a_share",
        priority="medium",
        http_method="GET",
        http_path="/xueqiu/search",
    ),
    # ── 北向资金流入（香港通） — 内部直连 Eastmoney KLineJSAPI ──
    # ⚠️ 这些工具不走 Market Gateway，由 app/research/hk_northbound.fetch_all_hk_context() 直接获取。
    #    Gateway 侧如需统一接入，需添加以下端点：
    #      POST /eastmoney/northbound     → {secid: "HK.960036/SZ.960053/SH.960052", days: int}
    #      GET  /market/snapshot?symbol=hk00700&exchange=tencent  （已有，复用中）
    # OperationId 待 Gateway 实现后更新为真实值。
    ToolMeta(
        "hk_northbound_daily",
        "internal_hk_northbound",
        "港股通北向资金净流入时序（东财接口）— 内部实现，不经过 Gateway",
        domain="hk_stock",
        priority="high",
        http_method="INTERNAL",
        http_path="",
    ),
    ToolMeta(
        "hk_index_snapshot",
        "internal_hk_index",
        "恒生指数 & 恒生科技指数快照 — 内部实现，不经过 Gateway",
        domain="hk_stock",
        priority="medium",
        http_method="INTERNAL",
        http_path="",
    ),
]

# ------------------------------------------------------------------ #
# 大宗商品 — 待接入金属 / 能源 / 农产品数据源                          #
# ------------------------------------------------------------------ #

_COMMODITIES_PLACEHOLDERS = [
    # ✅ OKX 已确认支持以下贵金属永续合约（ccxt 统一写法）：
    #   XAU/USDT:USDT（黄金）, XAG/USDT:USDT（白银）, XPT/USDT:USDT（铂金）
    # ⚠️ PALL（钯金）、铜、原油暂无主流 crypto 交易所标准 USDT 永续
    ToolMeta(
        "commodity_gold",
        "klines_market_klines_post",
        "黄金 XAU 价格 K 线（OKX 永续合约，需 symbol=XAU/USDT:USDT）",
        domain="commodities",
        priority="high",
        http_method="POST",
        http_path="/market/klines",
    ),
    ToolMeta(
        "commodity_silver",
        "snapshot_market_snapshot_post",
        "白银 XAG 实时行情快照（OKX 永续合约，需 symbol=XAG/USDT:USDT）",
        domain="commodities",
        priority="medium",
        http_method="POST",
        http_path="/market/snapshot",
    ),
    ToolMeta(
        "commodity_platinum",
        "snapshot_market_snapshot_post",
        "铂金 XPT 实时行情快照（OKX 永续合约，需 symbol=XPT/USDT:USDT）",
        domain="commodities",
        priority="low",
        http_method="POST",
        http_path="/market/snapshot",
    ),
]

# ------------------------------------------------------------------ #
# US Stock 占位符 — 接入第三方 API 后替换真实 operationId             #
# ------------------------------------------------------------------ #

_US_STOCK_PLACEHOLDERS = []

_CRYPTO_MARKET = [
    ToolMeta(
        "klines",
        "klines_market_klines_post",
        "通用历史 K 线（POST，支持任意 symbol 如 BTC/USDT, XAU/USDT:USDT, SH600519，返回 UTC 毫秒时间戳）",
        domain="cross",
        priority="high",
        http_method="POST",
        http_path="/market/klines",
    ),
    ToolMeta(
        "snapshot",
        "snapshot_market_snapshot_post",
        "通用行情快照 POST（价格/24h量/涨跌幅，支持任意 symbol 如 XAU/USDT:USDT, BTC/USDT）",
        domain="cross",
        priority="high",
        http_method="POST",
        http_path="/market/snapshot",
    ),
    ToolMeta(
        "window",
        "window_market_window_post",
        "复盘时间窗聚合数据（通用，支持任意 symbol）",
        domain="cross",
        priority="medium",
        http_method="POST",
        http_path="/market/window",
    ),
    ToolMeta(
        "exchanges",
        "exchanges_market_exchanges_get",
        "交易所/周期能力查询（确认支持情况）",
        domain="crypto",
        priority="medium",
        http_method="GET",
        http_path="/market/exchanges",
    ),
]

# ------------------------------------------------------------------ #
# Crypto 衍生品 & 永续合约                                            #
# ------------------------------------------------------------------ #

_CRYPTO_DERIVATIVES = [
    ToolMeta(
        "derivatives_history",
        "derivatives_history_market_derivatives_history_post",
        "永续历史资金费率、OI、多空指标",
        domain="crypto",
        priority="high",
        http_method="POST",
        http_path="/market/derivatives/history",
    ),
]

# ------------------------------------------------------------------ #
# CoinGlass / Hyperliquid                                             #
# ------------------------------------------------------------------ #

_CRYPTO_COINGLASS = [
    ToolMeta(
        "hyperliquid_symbols",
        "hyperliquid_symbols_coinglass_hyperliquid_symbols_get",
        "Hyperliquid 可用结算币种列表",
        domain="crypto",
        priority="medium",
        http_method="GET",
        http_path="/coinglass/hyperliquid/symbols",
    ),
    ToolMeta(
        "liqmap",
        "hyperliquid_liqmap_coinglass_hyperliquid_liqmap_get",
        "大户仓位与清算价位地图",
        domain="crypto",
        priority="high",
        http_method="GET",
        http_path="/coinglass/hyperliquid/liqmap",
    ),
    ToolMeta(
        "top_position",
        "hyperliquid_top_position_coinglass_hyperliquid_top_position_get",
        "跨币种截断持仓榜",
        domain="crypto",
        priority="high",
        http_method="GET",
        http_path="/coinglass/hyperliquid/top-position",
    ),
    ToolMeta(
        "user_count",
        "hyperliquid_user_count_coinglass_hyperliquid_user_count_get",
        "Hyperliquid 全站地址数时序",
        domain="crypto",
        priority="medium",
        http_method="GET",
        http_path="/coinglass/hyperliquid/user-count",
    ),
    ToolMeta(
        "vaults",
        "hyperliquid_vaults_coinglass_hyperliquid_vaults_get",
        "金库 APR/回撤",
        domain="crypto",
        priority="low",
        http_method="GET",
        http_path="/coinglass/hyperliquid/vaults",
    ),
    ToolMeta(
        "liquidation_today",
        "liquidation_today_coinglass_liquidation_today_get",
        "当日全网爆仓统计",
        domain="crypto",
        priority="high",
        http_method="GET",
        http_path="/coinglass/liquidation/today",
    ),
    ToolMeta(
        "funding_rate",
        "funding_rate_coinglass_funding_rate_get",
        "资金费率榜",
        domain="crypto",
        priority="high",
        http_method="GET",
        http_path="/coinglass/funding-rate",
    ),
]

# ------------------------------------------------------------------ #
# US Stock 占位符 — 接入第三方 API 后替换真实 operationId             #
# ------------------------------------------------------------------ #

_US_STOCK_PLACEHOLDERS = [
    # TODO: 接入 real market data provider (如 AlphaVantage, Finnhub, Polygon)
    # 当前 key 命名预留，operationId 待填充
]

# ------------------------------------------------------------------ #
# Macro 占位符                                                        #
# ------------------------------------------------------------------ #

_MACRO_PLACEHOLDERS = []

# ------------------------------------------------------------------ #
# 服务健康检查                                                        #
# ------------------------------------------------------------------ #

_HEALTH_TOOLS = [
    ToolMeta(
        "health",
        "health_health_get",
        "网关进程和本机依赖健康状态",
        domain="unknown",
        priority="low",
        http_method="GET",
        http_path="/health",
    ),
    ToolMeta(
        "market_health",
        "health_market_health_get",
        "行情模块状态",
        domain="unknown",
        priority="low",
        http_method="GET",
        http_path="/market/health",
    ),
]

# ------------------------------------------------------------------ #
# 完整注册表 & 索引                                                     #
# ------------------------------------------------------------------ #

#: 所有已注册的工具元数据。
ALL_TOOLS: list[ToolMeta] = (
    _ASHARE_MASTERTOOLS
    + _ASHARE_FUNDAMENTALS
    + _ASHARE_MICRO
    + _HK_STOCK_PLACEHOLDERS
    + _COMMODITIES_PLACEHOLDERS
    + _CRYPTO_MARKET
    + _CRYPTO_DERIVATIVES
    + _CRYPTO_COINGLASS
    + _US_STOCK_PLACEHOLDERS
    + _MACRO_PLACEHOLDERS
    + _HEALTH_TOOLS
)

#: key → ToolMeta 索引（业务层面使用）
BY_KEY: dict[str, ToolMeta] = {x.key: x for x in ALL_TOOLS}

#: operationId → ToolMeta 索引（底层调用使用）
BY_NAME: dict[str, ToolMeta] = {x.tool_name: x for x in ALL_TOOLS}

# 保留向后兼容别名
BY_KEY_CORE = BY_KEY
BY_NAME_CORE = BY_NAME

#: 向后兼容 — 旧代码使用 CORE_TOOLS 变量名
CORE_TOOLS = ALL_TOOLS


# ------------------------------------------------------------------ #
# 公开 API                                                              #
# ------------------------------------------------------------------ #

def registry_text(domains: list[MarketDomain] | None = None) -> str:
    """返回格式化文本供 Prompt 使用。

    Args:
        domains: 只包含这些域的工具；None 表示全部。
    """
    if domains is None:
        tools = ALL_TOOLS
    else:
        filtered = [t for t in ALL_TOOLS if t.domain in domains]
        # 同时始终包含 health 工具（不受域名限制）
        filtered = [t for t in filtered if t.domain != "unknown"] or ALL_TOOLS
        tools = filtered

    return "\n".join(
        f"- {x.key}: {x.tool_name} [{x.domain}] — {x.purpose} [{x.priority}]"
        for x in tools
    )


def resolve_tool(key: str) -> ToolMeta:
    """通过逻辑 key 解析 ToolMeta。"""
    if key not in BY_KEY:
        raise KeyError(f"Unknown tool key: {key}")
    return BY_KEY[key]


def resolve_tool_by_name(tool_name: str) -> ToolMeta:
    """通过 operationId 解析 ToolMeta。"""
    if tool_name not in BY_NAME:
        raise KeyError(f"Tool is not allowed by registry: {tool_name}")
    return BY_NAME[tool_name]


def tools_by_domain(domain: MarketDomain) -> list[ToolMeta]:
    """获取指定域的所有工具。"""
    return [t for t in ALL_TOOLS if t.domain == domain]


def build_mcp_tool_index(mcp_tools: list[Any]) -> dict[str, Any]:
    """只暴露 Registry 批准的 Tool 给 Agent。

    返回一个 key → MCP tool 对象的字典，Planner/Evaluator 使用 logical key。
    """
    available = {getattr(t, "name", ""): t for t in mcp_tools}
    result: dict[str, Any] = {}
    for meta in ALL_TOOLS:
        if meta.tool_name in available:
            result[meta.key] = available[meta.tool_name]
    return result


def get_enabled_domains() -> list[MarketDomain]:
    """返回当前启用的市场域列表（排除 unknown）。"""
    seen: set[str] = set()
    for t in ALL_TOOLS:
        if t.domain != "unknown":
            seen.add(t.domain)
    return sorted(seen)
