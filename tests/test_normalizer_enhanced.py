"""Additional tests for app.gateway.normalizer module."""

from app.gateway.normalizer import (
    _extract_metrics,
    _infer_domain,
    _make_summary,
    normalize_tool_result,
)
from app.models.market import NormalizedDatum


class TestNormalizeToolResult:
    """测试正常化入口函数的行为。"""

    def test_error_path(self):
        result = normalize_tool_result(
            "overview", {}, None, error="connection refused"
        )
        assert result.status == "error"
        assert result.error == "connection refused"
        assert result.partial is False
        assert len(result.normalized) == 0

    def test_flat_dict_extraction(self):
        raw = {"sentiment": 54, "timestamp": "2026-09-04"}
        result = normalize_tool_result("sentiment_tool", {}, raw)
        assert result.status == "success"
        assert result.partial is False
        assert len(result.normalized) > 0
        # sentiment 应该被提取
        metrics = [d.metric for d in result.normalized]
        assert "sentiment" in metrics

    def test_partial_flag_propagation(self):
        raw = {"data": [1, 2, 3], "partial": True, "note": "history incomplete"}
        result = normalize_tool_result("klines", {}, raw)
        assert result.status == "partial"
        assert result.partial is True

    def test_nested_container_expansion(self):
        """验证容器键（如 data/items/candles）的子元素被正确展开。"""
        raw = {
            "items": [
                {"symbol": "SH600519", "price": 1272},
                {"symbol": "SZ000001", "price": 18.5},
            ]
        }
        result = normalize_tool_result("quote", {}, raw)
        assert len(result.normalized) > 0
        # 应该有从 items 列表中提取的元素
        all_values = [str(d.value) for d in result.normalized]
        combined = " ".join(all_values)
        assert "SH600519" in combined or "SZ000001" in combined

    def test_non_dict_response(self):
        raw = "simple string response"
        result = normalize_tool_result("foo", {}, raw)
        assert len(result.normalized) == 1
        assert result.normalized[0].metric == "response"


class TestExtractMetrics:
    """测试递归指标抽取逻辑。"""

    def test_simple_kv(self):
        result = _extract_metrics({"count": 42, "name": "test"})
        assert len(result) == 2
        metrics = {d.metric: d.value for d in result}
        assert metrics["count"] == 42
        assert metrics["name"] == "test"

    def test_container_list(self):
        result = _extract_metrics({
            "items": [{"a": 1}, {"b": 2}],
        })
        assert len(result) >= 2
        # 容器展开后生成 items[0], items[1] 以及嵌套对象的指标 a, b
        assert any(d.metric.startswith("items[") for d in result)

    def test_deeply_nested(self):
        """验证深层嵌套也能被提取到部分指标。"""
        data = {
            "level1": {
                "level2": {
                    "value": 123,
                    "other": "text",
                },
                "meta": {"source": "api"},
            }
        }
        result = _extract_metrics(data)
        assert len(result) > 0

    def test_none_values_skipped(self):
        result = _extract_metrics({"a": 1, "b": None, "c": 2})
        metrics = {d.metric: d.value for d in result}
        assert "b" not in metrics


class TestInferDomain:
    """测试域名推断逻辑。"""

    def test_ashare_tools(self):
        tools = ["ashare_sentiment_get", "xueqiu_quote_get",
                 "tencent_price_get", "eastmoney_finance_get"]
        for tool in tools:
            assert _infer_domain(tool) == "a_share"

    def test_crypto_tools(self):
        tools = ["market_klines_post", "coinglass_funding_get",
                 "hyperliquid_symbols_get"]
        for tool in tools:
            assert _infer_domain(tool) == "crypto"

    def test_unknown_domain(self):
        assert _infer_domain("unknown_tool_xyz") == "unknown"


class TestMakeSummary:
    """测试复杂对象的摘要生成。"""

    def test_list_summary(self):
        s = _make_summary([1, 2, 3, 4, 5])
        assert "list:5 items" in s

    def test_dict_summary(self):
        s = _make_summary({"a": 1, "b": 2, "c": 3})
        assert "object:" in s

    def test_string_passthrough(self):
        s = _make_summary("plain text")
        assert s == "plain text"
