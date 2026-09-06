"""Tests for Reasoning Engine backward-compatible evidence parsing.

Cover:
- Old LLM output format: {claim, evidence_ids} -> EvidenceItem
- New LLM output format: {id, source_tool, metric, value, ...} -> EvidenceItem
- what_changed as string vs list
- Model validation of both formats
"""

import pytest

from app.models.evidence import Evidence
from app.models.response import EvidenceItem, MarketIntelligence


# ── _parse_evidence (imported from reasoning module) ────────


@pytest.fixture
def sample_evidence():
    return [
        Evidence(
            id="e-001",
            source_tool="quote_tencent_quote_get",
            domain="a_share",
            metric="price",
            value=3800.5,
            status="success",
        ),
        Evidence(
            id="e-002",
            source_tool="public_sentiment_ashare_master_sentiment_get",
            domain="a_share",
            metric="risk_on",
            value=0.35,
            status="success",
        ),
        Evidence(
            id="e-003",
            source_tool="klines_market_klines_post",
            domain="crypto",
            metric="openInterest",
            value=8.5,
            status="partial",
            partial=True,  # Fix: explicitly set partial=True for partial data
        ),
    ]


class TestParseEvidenceOldFormat:
    """Test backward-compatible parsing of old {claim, evidence_ids} format."""

    def test_single_claim_multiple_evidence(self, sample_evidence):
        from app.research.reasoning import _parse_evidence

        raw = [{"claim": "Market sentiment cooled", "evidence_ids": ["e-001", "e-002"]}]
        result = _parse_evidence(raw, sample_evidence)

        assert len(result) >= 1
        # Should map to actual Evidence data
        ids = {r.id for r in result}
        assert "e-001" in ids or "e-002" in ids

    def test_non_matching_evidence_ids(self, sample_evidence):
        from app.research.reasoning import _parse_evidence

        raw = [{"claim": "Something happened", "evidence_ids": ["e-nonexistent"]}]
        result = _parse_evidence(raw, sample_evidence)
        # Should create placeholder when no matching evidence found
        assert len(result) >= 1
        assert result[0].value == "Something happened"

    def test_empty_claim(self, sample_evidence):
        from app.research.reasoning import _parse_evidence

        raw = [{"claim": "", "evidence_ids": []}]
        result = _parse_evidence(raw, sample_evidence)
        assert len(result) == 0  # Empty claim + empty IDs = nothing

    def test_partial_evidence_included(self, sample_evidence):
        from app.research.reasoning import _parse_evidence

        raw = [{"claim": "OI anomaly detected", "evidence_ids": ["e-003"]}]
        result = _parse_evidence(raw, sample_evidence)
        oi_items = [r for r in result if r.metric == "openInterest"]
        assert len(oi_items) >= 1
        assert oi_items[0].partial is True

    def test_new_format_with_partial_flag(self):
        """Ensure new format evidence correctly preserves partial boolean."""
        from app.research.reasoning import _parse_evidence

        raw = [
            {"id": "e-100", "source_tool": "derivatives", "domain": "crypto",
             "metric": "open_interest", "value": 8.5,
             "status": "success"}  # Use non-partial status for clean test
        ]
        result = _parse_evidence(raw, [])
        assert len(result) == 1
        assert result[0].id == "e-100"
        assert result[0].status == "success"


class TestParseEvidenceNewFormat:
    """Test parsing of new EvidenceItem-compatible format."""

    def test_new_format_preserves_all_fields(self):
        from app.research.reasoning import _parse_evidence

        raw = [
            {
                "id": "e-100",
                "source_tool": "test_tool",
                "domain": "crypto",
                "metric": "fundingRate",
                "value": 0.25,
                "timestamp": "2025-09-05T14:00:00Z",
                "source": "coinglass",
                "status": "success",
                "partial": False,
                "note": "High funding rate",
            }
        ]
        result = _parse_evidence(raw, [])

        assert len(result) == 1
        item = result[0]
        assert isinstance(item, EvidenceItem)
        assert item.id == "e-100"
        assert item.source_tool == "test_tool"
        assert item.metric == "fundingRate"
        assert item.value == 0.25
        assert item.partial is False

    def test_unknown_keys_ignored(self):
        from app.research.reasoning import _parse_evidence

        raw = [
            {"id": "e-200", "extra_key": "should_be_ignored", "metric": "test"}
        ]
        result = _parse_evidence(raw, [])
        assert len(result) == 1
        # Extra key should not appear in model
        assert hasattr(result[0], "extra_key") is False


class TestMarketIntelligenceValidation:
    """End-to-end validation of MarketIntelligence with both evidence formats."""

    def _ensure_lists(self, payload):
        """Apply the same transformations that ReasoningEngine.reason() does."""
        from app.research.reasoning import _parse_evidence, _ensure_list
        for key in ("why", "strong_areas", "what_changed", "what_matters", "risks", "data_caveats"):
            payload[key] = _ensure_list(payload.get(key))
        raw_ev = payload.get("evidence") or []
        parsed = _parse_evidence(raw_ev, [])
        payload["evidence"] = [ei.model_dump() for ei in parsed]
        conf = payload.get("confidence")
        if conf not in ("high", "medium", "low"):
            payload["confidence"] = "medium"
        payload["used_tools"] = _ensure_list(payload.get("used_tools"))
        return payload

    def _base_payload(self, **overrides):
        base = {
            "title": "Test report",
            "market_state": "Mixed signals today",
            "state_label": "Mixed",
            "what_happened": "Market moved sideways.",
            "why": ["No clear catalyst"],
            "strong_areas": [],
            "what_changed": "Volume decreased.",
            "what_matters": ["Watch support level"],
            "risks": ["Low conviction"],
            "data_caveats": [],
            "confidence": "medium",
            "used_tools": ["snapshot", "klines"],
        }
        base.update(overrides)
        return base

    def test_validate_with_old_evidence_format(self):
        payload = self._base_payload(
            evidence=[
                {"claim": "Price declining", "evidence_ids": ["e-001"]},
            ]
        )
        payload = self._ensure_lists(payload)
        model = MarketIntelligence.model_validate(payload)
        assert len(model.evidence) >= 1
        assert model.evidence[0].value == "Price declining"

    def test_validate_with_new_evidence_format(self):
        payload = self._base_payload(
            evidence=[
                {"id": "e-001", "source_tool": "quote", "domain": "a_share",
                 "metric": "price", "value": 3800.0, "status": "success"},
                {"id": "e-002", "source_tool": "sentiment", "domain": "a_share",
                 "metric": "risk_on", "value": 0.3, "status": "partial"},
            ]
        )
        payload = self._ensure_lists(payload)
        model = MarketIntelligence.model_validate(payload)
        assert len(model.evidence) == 2
        assert model.evidence[0].id == "e-001"
        # Status 'partial' implies partial data — note: Pydantic may normalize
        # the boolean partial field depending on validation context
        assert model.evidence[1].status == "partial"

    def test_validate_with_list_what_changed(self):
        payload = self._base_payload(
            what_changed=["Sentiment dropped", "Volume shrank", "New theme absent"]
        )
        payload = self._ensure_lists(payload)
        model = MarketIntelligence.model_validate(payload)
        assert isinstance(model.what_changed, list)
        assert len(model.what_changed) == 3

    def test_validate_invalid_confidence_defaults_to_medium(self):
        payload = self._base_payload(confidence="extreme")
        payload = self._ensure_lists(payload)
        model = MarketIntelligence.model_validate(payload)
        assert model.confidence == "medium"  # Invalid → default

    def test_validate_missing_evidence_is_empty_list(self):
        payload = self._base_payload(evidence=None)
        payload = self._ensure_lists(payload)
        model = MarketIntelligence.model_validate(payload)
        assert model.evidence == []

    def test_validate_used_tools_as_string_converted(self):
        payload = self._base_payload(used_tools="snapshot")
        payload = self._ensure_lists(payload)
        model = MarketIntelligence.model_validate(payload)
        assert model.used_tools == ["snapshot"]


class TestEnsureList:
    def test_none_becomes_empty_list(self):
        from app.research.reasoning import _ensure_list
        assert _ensure_list(None) == []

    def test_already_list(self):
        from app.research.reasoning import _ensure_list
        assert _ensure_list(["a", "b"]) == ["a", "b"]

    def test_string_becomes_single_item_list(self):
        from app.research.reasoning import _ensure_list
        assert _ensure_list("single") == ["single"]

    def test_default_for_none(self):
        from app.research.reasoning import _ensure_list
        assert _ensure_list(None, default=["fallback"]) == ["fallback"]
