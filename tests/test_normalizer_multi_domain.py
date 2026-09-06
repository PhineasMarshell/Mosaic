"""tests/test_normalizer_multi_domain.py — 跨域归一化测试。"""

import pytest

from app.gateway.normalizer import (
    _extract_metrics,
    find_partial,
    find_timestamp,
    infer_domain_from_tool,
    normalize_tool_result,
)


class TestDomainInference:
    """验证域名推断逻辑。"""

    def test_ashare_sentiment(self):
        assert infer_domain_from_tool(
            "public_sentiment_ashare_master_sentiment_get"
        ) == "a_share"

    def test_ashare_limit_up(self):
        assert infer_domain_from_tool(
            "public_limit_up_count_ashare_master_limit_up_count_get"
        ) == "a_share"

    def test_ashare_eastmoney(self):
        assert infer_domain_from_tool("overview_eastmoney_overview_get") == "a_share"

    def test_crypto_snapshot(self):
        # snapshot 现在是跨域通用 (domain="cross")
        assert infer_domain_from_tool("snapshot_market_snapshot_post") == "cross"

    def test_crypto_klines(self):
        # klines 现在是跨域通用 (domain="cross")
        assert infer_domain_from_tool("klines_market_klines_post") == "cross"

    def test_crypto_derivatives(self):
        assert infer_domain_from_tool(
            "derivatives_history_market_derivatives_history_post"
        ) == "crypto"

    def test_crypto_coinglass(self):
        assert infer_domain_from_tool(
            "funding_rate_coinglass_funding_rate_get"
        ) == "crypto"

    def test_crypto_hyperliquid(self):
        assert infer_domain_from_tool(
            "hyperliquid_top_position_coinglass_hyperliquid_top_position_get"
        ) == "crypto"

    def test_unknown_tool_unknown_domain(self):
        # 不在注册表中的工具，启发式推断回退到 unknown
        result = infer_domain_from_tool("unknown_external_provider_quote")
        assert result in ("unknown", "us_stock")


class TestTimestampDetection:
    """验证时间戳探测。"""

    def test_direct_timestamp_key(self):
        obj = {"timestamp": 1725484800000, "data": [1, 2]}
        ts = find_timestamp(obj)
        assert ts == "1725484800000"

    def test_time_key(self):
        obj = {"time": "2026-09-04T12:00:00Z"}
        assert find_timestamp(obj) is not None

    def test_open_time_crypto(self):
        obj = {"open_time": 1725484800000, "close": 30000}
        ts = find_timestamp(obj)
        assert ts == "1725484800000"

    def test_nested_meta_timestamp(self):
        obj = {
            "meta": {"timestamp": "2026-09-04"},
            "values": [1, 2, 3],
        }
        assert find_timestamp(obj) == "2026-09-04"

    def test_no_timestamp(self):
        assert find_timestamp({"price": 100}) is None
        assert find_timestamp([1, 2, 3]) is None


class TestPartialDetection:
    """验证 partial 检测。"""

    def test_partial_true(self):
        assert find_partial({"data": [], "partial": True}) is True

    def test_partial_false_explicit(self):
        assert find_partial({"data": [], "partial": False}) is False

    def test_partial_absent(self):
        assert find_partial({"data": []}) is False

    def test_partial_non_dict(self):
        assert find_partial([1, 2, 3]) is False


class TestCryptoNormalize:
    """验证 Crypto 数据规范化（跨域通用工具也适用于其他域）。"""

    def test_crypto_snapshot_result(self):
        raw = {
            "symbol": "BTCUSDT",
            "price": 65000.50,
            "volume24h": 12345.67,
            "change_pct": 2.3,
            "source_used": "binance",
        }
        result = normalize_tool_result(
            "snapshot_market_snapshot_post", {}, raw
        )
        assert result.status == "success"
        # snapshot 现在是跨域通用 (domain="cross")
        assert result.normalized[0].domain == "cross"
        metrics = [d.metric for d in result.normalized]
        assert any("price" in m for m in metrics)
        assert any("volume" in m for m in metrics)

    def test_crypto_klines_array(self):
        raw = [
            {"open_time": 1725484800000, "open": 60000, "high": 61000, "low": 59500, "close": 60500},
            {"open_time": 1725488400000, "open": 60500, "high": 61500, "low": 60000, "close": 61000},
        ]
        result = normalize_tool_result("klines_market_klines_post", {}, raw)
        assert result.status == "success"
        # klines 现在是跨域通用 (domain="cross")
        assert result.normalized[0].domain == "cross"
        # K 线数组应被展开为多个条目
        assert len(result.normalized) >= 2


class TestCryptoDerivativesNormalize:
    """验证 Crypto 衍生品数据规范化（独立测试类避免缩进问题）。"""

    def test_derivative_data_fields(self):
        raw = {
            "symbol": "BTCUSDT",
            "fundingRate": 0.0001,
            "indexPrice": 65000.0,
            "markPrice": 65010.0,
            "openInterest": 500000000.0,
            "partial": False,
        }
        result = normalize_tool_result(
            "derivatives_history_market_derivatives_history_post", {}, raw
        )
        assert result.status == "success"
        metrics = {d.metric: d.value for d in result.normalized}
        assert "fundingRate" in metrics or "funding_rate" in metrics


class TestErrorHandling:
    """验证错误处理。"""

    def test_error_returns_error_status(self):
        result = normalize_tool_result(
            "some_tool", {}, None, error="Connection refused"
        )
        assert result.status == "error"
        assert result.error == "Connection refused"
        assert len(result.normalized) == 0

    def test_partial_data_returns_partial_status(self):
        raw = {"data": [1], "partial": True}
        result = normalize_tool_result("some_tool", {}, raw)
        assert result.status == "partial"
        assert result.partial is True
