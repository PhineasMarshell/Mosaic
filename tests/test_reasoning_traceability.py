"""阶段 4（Reasoning / Critic 可追溯性）验收用例 —— 方案 §4 阶段 4。

覆盖三块要求：

1. **Reasoning**：关键论断必须带 claim—evidence 映射，且引用由**代码**核对 ——
   引用了账本里不存在的 evidence id 时，该引用被剥离、整份报告降级
   （confidence=low + data_caveats），不许 LLM 伪造引用。
2. **实体校验**：报告里的具体公司名必须有校验源（本地 code/name 映射或证据）；
   无校验源时"某公司不存在 / 未上市"这类断言由代码变成 ``invalid_entity`` issue。
3. **Critic 审查上下文**：必须看到全部用户可见字段（含 what_changed /
   what_matters / data_caveats 与 claim 映射），报告引用的证据优先给完整
   datum，截断必须显式标注并进 telemetry。

本文件 mock 了什么
------------------
- **LLM**：``ReasoningEngine.client`` / ``CriticNode.client`` 换成返回固定 JSON 的
  fake（``types.SimpleNamespace``），两个节点自身的后处理与 ``__call__`` 骨架真实运行。
- **数据**：``ToolResult`` / ``Evidence`` / 报告 dict 直接构造，不经过 Gateway。

因此**不覆盖**真实 LLM 是否遵守 prompt 里的引用纪律 —— 这里验证的是
"它违反了也拦得住"。``_call__`` 未被替换，所以不需要 ``# T35-OK:``。
"""

from __future__ import annotations

import json
import logging
import types

from app.config import Settings
from app.graph.nodes.critic import (
    _MAX_REVIEW_FIELD_CHARS,
    CriticNode,
    _code_level_issues,
    _format_evidence_for_review,
    _format_report_for_review,
)
from app.models.evidence import Evidence
from app.models.market import NormalizedDatum, ToolResult
from app.research.entity_check import check_report_entities, extract_entity_mentions, nonexistence_assertions
from app.research.reasoning import ReasoningEngine

REAL_ID = "technical-001"


# ------------------------------------------------------------------ #
# 脚手架                                                               #
# ------------------------------------------------------------------ #


def _reasoning_engine(payload: dict) -> ReasoningEngine:
    """只换 OpenAI 客户端：``reason()`` 的全部后处理真实运行。"""
    engine = ReasoningEngine(Settings())
    content = json.dumps(payload, ensure_ascii=False)

    async def create(**kwargs):
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(content=content),
                    finish_reason="stop",
                )
            ]
        )

    engine.client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
    )
    return engine


class _FakeCompletions:
    """Critic 用的 fake completion：记录最后一次 prompt，返回固定 JSON。"""

    def __init__(self, content: str):
        self.content = content
        self.last_prompt: str = ""

    async def create(self, **kwargs):
        messages = kwargs.get("messages") or []
        self.last_prompt = "\n".join(
            str(m.get("content", "")) for m in messages if isinstance(m, dict) and m.get("role") == "user"
        )
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=self.content))]
        )


def _critic(content: str) -> tuple[CriticNode, _FakeCompletions]:
    node = CriticNode.__new__(CriticNode)
    node.settings = Settings()
    completions = _FakeCompletions(content)
    node.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
    return node, completions


def _report(**overrides) -> dict:
    """一份"干净"的报告：无无效引用、无未验证实体。"""
    base = {
        "title": "今日A股市场情报",
        "market_state": "指数震荡整理",
        "state_label": "Mixed",
        "what_happened": "沪深300 收跌 0.4%。",
        "why": ["量能不足"],
        "strong_areas": ["银行", "煤炭"],
        "what_changed": ["成交额较昨日下降 8%"],
        "what_matters": ["关注明日量能能否修复"],
        "risks": ["外围市场波动"],
        "data_caveats": ["情绪指标为 partial"],
        "confidence": "medium",
        "used_tools": ["get_market_quotes"],
        "evidence": [],
        "claims": [],
        "evidence_violations": [],
        "unverified_entities": [],
    }
    base.update(overrides)
    return base


def _ledger(**overrides) -> Evidence:
    kwargs = dict(
        id=REAL_ID,
        source_tool="get_market_quotes",
        domain="a_share",
        metric="close",
        value=3820.5,
        status="success",
        instrument="000300",
    )
    kwargs.update(overrides)
    return Evidence(**kwargs)


def _result(**overrides) -> ToolResult:
    kwargs = dict(
        tool="get_market_quotes",
        tool_key="quote",
        arguments={"symbol": "000300"},
        status="success",
        normalized=[NormalizedDatum(metric="close", value=3820.5, tool="get_market_quotes")],
    )
    kwargs.update(overrides)
    return ToolResult(**kwargs)


def _state(report: dict, *, evidence=(), results=None) -> dict:
    return {
        "run_id": "run-stage4",
        "question": "今天A股发生了什么？",
        "domain": "a_share",
        "report": report,
        "results": list(results or []),
        "evidence": list(evidence),
        "gate": None,
    }


PASS_JSON = json.dumps({"issues": [], "verdict": "pass", "reason": "证据充分"}, ensure_ascii=False)


# ------------------------------------------------------------------ #
# 1. claim—evidence 映射（Reasoning 侧代码级校验）                      #
# ------------------------------------------------------------------ #


class TestClaimEvidenceMapping:
    async def test_valid_reference_is_kept(self):
        report = await _reasoning_engine(
            _base_payload(
                claims=[{"claim": "沪深300 收跌 0.4%", "evidence_ids": [REAL_ID], "claim_type": "fact"}]
            )
        ).reason("今天A股发生了什么？", [], [_ledger()])

        assert report.evidence_violations == []
        assert [c.evidence_ids for c in report.claims] == [[REAL_ID]]
        assert report.claims[0].claim_type == "fact"
        assert report.confidence == "medium"

    async def test_forged_reference_is_stripped_and_report_degraded(self):
        report = await _reasoning_engine(
            _base_payload(
                confidence="high",
                claims=[{"claim": "主力资金净流入 100 亿", "evidence_ids": ["technical-999"], "claim_type": "fact"}],
            )
        ).reason("今天A股发生了什么？", [], [_ledger()])

        # 该论断原本引用 id、剥离后一个不剩 → 整条移除（不能留一条无据的"事实"）
        assert report.claims == []
        assert len(report.evidence_violations) == 1
        assert "technical-999" in report.evidence_violations[0]
        assert report.confidence == "low"
        assert any("不存在的证据 id" in caveat for caveat in report.data_caveats)

    async def test_partially_valid_reference_keeps_only_the_valid_id(self):
        report = await _reasoning_engine(
            _base_payload(
                claims=[
                    {
                        "claim": "两市成交额低于昨日",
                        "evidence_ids": [REAL_ID, "technical-404"],
                        "claim_type": "comparison",
                    }
                ]
            )
        ).reason("今天A股发生了什么？", [], [_ledger()])

        assert [c.evidence_ids for c in report.claims] == [[REAL_ID]]
        assert len(report.evidence_violations) == 1

    async def test_claim_without_reference_is_not_a_violation(self):
        """纯推断本来就没有引用，不能算伪造引用。"""
        report = await _reasoning_engine(
            _base_payload(claims=[{"claim": "可能是情绪面修复", "evidence_ids": [], "claim_type": "inference"}])
        ).reason("今天A股发生了什么？", [], [_ledger()])

        assert len(report.claims) == 1
        assert report.evidence_violations == []

    async def test_illegal_claim_type_falls_back_to_other(self):
        report = await _reasoning_engine(
            _base_payload(claims=[{"claim": "情绪转暖", "evidence_ids": [REAL_ID], "claim_type": "vibes"}])
        ).reason("今天A股发生了什么？", [], [_ledger()])

        assert report.claims[0].claim_type == "other"

    async def test_malformed_claims_cannot_destroy_the_whole_report(self):
        """形状错 → 丢弃该条，而不是让整份报告变成 LLMOutputError。"""
        report = await _reasoning_engine(
            _base_payload(claims=["沪深300 收跌", 42, None, {"evidence_ids": [REAL_ID]}])
        ).reason("今天A股发生了什么？", [], [_ledger()])

        assert report.claims == []
        assert report.what_happened  # 报告本体完好


def _base_payload(**overrides) -> dict:
    base = {
        "title": "今日A股市场情报",
        "market_state": "指数震荡整理",
        "state_label": "Mixed",
        "what_happened": "沪深300 收跌 0.4%。",
        "why": ["量能不足"],
        "strong_areas": [],
        "what_changed": ["成交额下降"],
        "what_matters": ["关注量能"],
        "risks": [],
        "data_caveats": [],
        "confidence": "medium",
        "used_tools": ["get_market_quotes"],
        "evidence": [],
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------ #
# 2. 实体校验                                                          #
# ------------------------------------------------------------------ #


class TestEntityVerification:
    async def test_unknown_entity_is_recorded_as_unverified(self):
        report = await _reasoning_engine(_base_payload(what_happened="行云科技今日领涨")).reason(
            "今天A股发生了什么？", [], []
        )

        assert "行云科技" in report.unverified_entities

    async def test_locally_known_entity_is_not_flagged(self):
        report = await _reasoning_engine(_base_payload(what_happened="宁德时代今日领涨")).reason(
            "今天A股发生了什么？", [], []
        )

        assert "宁德时代" not in report.unverified_entities

    async def test_entity_backed_by_evidence_instrument_is_not_flagged(self):
        report = await _reasoning_engine(_base_payload(what_happened="行云科技今日领涨")).reason(
            "今天A股发生了什么？", [], [_ledger(instrument="行云科技")]
        )

        assert "行云科技" not in report.unverified_entities

    def test_two_entities_in_one_sentence_are_both_extracted(self):
        """修掉的历史缺陷：贪婪正则会把"宁德时代与行云科技"吞成一个假实体。"""
        mentions = extract_entity_mentions("宁德时代与行云科技同时上涨")

        assert "行云科技" in mentions
        assert "宁德时代" in mentions
        assert all("与" not in name for name in mentions)

    def test_check_report_entities_marks_the_unverified_one(self):
        unverified, lines = check_report_entities("宁德时代与行云科技同时上涨", [])

        assert "行云科技" in unverified
        assert "宁德时代" not in unverified
        assert any("无校验源" in line for line in lines)
        assert any("本地映射" in line for line in lines)

    def test_nonexistence_assertion_is_detected_next_to_the_entity(self):
        assert nonexistence_assertions("行云科技不存在，市场上没有这只股票。", ["行云科技"]) == ["行云科技"]

    def test_nonexistence_assertion_is_not_invented_far_away(self):
        text = "行云科技今日上涨，" + "市场整体情绪偏暖，" * 6 + "该标的并不存在退市风险。"
        assert nonexistence_assertions(text, ["行云科技"]) == []

    def test_shorter_name_inside_a_longer_one_is_dropped(self):
        mentions = extract_entity_mentions("行云科技股份今日停牌")

        assert mentions == ["行云科技股份"]


# ------------------------------------------------------------------ #
# 3. Critic 审查上下文                                                  #
# ------------------------------------------------------------------ #


class TestReportReviewContext:
    def test_every_user_visible_field_is_reviewed(self):
        report = _report(
            claims=[{"claim": "量能萎缩", "evidence_ids": [REAL_ID], "claim_type": "fact"}],
            evidence=[{"id": REAL_ID}],
        )

        text, truncations = _format_report_for_review(report)

        for label in (
            "标题",
            "置信度",
            "市场状态",
            "综合状态",
            "发生了什么",
            "原因分析",
            "强势方向",
            "与之前相比的变化",
            "后续关注点",
            "风险/反证",
            "数据口径与时效说明",
        ):
            assert label in text, label
        assert "成交额较昨日下降 8%" in text  # what_changed
        assert "关注明日量能能否修复" in text  # what_matters
        assert "情绪指标为 partial" in text  # data_caveats
        assert f"[fact] 量能萎缩 ← {REAL_ID}" in text
        assert truncations == []

    def test_missing_claim_mapping_is_called_out(self):
        text, _ = _format_report_for_review(_report())

        assert "未给出任何 claim 映射" in text

    def test_truncation_is_marked_on_the_field(self):
        report = _report(what_happened="啊" * (_MAX_REVIEW_FIELD_CHARS + 50))

        text, truncations = _format_report_for_review(report)

        assert [t["field"] for t in truncations].count("what_happened") == 1
        assert truncations[0]["total"] == _MAX_REVIEW_FIELD_CHARS + 50
        assert "已截断：仅展示前" in text

    def test_code_level_findings_are_shown_to_the_critic(self):
        report = _report(
            evidence_violations=["论断「x」引用了不存在的 evidence id: technical-999"],
            unverified_entities=["行云科技"],
        )

        text, _ = _format_report_for_review(report)

        assert "本报告不可能 pass" in text
        assert "technical-999" in text
        assert "无校验源的实体" in text
        assert "行云科技" in text


class TestEvidenceReviewContext:
    def test_referenced_evidence_gets_full_datum_first(self):
        ledger = [_ledger(), _ledger(id="technical-002", metric="amount", value=1)]

        text, stats = _format_evidence_for_review([_result()], ledger, None, [REAL_ID])

        assert "报告引用的证据（完整原始 datum）" in text
        assert stats["referenced_shown"] == 1
        assert stats["referenced_missing"] == []
        assert text.index(REAL_ID) < text.index("technical-002")
        assert stats["other_total"] == 1

    def test_reference_missing_from_ledger_is_flagged(self):
        text, stats = _format_evidence_for_review([], [_ledger()], None, ["technical-999"])

        assert stats["referenced_missing"] == ["technical-999"]
        assert "不在证据账本中（该引用无法核实）" in text

    def test_omitted_content_is_never_read_as_absent(self):
        ledger = [_ledger(id=f"technical-{i:03d}", metric=f"m{i}") for i in range(3)]
        normalized = [NormalizedDatum(metric=f"m{i}", value=i, tool="get_market_quotes") for i in range(15)]

        text, stats = _format_evidence_for_review([_result(normalized=normalized)], ledger, None, [])

        assert stats["other_shown"] == 3
        assert stats["datums_omitted"] == 5
        assert "未展示的内容不能据此判定为无证据" in text
        assert "该工具另有 5 条 datum 未展示" in text


# ------------------------------------------------------------------ #
# 4. 代码级 issue                                                      #
# ------------------------------------------------------------------ #


class TestCodeLevelIssues:
    def test_forged_reference_becomes_a_high_severity_issue(self):
        issues = _code_level_issues(_report(evidence_violations=["论断「x」引用了不存在的 evidence id: technical-999"]))

        assert len(issues) == 1
        assert issues[0].kind == "unsupported_claim"
        assert issues[0].severity == "high"
        assert issues[0].action == "remove_or_qualify"
        assert "technical-999" in issues[0].claim

    def test_nonexistence_claim_about_unverified_entity_becomes_an_issue(self):
        report = _report(what_happened="行云科技不存在，市场上查无此股。", unverified_entities=["行云科技"])

        issues = _code_level_issues(report)

        assert [i.kind for i in issues] == ["invalid_entity"]
        assert issues[0].action == "remove_or_qualify"
        assert "行云科技" in issues[0].claim

    def test_verified_entity_mention_is_not_an_issue(self):
        report = _report(what_happened="行云科技今日上涨。", unverified_entities=[])

        assert _code_level_issues(report) == []


# ------------------------------------------------------------------ #
# 5. Critic 端到端                                                      #
# ------------------------------------------------------------------ #


class TestCriticEndToEnd:
    async def test_forged_reference_cannot_pass_even_if_the_model_says_pass(self):
        node, _ = _critic(PASS_JSON)
        report = _report(
            claims=[{"claim": "主力净流入 100 亿", "evidence_ids": ["technical-999"], "claim_type": "fact"}],
            evidence_violations=["论断「主力净流入 100 亿」引用了不存在的 evidence id: technical-999"],
        )

        out = await node(_state(report, evidence=[_ledger()]))

        assert out["critique"].verdict != "pass"
        assert "errors" not in out  # 审计没通过不是运行错误
        kinds = [i.kind for i in out["critique"].issues]
        assert "unsupported_claim" in kinds

    async def test_clean_report_can_still_pass(self):
        """反向控制：没有代码级问题时，模型说 pass 仍然可以 pass（快路径没被堵死）。"""
        node, completions = _critic(PASS_JSON)
        report = _report(
            claims=[{"claim": "沪深300 收跌 0.4%", "evidence_ids": [REAL_ID], "claim_type": "fact"}]
        )

        out = await node(_state(report, evidence=[_ledger()]))

        assert out["critique"].verdict == "pass"
        assert "errors" not in out
        assert "关键论断 → 证据映射" in completions.last_prompt
        assert "=== 实际证据 ===" in completions.last_prompt

    async def test_truncation_is_disclosed_to_the_model_and_the_telemetry(self, caplog):
        node, completions = _critic(PASS_JSON)
        report = _report(what_happened="啊" * (_MAX_REVIEW_FIELD_CHARS + 10))

        with caplog.at_level(logging.INFO, logger="app.graph.run_log"):
            out = await node(_state(report))

        assert out["critique"].verdict == "pass"
        assert "审查上下文截断说明" in completions.last_prompt
        assert "未展示的内容不能据此判定为「无证据」" in completions.last_prompt

        events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "app.graph.run_log"]
        critic_events = [e for e in events if e.get("event") == "critic"]
        assert critic_events, events
        truncation = critic_events[-1]["context_truncation"]
        assert [f["field"] for f in truncation["report_fields"]] == ["what_happened"]
        assert truncation["evidence"]["other_shown"] == 0
