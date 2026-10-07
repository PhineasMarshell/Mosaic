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
            source_tool="get_market_quotes",
            domain="a_share",
            metric="price",
            value=3800.5,
            status="success",
        ),
        Evidence(
            id="e-002",
            source_tool="get_ashare_sentiment",
            domain="a_share",
            metric="risk_on",
            value=0.35,
            status="success",
        ),
        Evidence(
            id="e-003",
            source_tool="get_market_klines",
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
    """真调 ``ReasoningEngine.reason()`` 的端到端校验（evidence 双格式兼容）。

    T29：旧实现自带一份 ``_ensure_lists`` 副本，其中把非法 confidence 置
    ``medium``——与生产**相反**（``reasoning.py`` 置 ``low``，缺失置信度应表示
    "没有足够把握"），6 个用例从不调用真的 ``reason()``，假阳性。
    stub 模式复用本文件的 ``_stub_reasoning_engine``（照
    tests/test_data_integrity.py 的 _stub_reasoning 模式）。

    本类 mock 掉了什么：只 mock OpenAI 客户端（返回固定 JSON payload），
    ``reason()`` 的全部后处理（_ensure_list / _parse_evidence / confidence
    兜底 / schema 校验）都走生产代码。
    """

    def _engine(self, payload: dict):
        import json as _json

        return _stub_reasoning_engine(_json.dumps(payload, ensure_ascii=False))

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
            "evidence": [],
        }
        base.update(overrides)
        return base

    @pytest.mark.asyncio
    async def test_old_evidence_format_becomes_evidence_items(self):
        payload = self._base_payload(
            evidence=[
                {"claim": "Price declining", "evidence_ids": ["e-001"]},
            ]
        )
        report = await self._engine(payload).reason("q", [], [])
        assert len(report.evidence) >= 1
        assert report.evidence[0].value == "Price declining"

    @pytest.mark.asyncio
    async def test_new_evidence_format_matches_originals(self):
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
        report = await self._engine(payload).reason("q", [], ev)
        assert len(report.evidence) == 2
        assert report.evidence[0].id == "e-001"
        # LLM 说 value=999，但真实值是 3800
        assert report.evidence[0].value == 3800.0
        assert report.evidence[1].status == "partial"

    @pytest.mark.asyncio
    async def test_list_what_changed_preserved(self):
        payload = self._base_payload(what_changed=["Sentiment dropped", "Volume shrank", "New theme absent"])
        report = await self._engine(payload).reason("q", [], [])
        assert isinstance(report.what_changed, list)
        assert len(report.what_changed) == 3

    @pytest.mark.asyncio
    async def test_invalid_confidence_defaults_to_low(self):
        """T29：生产把非法 confidence 置 ``low``（副本曾置 medium、断言与生产相反）。"""
        payload = self._base_payload(confidence="extreme")
        report = await self._engine(payload).reason("q", [], [])
        assert report.confidence == "low"

    @pytest.mark.asyncio
    async def test_missing_evidence_is_empty_list(self):
        payload = self._base_payload(evidence=None)
        report = await self._engine(payload).reason("q", [], [])
        assert report.evidence == []

    @pytest.mark.asyncio
    async def test_used_tools_as_string_converted(self):
        payload = self._base_payload(used_tools="snapshot")
        report = await self._engine(payload).reason("q", [], [])
        assert report.used_tools == ["snapshot"]


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


# ── T20：必填字段缺失不再毁掉整份报告 + D3：anomalies 由代码填 ──────


def _stub_reasoning_engine(content):
    """照 tests/test_data_integrity.py 的 _stub_reasoning 模式：真调 reason()。"""
    from types import SimpleNamespace

    from app.config import Settings
    from app.research.reasoning import ReasoningEngine

    engine = ReasoningEngine(Settings())

    async def create(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")]
        )

    engine.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return engine


@pytest.mark.asyncio
async def test_missing_required_fields_degrade_with_caveats():
    """模型 payload 缺 market_state / what_happened → 仍产出 report，
    字段置空且 data_caveats 说明降级（旧实现：ValidationError → report=None）。"""
    engine = _stub_reasoning_engine(
        '{"title":"t","state_label":"sl","why":[],"strong_areas":[],'
        '"what_changed":[],"what_matters":[],"risks":[],"data_caveats":[],'
        '"confidence":"low","used_tools":[],"evidence":[]}'
    )
    report = await engine.reason("q", [], [])

    assert report is not None
    assert report.market_state == ""
    assert report.what_happened == ""
    assert any("market_state" in c for c in report.data_caveats)
    assert any("what_happened" in c for c in report.data_caveats)


@pytest.mark.asyncio
async def test_null_required_fields_degrade_with_caveats():
    """模型写 null 同样要降级，不许静默。"""
    engine = _stub_reasoning_engine(
        '{"title":"t","market_state":null,"what_happened":null,"state_label":"sl",'
        '"why":[],"strong_areas":[],"what_changed":[],"what_matters":[],"risks":[],'
        '"data_caveats":[],"confidence":"low","used_tools":[],"evidence":[]}'
    )
    report = await engine.reason("q", [], [])
    assert report is not None
    assert report.market_state == ""
    assert any("market_state" in c for c in report.data_caveats)


@pytest.mark.asyncio
async def test_model_anomalies_scalar_does_not_crash_report():
    """模型把 anomalies 写成标量 → 不再炸成 LLMOutputError（D3：代码填，模型条目丢弃）。"""
    engine = _stub_reasoning_engine(
        '{"title":"t","market_state":"s","what_happened":"w","state_label":"sl",'
        '"why":[],"strong_areas":[],"what_changed":[],"what_matters":[],"risks":[],'
        '"data_caveats":[],"confidence":"low","used_tools":[],"evidence":[],'
        '"anomalies":"模型作文"}'
    )
    report = await engine.reason("q", [], [])
    assert report is not None
    assert report.anomalies == []


async def test_build_response_fills_anomalies_from_code(monkeypatch):
    """D3：report.anomalies 由 detect_anomalies 产出（基于 state["results"]），
    模型给的 anomalies 被覆盖。"""
    from app.models.market import NormalizedDatum, ToolResult
    from app.models.response import build_response_from_state

    prev = ToolResult(
        tool="d",
        arguments={},
        status="success",
        normalized=[NormalizedDatum(metric="openInterest", value=1e9, tool="d")],
    )
    curr = ToolResult(
        tool="d",
        arguments={},
        status="success",
        normalized=[NormalizedDatum(metric="openInterest", value=1.2e9, tool="d")],
    )
    report = MarketIntelligence(
        market_state="s",
        what_happened="w",
        anomalies=[{"id": "model-made", "severity": 1}],
    )
    state = {"report": report, "results": [prev, curr], "domain": "crypto"}

    response = build_response_from_state(state, question="q")

    assert response.report is not None
    assert response.report.anomalies, "OI +20% 应触发异常，anomalies 不应为空"
    assert response.report.anomalies[0]["severity"] == "critical"
    assert all(a["id"] != "model-made" for a in response.report.anomalies)
