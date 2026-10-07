"""tests/test_cache_multi_domain.py — 多域缓存策略测试。"""

from app.cache import (
    DERIVATIVES_TTL,
    EXCHANGES_TTL,
    FUNDING_RATE_TTL,
    KLINE_TTL,
    LIQMAP_TTL,
    LIQUIDATION_TTL,
    QUOTE_TTL,
    SENTIMENT_TTL,
    SNAPSHOT_TTL,
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
        assert _resolve_ttl("get_market_quotes") == QUOTE_TTL
        assert _resolve_ttl("get_ashare_sentiment") == SENTIMENT_TTL
        assert _resolve_ttl("get_limit_up_count") > 0

    def test_resolve_crypto_tools(self):
        assert _resolve_ttl("get_market_snapshot") == SNAPSHOT_TTL
        assert _resolve_ttl("get_market_klines") == KLINE_TTL
        assert _resolve_ttl("get_derivatives_history") == DERIVATIVES_TTL
        assert _resolve_ttl("list_crypto_funding_rates") == FUNDING_RATE_TTL
        assert _resolve_ttl("get_crypto_liquidation_today") == LIQUIDATION_TTL
        assert _resolve_ttl("get_hyperliquid_liquidation_map") == LIQMAP_TTL
        assert _resolve_ttl("list_exchanges") == EXCHANGES_TTL

    def test_unknown_tool_gets_default(self):
        default_ttl = _resolve_ttl("nonexistent_tool_xyz")
        assert default_ttl == 30.0
