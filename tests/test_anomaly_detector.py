"""Tests for Anomaly Detection System.

Coverage:
- Rule matching (metric pattern)
- Numeric extraction from various formats
- Crypto anomaly detection rules
- A-share anomaly detection rules
- Severity sorting
- AnomalyRecord serialization
"""

import pytest

from app.detector.anomaly import (
    AnomalyRecord,
    ALL_RULES,
    _extract_numeric,
    _matches_metric,
    detect_anomalies,
)


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


# ── _matches_metric Tests ─────────────────


class TestMatchesMetric:
    def setup_method(self):
        # Create a mock rule for testing
        class MockRule:
            metric_pattern = "openInterest|open_interest|oi"

        self.rule = MockRule()

    @pytest.mark.parametrize(
        "metric",
        ["openInterest", "open_interest", "oi", "totalOi"],
    )
    def test_matches_crypto_oi_patterns(self, metric):
        assert _matches_metric(self.rule, metric) is True

    @pytest.mark.parametrize(
        "metric",
        ["price", "fundingRate", "volume"],
    )
    def test_no_match_wrong_metric(self, metric):
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


class MockDatum:
    def __init__(self, metric, value, domain="crypto"):
        self.metric = metric
        self.value = value
        self.domain = domain


class MockResult:
    def __init__(self, datums):
        self.normalized = datums
        self.tool = "test_tool"


class TestDetectCryptoAnomalies:
    def test_oi_spike_detected(self):
        results = [MockResult([MockDatum("openInterest", 8.5)])]
        anomalies = detect_anomalies(results, domain="crypto")
        oi_found = [a for a in anomalies if a.type == "oi_spike"]
        assert len(oi_found) >= 1
        assert oi_found[0].severity == "high"

    def test_critical_oi_spike(self):
        results = [MockResult([MockDatum("openInterest", 12.0)])]
        anomalies = detect_anomalies(results, domain="crypto")
        critical_found = [a for a in anomalies if a.severity == "critical"]
        assert len(critical_found) >= 1

    def test_funding_rate_high(self):
        results = [MockResult([MockDatum("fundingRate", 0.3)])]
        anomalies = detect_anomalies(results, domain="crypto")
        funding_found = [a for a in anomalies if a.type == "funding_rate_anomaly"]
        assert len(funding_found) >= 1

    def test_liquidation_detection(self):
        results = [MockResult([MockDatum("totalLiqValue", 6_000_000)])]
        anomalies = detect_anomalies(results, domain="crypto")
        liq_found = [a for a in anomalies if a.type == "liquidation_event"]
        assert len(liq_found) >= 1

    def test_no_false_positive_on_normal_values(self):
        results = [MockResult([
            MockDatum("openInterest", 2.0),   # below threshold
            MockDatum("fundingRate", 0.02),    # below threshold
        ])]
        anomalies = detect_anomalies(results, domain="crypto")
        # Should have no HIGH or CRITICAL crypto anomalies
        high_sev = [a for a in anomalies if a.severity in ("high", "critical")]
        assert len(high_sev) == 0


class TestDetectAshareAnomalies:
    def test_limit_up_surge(self):
        results = [MockResult([MockDatum("涨停家数", 85)])]
        anomalies = detect_anomalies(results, domain="a_share")
        surge_found = [a for a in anomalies if a.type == "limit_up_surge"]
        assert len(surge_found) >= 1

    def test_limit_up_collapse(self):
        results = [MockResult([MockDatum("涨停家数", 15)])]
        anomalies = detect_anomalies(results, domain="a_share")
        collapse_found = [a for a in anomalies if a.type == "limit_up_collapse"]
        assert len(collapse_found) >= 1

    def test_market_breadth_decline(self):
        results = [MockResult([MockDatum("下跌家数", 3500)])]
        anomalies = detect_anomalies(results, domain="a_share")
        breadth_found = [a for a in anomalies if a.type == "sentiment_change"]
        assert len(breadth_found) >= 1

    def test_empty_results(self):
        results = []
        anomalies = detect_anomalies(results, domain="a_share")
        assert anomalies == []

    def test_no_normalized_data(self):
        results = [MockResult([])]
        anomalies = detect_anomalies(results, domain="crypto")
        assert anomalies == []


class TestSeveritySorting:
    def test_sorted_by_severity(self):
        """Anomalies should be sorted: critical > high > medium > low."""
        # This implicitly tests sorting if any combination of thresholds triggers
        results = [MockResult([
            MockDatum("openInterest", 8.0),      # → high
            MockDatum("fundingRate", 0.6),        # → critical
        ])]
        anomalies = detect_anomalies(results, domain="crypto")
        # Critical should come before high
        if len(anomalies) >= 2:
            severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
            sevs = [severity_order[a.severity] for a in anomalies]
            assert sevs == sorted(sevs), "Anomalies not sorted by severity"


class TestRuleCount:
    def test_rules_exist(self):
        crypto_rules = [r for r in ALL_RULES if r.domain == "crypto"]
        ashare_rules = [r for r in ALL_RULES if r.domain == "a_share"]
        assert len(crypto_rules) > 0, "No crypto rules defined"
        assert len(ashare_rules) > 0, "No A-share rules defined"
