"""B4 硬闸门：Market Gateway 3.2.1 改名迁移回归（防事故复发）。

背景（2026-10-06 事故）：上游 market-gateway 升到 3.2.1 后全部 operationId 改名，
Mosaic 注册表里 40 个旧 operationId 全部失效，MCP 模式下全部工具 100% 失败
（``KeyError: Tool is not allowed by registry``），而测试全绿——mock 掩盖了链路层。

本文件把 §1.2 映射表的旧名列**全量硬编码**为黑名单：
1. 注册表（含缓存 TTL 表、白名单等所有引用点）里**不存在任何旧 operationId**；
2. 全部新 operationId 可 ``resolve_tool_by_name``；
3. 共享 operationId 的 BY_NAME/SHARED_BY_NAME 关系在改名后仍成立。
"""

import pytest

from app.cache import _resolve_ttl
from app.gateway.tool_registry import ALL_TOOLS, BY_NAME, SHARED_BY_NAME, resolve_tool_by_name

# §1.2 映射表的旧 operationId 全量黑名单（唯一名集合，32 个）。
# 若上游再次改名：把新映射的「旧名」追加到这里，并把新名加进 NEW_TOOL_NAMES。
LEGACY_OPERATION_IDS = frozenset(
    [
        "public_sentiment_ashare_master_sentiment_get",
        "public_limit_up_count_ashare_master_limit_up_count_get",
        "public_limit_up_sectors_ashare_master_limit_up_sectors_get",
        "public_limit_up_pool_ashare_master_limit_up_pool_get",
        "overview_eastmoney_overview_get",
        "detail_eastmoney_detail_get",
        "business_eastmoney_f10_business_get",
        "concept_eastmoney_f10_concept_get",
        "finance_eastmoney_f10_finance_get",
        "shareholders_eastmoney_f10_shareholders_get",
        "survey_eastmoney_f10_survey_get",
        "quote_tencent_quote_get",
        "longhu_xueqiu_longhu_get",
        "abnormal_reasons_xueqiu_abnormal_reasons_get",
        "orderbook_xueqiu_orderbook_get",
        "trades_xueqiu_trades_get",
        "timeline_xueqiu_timeline_get",
        "search_xueqiu_search_get",
        "klines_market_klines_post",
        "snapshot_market_snapshot_post",
        "window_market_window_post",
        "exchanges_market_exchanges_get",
        "derivatives_history_market_derivatives_history_post",
        "hyperliquid_symbols_coinglass_hyperliquid_symbols_get",
        "hyperliquid_liqmap_coinglass_hyperliquid_liqmap_get",
        "hyperliquid_top_position_coinglass_hyperliquid_top_position_get",
        "hyperliquid_user_count_coinglass_hyperliquid_user_count_get",
        "hyperliquid_vaults_coinglass_hyperliquid_vaults_get",
        "liquidation_today_coinglass_liquidation_today_get",
        "funding_rate_coinglass_funding_rate_get",
        "health_health_get",
        "health_market_health_get",
    ]
)

#: 3.2.1 新 operationId 全集（MCP 工具名逐字匹配）。
NEW_TOOL_NAMES = frozenset(
    [
        "get_ashare_sentiment",
        "get_limit_up_count",
        "list_limit_up_sectors",
        "list_limit_up_stocks",
        "get_company_overview",
        "get_company_detail",
        "get_company_business",
        "get_company_concepts",
        "get_company_finance",
        "get_company_shareholders",
        "get_company_survey",
        "get_market_quotes",
        "get_stock_longhu",
        "get_stock_abnormal_reasons",
        "get_market_orderbook",
        "list_market_trades",
        "list_stock_discussions",
        "search_stocks",
        "get_market_klines",
        "get_market_snapshot",
        "get_market_window",
        "list_exchanges",
        "get_derivatives_history",
        "list_hyperliquid_symbols",
        "get_hyperliquid_liquidation_map",
        "get_hyperliquid_top_position",
        "get_hyperliquid_user_count",
        "list_hyperliquid_vaults",
        "get_crypto_liquidation_today",
        "list_crypto_funding_rates",
        "get_service_health",
        "get_market_health",
    ]
)

#: 保持不动的内部工具 / news（非 Gateway operationId）。
UNTOUCHED_TOOL_NAMES = frozenset(
    [
        "internal_hk_northbound",
        "internal_hk_index",
        "internal_us_fundamentals",
        "internal_us_filings_recent",
        "news_search",
    ]
)


def test_registry_has_no_legacy_operation_id():
    """注册表里不存在任何旧 operationId —— 本条就是防 3.2.1 事故复发的闸门。"""
    present = {t.tool_name for t in ALL_TOOLS}
    stale = sorted(present & LEGACY_OPERATION_IDS)
    assert stale == [], f"注册表残留旧 operationId（3.2.1 上必然 403/KeyError）: {stale}"
    assert not (set(BY_NAME) & LEGACY_OPERATION_IDS), "BY_NAME 索引残留旧 operationId"


def test_registry_has_no_legacy_name_in_shared_index():
    assert not (set(SHARED_BY_NAME) & LEGACY_OPERATION_IDS), "SHARED_BY_NAME 索引残留旧 operationId"


def test_all_new_operation_ids_resolve():
    for name in sorted(NEW_TOOL_NAMES):
        meta = resolve_tool_by_name(name)  # KeyError 即失败
        assert meta.domain != ""


def test_tool_counts_unchanged():
    """3.2.1 改名轮只改名不增删；新闻面多源接入（A3）后 ALL_TOOLS 44→47。

    44→47 = 新增 symbol_news / telegraph / news_digest 三条 INTERNAL 新闻工具。
    BY_NAME 37→40（同样 +3）；差额 7 仍来自共享 operationId（键唯一、name可复用）。
    """
    assert len(ALL_TOOLS) == 47
    assert len(BY_NAME) == 40
    assert len(NEW_TOOL_NAMES) == 32


def test_shared_operation_ids_survive_rename():
    """共享 operationId 改名后共享关系仍成立（§1.2 #23/#24/#25 与 quote/search）。"""
    for name, canonical_key, expect_shared in [
        ("get_market_klines", "klines", {"commodity_gold", "us_klines"}),
        ("get_market_snapshot", "snapshot", {"commodity_silver", "commodity_platinum"}),
        ("get_market_window", "window", {"us_window"}),
        ("get_market_quotes", "quote", {"hk_quote"}),
        ("search_stocks", "search", {"hk_search"}),
    ]:
        canonical = resolve_tool_by_name(name)
        assert canonical.key == canonical_key, f"{name}: 规范条目应为 cross/先注册者 {canonical_key}"
        shared_keys = {m.key for m in SHARED_BY_NAME[name]}
        assert shared_keys == expect_shared, f"{name}: 共享条目 {shared_keys} != {expect_shared}"


def test_new_paths_registered():
    """改名 + 改路径的 7 条：http_path 必须指向 3.2.1 新路径。"""
    expect = {
        "get_market_quotes": "/market/quotes",
        "get_stock_longhu": "/market/longhu",
        "get_stock_abnormal_reasons": "/market/abnormal-reasons",
        "get_market_orderbook": "/market/orderbook",
        "list_market_trades": "/market/trades",
        "list_stock_discussions": "/market/discussions",
        "search_stocks": "/market/search",
    }
    for name, path in expect.items():
        assert resolve_tool_by_name(name).http_path == path


def test_ttl_table_uses_new_operation_ids():
    """app/cache.py 的 TTL 表按 operationId 建键——改名后必须仍命中，静默退回默认 30s 即 bug。"""
    from app.cache import (
        DERIVATIVES_TTL,
        EXCHANGES_TTL,
        FUNDING_RATE_TTL,
        KLINE_TTL,
        LIMIT_UP_TTL,
        LIQMAP_TTL,
        LIQUIDATION_TTL,
        LONGHU_TTL,
        OVERVIEW_TTL,
        QUOTE_TTL,
        SENTIMENT_TTL,
        SNAPSHOT_TTL,
        TOP_POSITION_TTL,
    )

    expect = {
        "get_market_quotes": QUOTE_TTL,
        "get_ashare_sentiment": SENTIMENT_TTL,
        "get_limit_up_count": LIMIT_UP_TTL,
        "list_limit_up_sectors": LIMIT_UP_TTL,
        "list_limit_up_stocks": LIMIT_UP_TTL,
        "get_company_overview": OVERVIEW_TTL,
        "get_stock_longhu": LONGHU_TTL,
        "get_market_klines": KLINE_TTL,
        "get_market_snapshot": SNAPSHOT_TTL,
        "get_derivatives_history": DERIVATIVES_TTL,
        "list_crypto_funding_rates": FUNDING_RATE_TTL,
        "get_crypto_liquidation_today": LIQUIDATION_TTL,
        "get_hyperliquid_liquidation_map": LIQMAP_TTL,
        "get_hyperliquid_top_position": TOP_POSITION_TTL,
        "list_exchanges": EXCHANGES_TTL,
    }
    for name, ttl in expect.items():
        assert _resolve_ttl(name) == ttl, f"{name}: TTL 未命中映射（静默退回默认值）"
    for name in LEGACY_OPERATION_IDS:
        assert _resolve_ttl(name) == 30.0  # 旧名应落进默认值——存在即说明映射残留


def test_whitelist_no_symbol_uses_new_names():
    """WHITELIST_NO_SYMBOL 里不得残留旧名；本就无 symbol 的能力改名后仍在白名单。"""
    from app.graph.nodes.analysts.base import MarketAnalystNode

    whitelist = MarketAnalystNode.WHITELIST_NO_SYMBOL
    assert not (set(whitelist) & LEGACY_OPERATION_IDS)
    for name in [
        "get_ashare_sentiment",
        "get_limit_up_count",
        "list_limit_up_sectors",
        "list_limit_up_stocks",
        "list_hyperliquid_symbols",
        "get_hyperliquid_user_count",
        "list_hyperliquid_vaults",
        "list_exchanges",
        "get_service_health",
        "get_market_health",
        "news_search",
        "search_stocks",
        "get_stock_longhu",
        "internal_hk_northbound",
        "internal_hk_index",
    ]:
        assert name in whitelist, f"{name} 应留在无 symbol 白名单"
    # 计划 B1 步 6：这些工具必须提供 symbol，不得进白名单
    for name in ["list_stock_discussions", "get_market_quotes"]:
        assert name not in whitelist


def test_internal_and_news_tools_untouched():
    for name in UNTOUCHED_TOOL_NAMES:
        resolve_tool_by_name(name)


def test_no_chip_tools_registered_this_round():
    """B2：8 条 A 股筹码工具实测 coverage_status=not_collected，本轮不注册；
    list_stock_post_comments（留给情绪面计划）与 verify_upstream（诊断端点）同样不注册。"""
    present = {t.tool_name for t in ALL_TOOLS}
    not_registered = [
        "get_ashare_capital_flow",
        "list_ashare_holders",
        "list_ashare_reductions",
        "list_ashare_unlocks",
        "get_latest_ashare_capital_flow",
        "get_latest_ashare_holders",
        "get_latest_ashare_reductions",
        "get_latest_ashare_unlocks",
        "list_stock_post_comments",
        "verify_upstream",
    ]
    leaked = sorted(present & set(not_registered))
    assert leaked == [], f"本轮不应注册的工具混进了注册表: {leaked}"


def test_blacklist_and_new_names_are_disjoint():
    assert not (LEGACY_OPERATION_IDS & NEW_TOOL_NAMES)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
