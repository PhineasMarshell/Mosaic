"""Tests for Anomaly Detection System.

T22 契约：
- 规则按 **精确别名表** 匹配 metric（'oi' 这类子串不再命中 noise / openInterestRate）；
- OI 规则语义是 **对前值的 % 变化**：拿不到前值（或前值相同）就不触发——
  真实 OI ≥1e9，旧实现直接拿绝对值比百分比阈值，每条快照都误报 critical；
- fundingRate 语义是 **小数比率 → 百分比**（0.002 = 0.2%），0.0001（0.01%）不触发；
- 同一 datum 同一 rule_type 只报最高严重级（不再 high + critical 报两条）。

fixture 一律使用 normalizer 会产出的**真实量级**值（OI 1e9 级、fundingRate 小数、
清算金额美元），不再手搓百分比假值。
"""

import pytest

from app.cache import market_cache
from app.detector.anomaly import (
    ALL_RULES,
    AnomalyRecord,
    _extract_numeric,
    _matches_metric,
    detect_anomalies,
)
from app.models.market import NormalizedDatum, ToolResult


@pytest.fixture(autouse=True)
def _clear_anomaly_prev_cache():
    """OI 前值存放在全局 market_cache —— 不清会跨用例泄漏，导致触发与否取决于用例顺序。"""
    market_cache.clear()
    yield
    market_cache.clear()


# ── _extract_numeric Tests ────────────────


class TestExtractNumeric:
    @pytest.mark.parametrize(
        "input_val,expected",
        [
            (42, 42.0),
            (3.14, 3.14),
            (-7.5, -7.5),
            ("123", 123.0),
            ("$1.2M", 1_200_000.0),
            ("$1.5B", 1_500_000_000.0),
            ("1,234,567", 1_234_567.0),
            ("invalid", None),
            ("", None),
            (None, None),
        ],
    )
    def test_numeric_extraction(self, input_val, expected):
        assert _extract_numeric(input_val) == expected

    def test_dict_and_list_return_none(self):
        assert _extract_numeric({"key": "value"}) is None
        assert _extract_numeric([1, 2, 3]) is None


# ── _matches_metric Tests（T22：精确别名匹配）─────────────


class TestMatchesMetric:
    def setup_method(self):
        class MockRule:
            metric_pattern = "openInterest|open_interest|totalOi"

        self.rule = MockRule()

    @pytest.mark.parametrize("metric", ["openInterest", "open_interest", "OpenInterest", "totalOi"])
    def test_exact_alias_matches(self, metric):
        assert _matches_metric(self.rule, metric) is True

    @pytest.mark.parametrize("metric", ["noise", "openInterestRate", "price", "fundingRate", "volume"])
    def test_substring_no_longer_matches(self, metric):
        # 旧实现：'oi' in metric.lower() → noise / openInterestRate 全部误命中
        assert _matches_metric(self.rule, metric) is False

    def test_empty_metric_returns_false(self):
        assert _matches_metric(self.rule, "") is False


# ── AnomalyRecord Serialization ──────────


class TestAnomalyRecord:
    def test_to_dict(self):
        record = AnomalyRecord(
            id="anomaly-001",
            type="oi_spike",
            domain="crypto",
            severity="high",
            description="OI increased 8.2%",
            metric="openInterest",
            value=8.2,
            normal_range="< ±5%",
            possible_meaning="Leverage building up",
            status="investigating",
            timestamp="2025-09-05 14:32",
        )
        d = record.to_dict()
        assert d["id"] == "anomaly-001"
        assert d["severity"] == "high"
        assert isinstance(d, dict)


# ── Integration: detect_anomalies ────────


def _result(*datums: NormalizedDatum) -> ToolResult:
    return ToolResult(
        tool="derivatives",
        arguments={},
        status="success",
        normalized=list(datums),
    )


class TestDetectCryptoAnomalies:
    def test_real_magnitude_oi_without_prev_never_triggers(self):
        """真实量级 OI（3.2e9）无前值 → 不触发（旧实现报 critical + high）。"""
        anomalies = detect_anomalies([_result(NormalizedDatum(metric="openInterest", value=3.2e9, tool="d"))], domain="crypto")
        assert anomalies == []

    def test_oi_same_as_prev_does_not_trigger(self):
        """前值相同 → 0% 变化 → 不触发。"""
        r1 = _result(NormalizedDatum(metric="openInterest", value=3.2e9, tool="d"))
        r2 = _result(NormalizedDatum(metric="openInterest", value=3.2e9, tool="d"))
        assert detect_anomalies([r1, r2], domain="crypto") == []

    def test_oi_spike_percent_change_triggers_highest_severity_only(self):
        """前值 1e9 → 现值 1.2e9（+20%）→ 只报一条 critical（不再 high+critical 报两条）。"""
        prev = _result(NormalizedDatum(metric="openInterest", value=1e9, tool="d"))
        curr = _result(NormalizedDatum(metric="openInterest", value=1.2e9, tool="d"))
        anomalies = detect_anomalies([prev, curr], domain="crypto")

        assert len(anomalies) == 1
        assert anomalies[0].severity == "critical"
        assert anomalies[0].type == "oi_spike"
        assert "20.0%" in anomalies[0].description

    def test_oi_moderate_spike_triggers_high(self):
        """+6% → high（不超过 critical 的 10% 阈值）。"""
        prev = _result(NormalizedDatum(metric="openInterest", value=1e9, tool="d"))
        curr = _result(NormalizedDatum(metric="openInterest", value=1.06e9, tool="d"))
        anomalies = detect_anomalies([prev, curr], domain="crypto")
        assert [a.severity for a in anomalies] == ["high"]

    def test_funding_rate_percent_semantics(self):
        """fundingRate 0.002（0.2%）→ high；0.0001（0.01%）→ 不触发。"""
        high = detect_anomalies([_result(NormalizedDatum(metric="fundingRate", value=0.002, tool="d"))], domain="crypto")
        assert [a.severity for a in high] == ["high"]
        assert "0.2000%" in high[0].description

        quiet = detect_anomalies([_result(NormalizedDatum(metric="fundingRate", value=0.0001, tool="d"))], domain="crypto")
        assert quiet == []

    def test_funding_rate_critical(self):
        """0.006（0.6% > 0.5%）→ critical（单条，去重）。"""
        anomalies = detect_anomalies([_result(NormalizedDatum(metric="fundingRate", value=0.006, tool="d"))], domain="crypto")
        assert [a.severity for a in anomalies] == ["critical"]

    def test_liquidation_absolute_semantics_unchanged(self):
        """清算金额是绝对量：$6M → high（语义本来就正确，回归保护）。"""
        anomalies = detect_anomalies([_result(NormalizedDatum(metric="totalLiqValue", value=6_000_000, tool="d"))], domain="crypto")
        assert [a.severity for a in anomalies] == ["high"]

    def test_noise_metric_never_triggers(self):
        """metric='noise' 含 'oi' 子串但值再大也不触发（精确匹配）。"""
        anomalies = detect_anomalies([_result(NormalizedDatum(metric="noise", value=3.2e9, tool="d"))], domain="crypto")
        assert anomalies == []

    def test_no_false_positive_on_normal_values(self):
        """正常量级快照：OI 无前值、fundingRate 0.0001 → 无 high/critical。"""
        results = [
            _result(
                NormalizedDatum(metric="openInterest", value=2.0e9, tool="d"),
                NormalizedDatum(metric="fundingRate", value=0.0001, tool="d"),
            )
        ]
        anomalies = detect_anomalies(results, domain="crypto")
        assert [a for a in anomalies if a.severity in ("high", "critical")] == []


class TestDetectAshareAnomalies:
    def test_limit_up_surge(self):
        results = [_result(NormalizedDatum(metric="涨停家数", value=85, tool="a", domain="a_share"))]
        anomalies = detect_anomalies(results, domain="a_share")
        surge_found = [a for a in anomalies if a.type == "limit_up_surge"]
        assert len(surge_found) == 1

    def test_limit_up_collapse(self):
        results = [_result(NormalizedDatum(metric="涨停家数", value=15, tool="a", domain="a_share"))]
        anomalies = detect_anomalies(results, domain="a_share")
        collapse_found = [a for a in anomalies if a.type == "limit_up_collapse"]
        assert len(collapse_found) == 1

    def test_market_breadth_decline(self):
        results = [_result(NormalizedDatum(metric="下跌家数", value=3500, tool="a", domain="a_share"))]
        anomalies = detect_anomalies(results, domain="a_share")
        breadth_found = [a for a in anomalies if a.type == "sentiment_change"]
        assert len(breadth_found) == 1

    def test_empty_results(self):
        assert detect_anomalies([], domain="a_share") == []

    def test_no_normalized_data(self):
        anomalies = detect_anomalies([_result()], domain="crypto")
        assert anomalies == []


class TestSeveritySorting:
    def test_sorted_by_severity(self):
        """critical 应排在 high 前面（funding critical + 清算 high 组合）。"""
        results = [
            _result(
                NormalizedDatum(metric="fundingRate", value=0.006, tool="d"),  # → critical
                NormalizedDatum(metric="totalLiqValue", value=6_000_000, tool="d"),  # → high
            )
        ]
        anomalies = detect_anomalies(results, domain="crypto")
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        sevs = [severity_order[a.severity] for a in anomalies]
        assert sevs == sorted(sevs), "Anomalies not sorted by severity"


class TestRuleCount:
    def test_rules_exist(self):
        crypto_rules = [r for r in ALL_RULES if r.domain == "crypto"]
        ashare_rules = [r for r in ALL_RULES if r.domain == "a_share"]
        assert len(crypto_rules) > 0, "No crypto rules defined"
        assert len(ashare_rules) > 0, "No A-share rules defined"
