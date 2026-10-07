"""Mosaic 多域工具注册表。

核心设计原则：
- ToolMeta.domain 标识该工具所属市场域
- Planner 通过 domain 过滤候选工具，而不是硬编码操作 ID
- 新增市场域只需添加新 ToolMeta 条目并更新 DEFAULT_DOMAINS
- 兼容旧版 resolve_tool(key) → ToolMeta 查询

所有 44 个 Market Gateway Tool 均在此声明，按域分组。
后续 Tool 名变化时，优先修改这里，而不是 Planner。
"""

import logging
from typing import Any, Literal

from app.models.research import MarketDomain

logger = logging.getLogger(__name__)

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
        "category",
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
        category: str = "shared",
    ):
        self.key = key
        self.tool_name = tool_name
        self.purpose = purpose
        self.domain = domain
        self.priority = priority
        self.http_method = http_method
        self.http_path = http_path
        self.category = category

    def model_dump(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "tool_name": self.tool_name,
            "purpose": self.purpose,
            "domain": self.domain,
            "priority": self.priority,
            "http_method": self.http_method,
            "http_path": self.http_path,
            "category": self.category,
        }


# ------------------------------------------------------------------ #
# A 股市场生态（涨停、情绪）                                           #
# ------------------------------------------------------------------ #

_ASHARE_MASTERTOOLS = [
    ToolMeta(
        "sentiment",
        "get_ashare_sentiment",
        "A股市场情绪时间序列（风险偏好、涨跌比等）",
        domain="a_share",
        priority="high",
        http_method="GET",
        http_path="/ashare-master/sentiment",
        category="technical",
    ),
    ToolMeta(
        "limit_up_count",
        "get_limit_up_count",
        "A股当日涨停家数（活跃指标）",
        domain="a_share",
        priority="high",
        http_method="GET",
        http_path="/ashare-master/limit-up/count",
        category="technical",
    ),
    ToolMeta(
        "limit_up_sectors",
        "list_limit_up_sectors",
        "A股当日涨停题材与板块分布（题材强度）",
        domain="a_share",
        priority="high",
        http_method="GET",
        http_path="/ashare-master/limit-up/sectors",
        category="technical",
    ),
    ToolMeta(
        "limit_up_pool",
        "list_limit_up_stocks",
        "A股当日涨停股票明细（深入调查用）",
        domain="a_share",
        priority="medium",
        http_method="GET",
        http_path="/ashare-master/limit-up/pool",
        category="technical",
    ),
]

# ------------------------------------------------------------------ #
# A 股基本面 & 深度                                                   #
# ------------------------------------------------------------------ #

_ASHARE_FUNDAMENTALS = [
    ToolMeta(
        "overview",
        "get_company_overview",
        "A股全市场估值/市值、涨跌、两融、未来解禁的一览",
        domain="a_share",
        priority="high",
        http_method="GET",
        http_path="/eastmoney/overview",
        category="fundamental",
    ),
    ToolMeta(
        "detail",
        "get_company_detail",
        "A股公司股东、两融、解禁、减持时间表线",
        domain="a_share",
        priority="medium",
        http_method="GET",
        http_path="/eastmoney/detail",
        category="fundamental",
    ),
    ToolMeta(
        "business",
        "get_company_business",
        "A股公司主营业务信息",
        domain="a_share",
        priority="low",
        http_method="GET",
        http_path="/eastmoney/f10/business",
        category="fundamental",
    ),
    ToolMeta(
        "concept",
        "get_company_concepts",
        "A股公司概念标签",
        domain="a_share",
        priority="low",
        http_method="GET",
        http_path="/eastmoney/f10/concept",
        category="fundamental",
    ),
    ToolMeta(
        "finance",
        "get_company_finance",
        "A股公司财务信息",
        domain="a_share",
        priority="low",
        http_method="GET",
        http_path="/eastmoney/f10/finance",
        category="fundamental",
    ),
    ToolMeta(
        "shareholders",
        "get_company_shareholders",
        "A股公司股东信息",
        domain="a_share",
        priority="low",
        http_method="GET",
        http_path="/eastmoney/f10/shareholders",
        category="fundamental",
    ),
    ToolMeta(
        "survey",
        "get_company_survey",
        "A股公司资料/调查信息",
        domain="a_share",
        priority="low",
        http_method="GET",
        http_path="/eastmoney/f10/survey",
        category="fundamental",
    ),
]

# ------------------------------------------------------------------ #
# A 股实时行情 & 微观结构（雪球/腾讯）                                 #
# ------------------------------------------------------------------ #

_ASHARE_MICRO = [
    ToolMeta(
        "quote",
        "get_market_quotes",
        "多市场实时行情与五档盘口（腾讯 API，支持 A 股 / 港股 / US Stock 代码）",
        domain="a_share",
        priority="medium",
        http_method="GET",
        http_path="/market/quotes",
        category="technical",
    ),
    ToolMeta(
        "longhu",
        "get_stock_longhu",
        "A 股龙虎榜资金流向",
        domain="a_share",
        priority="medium",
        http_method="GET",
        http_path="/market/longhu",
        category="moneyflow",
    ),
    ToolMeta(
        "abnormal_reasons",
        "get_stock_abnormal_reasons",
        "雪球日K异常涨跌与趋势区间归因",
        domain="a_share",
        priority="medium",
        http_method="GET",
        http_path="/market/abnormal-reasons",
        category="technical",
    ),
    ToolMeta(
        "orderbook",
        "get_market_orderbook",
        "盘口挂单数据",
        domain="a_share",
        priority="low",
        http_method="GET",
        http_path="/market/orderbook",
        category="technical",
    ),
    ToolMeta(
        "trades",
        "list_market_trades",
        "逐笔成交数据",
        domain="a_share",
        priority="low",
        http_method="GET",
        http_path="/market/trades",
        category="technical",
    ),
    ToolMeta(
        "timeline",
        "list_stock_discussions",
        "分时/时间线数据",
        domain="a_share",
        priority="low",
        http_method="GET",
        http_path="/market/discussions",
        category="technical",
    ),
    ToolMeta(
        "search",
        "search_stocks",
        "证券搜索（支持 A 股 + 港股，仅用于候选筛选，不替代身份确认）",
        domain="a_share",
        priority="low",
        http_method="GET",
        http_path="/market/search",
        category="technical",
    ),
]

# ------------------------------------------------------------------ #
# 港股 — 当前复用 quote + search（腾讯行情支持 HK 代码）               #
# 后续 Market Gateway 接入独立港股 API 时替换为专用工具                 #
# ------------------------------------------------------------------ #

_HK_STOCK_PLACEHOLDERS = [
    # TODO: 接入独立港股数据源（如 Wind、Bloomberg、Yahoo Finance API）
    # 当前通过 get_market_quotes 传入 HK 股票代码（如 HK00700）获取行情
    # 后续专用工具: hk_quote, hk_realtime, hk_flow_southbound 等
    # T24：domain 修为 hk_stock —— 旧值 a_share 让港股工具被算进 A 股域、
    # 分派给错误的 analyst。注意这两个条目与 A 股 quote/search 复用同一
    # operationId：BY_NAME 的规范条目仍是 A 股原生的 quote/search（见索引区）。
    ToolMeta(
        "hk_quote",
        "get_market_quotes",
        "港股实时行情（腾讯 API，传入 HK 代码如 HK03400/00700）",
        domain="hk_stock",
        priority="high",
        http_method="GET",
        http_path="/market/quotes",
        category="technical",
    ),
    ToolMeta(
        "hk_search",
        "search_stocks",
        "港股证券搜索（雪球，支持 HK 股票代码和名称）",
        domain="hk_stock",
        priority="medium",
        http_method="GET",
        http_path="/market/search",
        # 与 A 股同名工具（search → technical）对齐；旧值 moneyflow 漂移
        category="technical",
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
        category="moneyflow",
    ),
    ToolMeta(
        "hk_index_snapshot",
        "internal_hk_index",
        "恒生指数 & 恒生科技指数快照 — 内部实现，不经过 Gateway",
        domain="hk_stock",
        priority="medium",
        http_method="INTERNAL",
        http_path="",
        category="technical",
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
        "get_market_klines",
        "黄金 XAU 价格 K 线（OKX 永续合约，需 symbol=XAU/USDT:USDT）",
        domain="commodities",
        priority="high",
        http_method="POST",
        http_path="/market/klines",
        category="technical",
    ),
    ToolMeta(
        "commodity_silver",
        "get_market_snapshot",
        "白银 XAG 实时行情快照（OKX 永续合约，需 symbol=XAG/USDT:USDT）",
        domain="commodities",
        priority="medium",
        http_method="POST",
        http_path="/market/snapshot",
        category="technical",
    ),
    ToolMeta(
        "commodity_platinum",
        "get_market_snapshot",
        "铂金 XPT 实时行情快照（OKX 永续合约，需 symbol=XPT/USDT:USDT）",
        domain="commodities",
        priority="low",
        http_method="POST",
        http_path="/market/snapshot",
        category="technical",
    ),
]

_CRYPTO_MARKET = [
    ToolMeta(
        "klines",
        "get_market_klines",
        "通用历史 K 线（POST，支持任意 symbol 如 BTC/USDT, XAU/USDT:USDT, SH600519，返回 UTC 毫秒时间戳）。"
        "多源：crypto（exchange=binance 等）与大宗商品；美股请用 us_klines/us_window（exchange=xueqiu + 裸代码）",
        domain="cross",
        priority="high",
        http_method="POST",
        http_path="/market/klines",
        category="technical",
    ),
    ToolMeta(
        "snapshot",
        "get_market_snapshot",
        "通用行情快照 POST（价格/24h量/涨跌幅，支持任意 symbol 如 XAU/USDT:USDT, BTC/USDT）。"
        "仅支持 crypto 交易所（binance/okx/bybit/aster/hyperliquid），不支持 A股/港股/美股个股"
        "（实测 exchange=xueqiu → 422），美股行情用 us_klines/us_window",
        domain="cross",
        priority="high",
        http_method="POST",
        http_path="/market/snapshot",
        category="technical",
    ),
    ToolMeta(
        "window",
        "get_market_window",
        "复盘时间窗聚合数据（通用，支持任意 symbol）。"
        "多源：crypto（exchange=binance 等）与大宗商品；美股请用 us_klines/us_window（exchange=xueqiu + 裸代码）",
        domain="cross",
        priority="medium",
        http_method="POST",
        http_path="/market/window",
        category="technical",
    ),
    ToolMeta(
        "exchanges",
        "list_exchanges",
        "交易所/周期能力查询（确认支持情况）",
        domain="crypto",
        priority="medium",
        http_method="GET",
        http_path="/market/exchanges",
        category="technical",
    ),
]

# ------------------------------------------------------------------ #
# Crypto 衍生品 & 永续合约                                            #
# ------------------------------------------------------------------ #

_CRYPTO_DERIVATIVES = [
    ToolMeta(
        "derivatives_history",
        "get_derivatives_history",
        "永续历史资金费率、OI、多空指标",
        domain="crypto",
        priority="high",
        http_method="POST",
        http_path="/market/derivatives/history",
        category="moneyflow",
    ),
]

# ------------------------------------------------------------------ #
# CoinGlass / Hyperliquid                                             #
# ------------------------------------------------------------------ #

_CRYPTO_COINGLASS = [
    ToolMeta(
        "hyperliquid_symbols",
        "list_hyperliquid_symbols",
        "Hyperliquid 可用结算币种列表",
        domain="crypto",
        priority="medium",
        http_method="GET",
        http_path="/coinglass/hyperliquid/symbols",
        category="moneyflow",
    ),
    ToolMeta(
        "liqmap",
        "get_hyperliquid_liquidation_map",
        "大户仓位与清算价位地图",
        domain="crypto",
        priority="high",
        http_method="GET",
        http_path="/coinglass/hyperliquid/liqmap",
        category="moneyflow",
    ),
    ToolMeta(
        "top_position",
        "get_hyperliquid_top_position",
        "跨币种截断持仓榜",
        domain="crypto",
        priority="high",
        http_method="GET",
        http_path="/coinglass/hyperliquid/top-position",
        category="moneyflow",
    ),
    ToolMeta(
        "user_count",
        "get_hyperliquid_user_count",
        "Hyperliquid 全站地址数时序",
        domain="crypto",
        priority="medium",
        http_method="GET",
        http_path="/coinglass/hyperliquid/user-count",
        category="moneyflow",
    ),
    ToolMeta(
        "vaults",
        "list_hyperliquid_vaults",
        "金库 APR/回撤",
        domain="crypto",
        priority="low",
        http_method="GET",
        http_path="/coinglass/hyperliquid/vaults",
        category="moneyflow",
    ),
    ToolMeta(
        "liquidation_today",
        "get_crypto_liquidation_today",
        "当日全网爆仓统计",
        domain="crypto",
        priority="high",
        http_method="GET",
        http_path="/coinglass/liquidation/today",
        category="moneyflow",
    ),
    ToolMeta(
        "funding_rate",
        "list_crypto_funding_rates",
        "资金费率榜",
        domain="crypto",
        priority="high",
        http_method="GET",
        http_path="/coinglass/funding-rate",
        category="moneyflow",
    ),
]

# ------------------------------------------------------------------ #
# 美股 —— 雪球通道行情 + SEC EDGAR 基本面                               #
# （行情 A0 实测：klines/window 可用，snapshot 不支持；                   #
#  基本面 A0 实测：EDGAR 三类端点带合规 UA 均 200，AAPL/NVDA 核心概念齐全）#
# ------------------------------------------------------------------ #

_US_STOCK_PLACEHOLDERS = [
    # A0 实测（2026-10-06，scripts/verify_us_market.py）：exchange=xueqiu 支持美股
    # 裸代码（AAPL/NVDA/TSLA/SPCX 均返回真实 OHLCV）；但 /market/snapshot 对
    # exchange=xueqiu 服务端 422「不支持实时快照」，故不注册 us_snapshot。
    # symbol 格式：裸代码，如 AAPL；港股式前缀/后缀均不需要。
    ToolMeta(
        "us_klines",
        "get_market_klines",
        "美股历史 K 线（雪球通道，必填: symbol=裸代码如 AAPL, exchange=xueqiu, interval=1d, "
        "start/end=ISO 日期范围如 2026-09-20/2026-10-05）",
        domain="us_stock",
        priority="high",
        http_method="POST",
        http_path="/market/klines",
        category="technical",
    ),
    ToolMeta(
        "us_window",
        "get_market_window",
        "美股复盘时间窗聚合（雪球通道，必填: symbol=裸代码如 AAPL, exchange=xueqiu, "
        "interval=1d, anchor=锚定日期如 2026-10-02）",
        domain="us_stock",
        priority="medium",
        http_method="POST",
        http_path="/market/window",
        category="technical",
    ),
    # A0 实测（2026-10-06，scripts/verify_sec_edgar.py）：EDGAR 三类端点带合规
    # UA 均 200、无 UA 403；AAPL/NVDA 营收/净利/EPS/毛利候选 tag 齐全（EPS 单位
    # 是 USD/shares；营收 tag 有新旧口径，研究模块按最新 end 选优）。内部直连，
    # 需 SEC_EDGAR_CONTACT 配置（SEC 公平访问政策），未配置时运行时返回 error。
    ToolMeta(
        "us_fundamentals",
        "internal_us_fundamentals",
        "美股基本面（SEC EDGAR XBRL，必填: symbol=美股裸代码如 AAPL；返回营收/净利/EPS/毛利的最新年报与季报值，含申报文件溯源）",
        domain="us_stock",
        priority="high",
        http_method="INTERNAL",
        http_path="",
        category="fundamental",
    ),
    ToolMeta(
        "us_filings_recent",
        "internal_us_filings_recent",
        "美股近期 SEC 申报（必填: symbol=美股裸代码如 AAPL；返回最近 10 份 10-K/10-Q/8-K 的类型/申报日/文件链接）",
        domain="us_stock",
        priority="medium",
        http_method="INTERNAL",
        http_path="",
        category="fundamental",
    ),
]

# ------------------------------------------------------------------ #
# 新闻舆情 — DDGS 内部直连（跨域通用）                                 #
# ------------------------------------------------------------------ #

_NEWS_SEARCH = [
    ToolMeta(
        "news_search",
        "news_search",
        "DDGS 新闻舆情搜索（必填: query=搜索词; 可选: max_results, time_limit=d/w/m，不限市场域）",
        domain="cross",
        priority="medium",
        http_method="INTERNAL",
        http_path="",
        category="news",
    ),
]

# ------------------------------------------------------------------ #
# 舆情评论 — P4-5：评论爬取 MCP 就绪后启用（工具名以实际 MCP 为准）     #
# ------------------------------------------------------------------ #
# _SENTIMENT_TOOLS = [
#     ToolMeta("xq_comments", "comments_xueqiu_get", "雪球个股评论区抓取",
#              category="sentiment", domain="a_share", ...),
#     ToolMeta("futu_comments", "comments_futu_get", "富途牛牛个股评论区",
#              category="sentiment", domain="hk_stock", ...),
#     ToolMeta("ths_comments", "comments_ths_get", "同花顺个股/板块评论区",
#              category="sentiment", domain="a_share", ...),
# ]

# ------------------------------------------------------------------ #
# 服务健康检查                                                        #
# ------------------------------------------------------------------ #

_HEALTH_TOOLS = [
    ToolMeta(
        "health",
        "get_service_health",
        "网关进程和本机依赖健康状态",
        domain="unknown",
        priority="low",
        http_method="GET",
        http_path="/health",
        category="shared",
    ),
    ToolMeta(
        "market_health",
        "get_market_health",
        "行情模块状态",
        domain="unknown",
        priority="low",
        http_method="GET",
        http_path="/market/health",
        category="shared",
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
    + _NEWS_SEARCH
    # + _SENTIMENT_TOOLS  # P4-5：评论 MCP 就绪后取消注释
    + _HEALTH_TOOLS
)

#: key → ToolMeta 索引（业务层面使用）
BY_KEY: dict[str, ToolMeta] = {x.key: x for x in ALL_TOOLS}

#: operationId → ToolMeta 索引（底层调用使用）。
#: T24：同一 operationId 可被多个域的条目复用（quote/search 被 hk 占位复用、
#: klines/snapshot 被 commodities 占位复用）。规范条目的选取是**显式规则**：
#: 跨域（cross）条目优先（klines/snapshot 是任意域可用的通用端点），
#: 否则取先注册者（quote/search 的规范条目是 A 股原生的 quote/search）；
#: 其余条目进 ``SHARED_BY_NAME``——不再让"后注册者静默胜出"。
BY_NAME: dict[str, ToolMeta] = {}

#: operationId → 复用同一 operationId 的**非规范（占位）条目**列表（T24）。
SHARED_BY_NAME: dict[str, list[ToolMeta]] = {}

for _t in ALL_TOOLS:
    _canonical = BY_NAME.get(_t.tool_name)
    if _canonical is None or (_t.domain == "cross" and _canonical.domain != "cross"):
        if _canonical is not None:
            # 规范条目被 cross 条目取代，旧条目降级为共享占位
            SHARED_BY_NAME.setdefault(_t.tool_name, []).append(_canonical)
        BY_NAME[_t.tool_name] = _t
    else:
        SHARED_BY_NAME.setdefault(_t.tool_name, []).append(_t)

# 保留向后兼容别名
BY_KEY_CORE = BY_KEY
BY_NAME_CORE = BY_NAME

#: 向后兼容 — 旧代码使用 CORE_TOOLS 变量名
CORE_TOOLS = ALL_TOOLS

# ── P3 category 索引 ──────────────────────────────────────────────
#: category → list[ToolMeta] 索引（各 analyst 节点通过此索引发现工具）
by_category: dict[str, list[ToolMeta]] = {}
for _t in ALL_TOOLS:
    by_category.setdefault(_t.category, []).append(_t)


def tools_by_category(category: str) -> list[ToolMeta]:
    """获取指定分析员类别的所有工具。"""
    return by_category.get(category, [])


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
        # T24：按注释的承诺实现——unknown（health 工具）**始终附加**，不受域名限制；
        # 删掉旧的 `or ALL_TOOLS`：域过滤为空时静默回退成全量注册表。
        # （历史上 us_stock 曾因未接入工具而拿到全部 40 个工具，即此行为要修的 bug。）
        filtered = [t for t in ALL_TOOLS if t.domain in domains and t.domain != "unknown"]
        health = [t for t in ALL_TOOLS if t.domain == "unknown"]
        if not filtered:
            logger.warning(
                "registry_text: no tools for domains=%s — returning health tools only",
                domains,
            )
        tools = filtered + health

    return "\n".join(f"- {x.key}: {x.tool_name} [{x.domain}] — {x.purpose} [{x.priority}]" for x in tools)


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
    """返回当前**可作为研究目标**的市场域列表。

    排除两类：
      - ``unknown``：health / market_health 这类自检工具，不是市场
      - ``cross``：klines / snapshot / window 是"任何域都能用"的工具标记，
        不是一个可以研究的市场。它也不在 MarketDomain 的字面量里。

    以前只排除 unknown，于是 "cross" 混进了这个列表，而 main.py 拿它当
    /api/ask 的 domain 白名单 —— 结果 ``{"question":"贵州茅台怎么样",
    "domain":"cross"}`` 能通过校验，落进兜底的 crypto 分支去研究 BTC/USDT，
    同时 daily state 里还会多出一个非法的 "cross" 键。
    """
    seen: set[str] = set()
    for t in ALL_TOOLS:
        if t.domain not in ("unknown", "cross"):
            seen.add(t.domain)
    return sorted(seen)
