"""tests/test_cache_multi_domain.py — 多域缓存策略测试。"""

import pytest

from app.cache import (
    DERIVATIVES_TTL,
    EXCHANGES_TTL,
    FUNDING_RATE_TTL,
    KLINE_TTL,
    LIQUIDATION_TTL,
    LIQMAP_TTL,
    QUOTE_TTL,
    SENTIMENT_TTL,
    SNAPSHOT_TTL,
    TOP_POSITION_TTL,
    _resolve_ttl,
)


class TestAssetTTLs:
    """验证各常量 TTL 定义合理。"""

    def test_quote_ttl_short(self):
        assert QUOTE_TTL <= 15  # 实时行情短缓存

    def test_sentiment_ttl_medium(self):
        assert 30 <= SENTIMENT_TTL <= 120  # 情绪中缓存

    def test_snapshot_ttl_crypto(self):
        assert SNAPSHOT_TTL <= 30  # Crypto 快照短缓存

    def test_kline_ttl(self):
        assert KLINE_TTL >= 30  # K 线至少 30 秒

    def test_derivatives_ttl_longer_than_kline(self):
        assert DERIVATIVES_TTL >= KLINE_TTL

    def test_funding_rate_teven_longer(self):
        assert FUNDING_RATE_TTL >= DERIVATIVES_TTL

    def test_liquidation_evt_driven_short(self):
        assert LIQUIDATION_TTL <= 60  # 事件驱动型，较短

    def test_exchanges_hourly(self):
        assert EXCHANGES_TTL >= 1800  # 交易所列表小时级


class TestTTLResolution:
    """验证按工具名解析 TTL。"""

    def test_resolve_ashare_tools(self):
        assert _resolve_ttl("quote_tencent_quote_get") == QUOTE_TTL
        assert _resolve_ttl(
            "public_sentiment_ashare_master_sentiment_get"
        ) == SENTIMENT_TTL
        assert _resolve_ttl(
            "public_limit_up_count_ashare_master_limit_up_count_get"
        ) > 0

    def test_resolve_crypto_tools(self):
        assert _resolve_ttl("snapshot_market_snapshot_post") == SNAPSHOT_TTL
        assert _resolve_ttl("klines_market_klines_post") == KLINE_TTL
        assert _resolve_ttl(
            "derivatives_history_market_derivatives_history_post"
        ) == DERIVATIVES_TTL
        assert _resolve_ttl(
            "funding_rate_coinglass_funding_rate_get"
        ) == FUNDING_RATE_TTL
        assert _resolve_ttl(
            "liquidation_today_coinglass_liquidation_today_get"
        ) == LIQUIDATION_TTL
        assert _resolve_ttl(
            "hyperliquid_liqmap_coinglass_hyperliquid_liqmap_get"
        ) == LIQMAP_TTL
        assert _resolve_ttl(
            "exchanges_market_exchanges_get"
        ) == EXCHANGES_TTL

    def test_unknown_tool_gets_default(self):
        default_ttl = _resolve_ttl("nonexistent_tool_xyz")
        assert default_ttl == 30.0
