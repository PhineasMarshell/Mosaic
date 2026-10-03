"""Tests for Reasoning Engine backward-compatible evidence parsing.

Cover:
- Old LLM output format: {claim, evidence_ids} -> EvidenceItem
- New LLM output format: {id, source_tool, metric, value, ...} -> EvidenceItem
- what_changed as string vs list
- Model validation of both formats

Note on new-format tests: 审计第 7 项修复后，新格式条目必须有匹配的原始 Evidence
才能通过验证层。没有 id 或 id 无法匹配真实数据的条目会被静默丢弃而非编造假证据。
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
            partial=True,
        ),
    ]


class TestParseEvidenceOldFormat:
    """Test backward-compatible parsing of old {claim, evidence_ids} format."""

    def test_single_claim_multiple_evidence(self, sample_evidence):
        from app.research.reasoning import _parse_evidence

        raw = [{"claim": "Market sentiment cooled", "evidence_ids": ["e-001", "e-002"]}]
        result = _parse_evidence(raw, sample_evidence)

        assert len(result) >= 1
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
        """新格式必须提供匹配的原始 Evidence 才能通过验证层。"""
        from app.research.reasoning import _parse_evidence

        raw = [
            {
                "id": "e-100",
                "source_tool": "derivatives",
                "domain": "crypto",
                "metric": "open_interest",
                "value": 8.5,
                "status": "success",
            }
        ]
        original = [
            Evidence(
                id="e-100",
                source_tool="derivatives",
                domain="crypto",
                metric="open_interest",
                value=8.5,
                status="success",
                partial=False,
            )
        ]
        result = _parse_evidence(raw, original)
        assert len(result) == 1
        assert result[0].id == "e-100"
        assert result[0].status == "success"


class TestParseEvidenceNewFormat:
    """新格式必须有匹配的原始 Evidence，否则条目被丢弃（审计第 7 项）。"""

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
        original = [
            Evidence(
                id="e-100",
                source_tool="test_tool",
                domain="crypto",
                metric="fundingRate",
                value=0.25,
                timestamp="2025-09-05T14:00:00Z",
                source="coinglass",
                status="success",
                partial=False,
            )
        ]
        result = _parse_evidence(raw, original)

        assert len(result) == 1
        item = result[0]
        assert isinstance(item, EvidenceItem)
        assert item.id == "e-100"
        assert item.source_tool == "test_tool"
        assert item.metric == "fundingRate"
        assert item.value == 0.25
        assert item.partial is False

    def test_unknown_id_is_dropped(self):
        """无匹配原始证据 → 丢弃而非编造。"""
        from app.research.reasoning import _parse_evidence

        raw = [{"id": "e-nonexistent", "metric": "price", "value": 100}]
        result = _parse_evidence(raw, [])
        assert len(result) == 0

    def test_llm_value_overridden_by_real_evidence(self):
        """LLM 说的 value=999 被真实数据覆盖。"""
        from app.research.reasoning import _parse_evidence

        raw = [{"id": "e-100", "value": 999}]
        original = [
            Evidence(id="e-100", source_tool="t", domain="crypto", metric="fundingRate", value=0.25, status="success")
        ]
        result = _parse_evidence(raw, original)
        assert len(result) == 1
        assert result[0].value == 0.25

    def test_missing_id_is_dropped(self):
        """没有 id 的新格式条目无法匹配 → 丢弃。"""
        from app.research.reasoning import _parse_evidence

        raw = [{"metric": "price", "value": 100}]
        original = [Evidence(id="x", source_tool="t", domain="a_share", metric="p", value=1, status="success")]
        result = _parse_evidence(raw, original)
        assert len(result) == 0

    def test_unknown_keys_ignored(self):
        from app.research.reasoning import _parse_evidence

        raw = [{"id": "e-200", "extra_key": "should_be_ignored", "metric": "test"}]
        original = [Evidence(id="e-200", source_tool="t", metric="test")]
        result = _parse_evidence(raw, original)
        assert len(result) == 1
        assert hasattr(result[0], "extra_key") is False


class TestMarketIntelligenceValidation:
    """End-to-end validation of MarketIntelligence with both evidence formats."""

    def _ensure_lists(self, payload, original_evidence=None):
        """Apply the same transformations that ReasoningEngine.reason() does."""
        from app.research.reasoning import _ensure_list, _parse_evidence

        for key in ("why", "strong_areas", "what_changed", "what_matters", "risks", "data_caveats"):
            payload[key] = _ensure_list(payload.get(key))
        raw_ev = payload.get("evidence") or []
        parsed = _parse_evidence(raw_ev, original_evidence or [])
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
        ev = [
            Evidence(id="e-001", source_tool="quote", domain="a_share", metric="price", value=3800.0, status="success"),
            Evidence(
                id="e-002",
                source_tool="sentiment",
                domain="a_share",
                metric="risk_on",
                value=0.3,
                status="partial",
                partial=True,
            ),
        ]
        payload = self._base_payload(
            evidence=[
                {
                    "id": "e-001",
                    "source_tool": "quote",
                    "domain": "a_share",
                    "metric": "price",
                    "value": 999.0,
                    "status": "success",
                },
                {
                    "id": "e-002",
                    "source_tool": "sentiment",
                    "domain": "a_share",
                    "metric": "risk_on",
                    "value": 0.9,
                    "status": "wrong_status",
                },
            ]
        )
        payload = self._ensure_lists(payload, original_evidence=ev)
        model = MarketIntelligence.model_validate(payload)
        assert len(model.evidence) == 2
        assert model.evidence[0].id == "e-001"
        # LLM 说 value=999，但真实值是 3800
        assert model.evidence[0].value == 3800.0
        assert model.evidence[1].status == "partial"

    def test_validate_with_list_what_changed(self):
        payload = self._base_payload(what_changed=["Sentiment dropped", "Volume shrank", "New theme absent"])
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


# ------------------------------------------------------------------ #
# P2.5-4：Reasoning 消费 findings digest                                #
# ------------------------------------------------------------------ #


class TestReasoningFindingsInPrompt:
    """findings 参数应被拼入 prompt 的 history_context 段。"""

    @pytest.mark.asyncio
    async def test_findings_appear_in_prompt(self, monkeypatch):
        """传入 findings → mock 捕获的 prompt 包含 ANALYST SUMMARIES 和 digest 文本。"""
        from app.config import Settings
        from app.research.reasoning import ReasoningEngine

        captured_prompts: list[str] = []

        class _FakeMessage:
            content = (
                '{"title":"t","market_state":"s","state_label":"sl",'
                '"what_happened":"w","why":[],"strong_areas":[],"what_changed":"c",'
                '"what_matters":[],"risks":[],"data_caveats":[],"confidence":"low",'
                '"used_tools":[],"evidence":[]}'
            )

        class _FakeChoice:
            message = _FakeMessage()

        class _FakeResp:
            choices = [_FakeChoice()]

        class _FakeCompletions:
            async def create(self, **kwargs):
                for msg in kwargs.get("messages", []):
                    if msg.get("role") == "user":
                        captured_prompts.append(msg["content"])
                return _FakeResp()

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeClient:
            chat = _FakeChat()

        settings = Settings()
        engine = object.__new__(ReasoningEngine)
        engine.settings = settings
        engine.client = _FakeClient()

        findings = [
            {"analyst": "technical", "digest": "执行了 3 个工具, 3 成功", "failed": False},
        ]

        await engine.reason(
            question="测试",
            results=[],
            evidence=[],
            findings=findings,
        )

        assert len(captured_prompts) == 1
        prompt = captured_prompts[0]
        assert "ANALYST SUMMARIES" in prompt
        assert "执行了 3 个工具, 3 成功" in prompt
        assert "technical" in prompt

    @pytest.mark.asyncio
    async def test_no_findings_behavior_unchanged(self, monkeypatch):
        """不传 findings → prompt 中不出现 ANALYST SUMMARIES，行为与旧版一致。"""
        from app.config import Settings
        from app.research.reasoning import ReasoningEngine

        captured_prompts: list[str] = []

        class _FakeMessage:
            content = (
                '{"title":"t","market_state":"s","state_label":"sl",'
                '"what_happened":"w","why":[],"strong_areas":[],"what_changed":"c",'
                '"what_matters":[],"risks":[],"data_caveats":[],"confidence":"low",'
                '"used_tools":[],"evidence":[]}'
            )

        class _FakeChoice:
            message = _FakeMessage()

        class _FakeResp:
            choices = [_FakeChoice()]

        class _FakeCompletions:
            async def create(self, **kwargs):
                for msg in kwargs.get("messages", []):
                    if msg.get("role") == "user":
                        captured_prompts.append(msg["content"])
                return _FakeResp()

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeClient:
            chat = _FakeChat()

        settings = Settings()
        engine = object.__new__(ReasoningEngine)
        engine.settings = settings
        engine.client = _FakeClient()

        await engine.reason(question="测试", results=[], evidence=[])

        assert len(captured_prompts) == 1
        assert "ANALYST SUMMARIES" not in captured_prompts[0]

    @pytest.mark.asyncio
    async def test_failed_finding_marked_in_prompt(self, monkeypatch):
        """failed=True 的 finding → prompt 中标记 [失败]。"""
        from app.config import Settings
        from app.research.reasoning import ReasoningEngine

        captured_prompts: list[str] = []

        class _FakeMessage:
            content = (
                '{"title":"t","market_state":"s","state_label":"sl",'
                '"what_happened":"w","why":[],"strong_areas":[],"what_changed":"c",'
                '"what_matters":[],"risks":[],"data_caveats":[],"confidence":"low",'
                '"used_tools":[],"evidence":[]}'
            )

        class _FakeChoice:
            message = _FakeMessage()

        class _FakeResp:
            choices = [_FakeChoice()]

        class _FakeCompletions:
            async def create(self, **kwargs):
                for msg in kwargs.get("messages", []):
                    if msg.get("role") == "user":
                        captured_prompts.append(msg["content"])
                return _FakeResp()

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeClient:
            chat = _FakeChat()

        settings = Settings()
        engine = object.__new__(ReasoningEngine)
        engine.settings = settings
        engine.client = _FakeClient()

        findings = [
            {"analyst": "fundamental", "digest": "分析失败: timeout", "failed": True},
        ]

        await engine.reason(
            question="测试",
            results=[],
            evidence=[],
            findings=findings,
        )

        prompt = captured_prompts[0]
        assert "[失败]" in prompt
        assert "分析失败: timeout" in prompt
