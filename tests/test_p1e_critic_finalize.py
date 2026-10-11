"""P1-E: claim-scoped Critic policy and closed-day finalization regressions."""

from __future__ import annotations

import json
import types

import pytest

from app.config import Settings
from app.graph.nodes.critic import CriticNode, _code_level_issues, _coverage_issues
from app.graph.nodes.finalize import FinalizeAuditNode
from app.models.evidence import Evidence


class _Completions:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload, ensure_ascii=False)

    async def create(self, **kwargs):
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=self.payload))]
        )


def _critic(payload: dict) -> CriticNode:
    node = CriticNode.__new__(CriticNode)
    node.settings = Settings(_env_file=None, openai_api_key="local-double")
    completions = _Completions(payload)
    node.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
    return node


def _evidence(*, metric: str, value, instrument: str = "600519") -> Evidence:
    return Evidence(
        id="quote-001",
        source_tool="quote",
        tool_key="quote",
        operation_id="get_market_quotes",
        metric=metric,
        value=value,
        instrument=instrument,
        as_of_date="2026-10-09",
        source="exchange",
        coverage="instrument",
    )


@pytest.mark.asyncio
async def test_closed_day_data_gap_is_verified_without_invented_market_claim():
    report = {
        "title": "请求日 2026-10-10，休市；最近交易日 2026-10-09 行情未核实",
        "market_state": "休市",
        "what_happened": "请求日 2026-10-10 A股休市，最近交易日 2026-10-09 暂无可核验行情数据。",
        "claims": [
            {"claim": "2026-10-10 A股休市", "claim_type": "structure", "evidence_ids": []},
            {"claim": "最近交易日 2026-10-09 行情无法确认", "claim_type": "fact", "evidence_ids": []},
        ],
    }
    state = {
        "question": "今天A股发生了什么？",
        "requested_date": "2026-10-10",
        "planned_as_of_date": "2026-10-09",
        "market_closed": True,
        "domain": "a_share",
        "report": report,
        "results": [],
        "evidence": [],
    }
    out = await _critic({"verdict": "pass", "reason": "calendar facts are explicit"})(state)
    assert out["critique"].verdict == "pass"
    finalized = await FinalizeAuditNode(Settings(_env_file=None))(state | out)
    assert finalized["delivery_status"] == "verified"
    assert "上涨" not in report["what_happened"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claim", "metric", "value"),
    [("600519今日上涨", "change_pct", 2.1), ("600519今日涨停", "limit_up_status", "涨停")],
)
async def test_real_quote_or_limit_up_evidence_can_pass(claim, metric, value):
    evidence = _evidence(metric=metric, value=value)
    report = {
        "what_happened": claim,
        "claims": [{"claim": claim, "claim_type": "fact", "evidence_ids": [evidence.id]}],
    }
    state = {
        "question": "今天A股发生了什么？",
        "planned_as_of_date": "2026-10-09",
        "domain": "a_share",
        "report": report,
        "evidence": [evidence],
        "results": [],
    }
    out = await _critic({"verdict": "pass", "reason": "matched evidence"})(state)
    assert out["critique"].verdict == "pass"


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", ["ASML今日订单增长", "地方补贴今日加码", "C50信贷今日增加"])
async def test_external_current_fact_without_evidence_requires_research(claim):
    report = {"what_happened": claim, "claims": [{"claim": claim, "claim_type": "fact", "evidence_ids": []}]}
    out = await _critic({"verdict": "pass", "reason": "model missed the gap"})(
        {"question": claim, "domain": "a_share", "report": report, "evidence": [], "results": []}
    )
    assert out["critique"].verdict == "research_more"
    assert any(issue.action == "research_more" for issue in out["critique"].issues)


@pytest.mark.asyncio
async def test_empty_optional_sections_do_not_block_a_safe_closed_day_report():
    report = {
        "market_state": "休市",
        "what_happened": "请求日 2026-10-10 A股休市，最近交易日行情无法确认。",
        "why": [],
        "strong_areas": [],
        "what_changed": [],
        "what_matters": [],
        "risks": [],
        "claims": [],
    }
    out = await _critic(
        {
            "verdict": "research_more",
            "issues": [
                {
                    "kind": "missing_evidence",
                    "claim": "what_matters 为空",
                    "action": "research_more",
                    "rationale": "可选字段没有填写",
                }
            ],
        }
    )({"domain": "a_share", "report": report, "evidence": [], "results": [], "market_closed": True})
    assert out["critique"].verdict == "pass"


@pytest.mark.asyncio
async def test_closed_day_missing_recent_quote_is_a_qualified_gap_not_failure():
    report = {
        "market_closed": True,
        "what_happened": "请求日 2026-10-10 A股休市，最近交易日行情无法确认。",
        "claims": [],
    }
    out = await _critic(
        {
            "verdict": "research_more",
            "issues": [
                {
                    "kind": "missing_evidence",
                    "claim": "缺少最近交易日行情",
                    "action": "research_more",
                    "rationale": "没有 quote 数据",
                }
            ],
        }
    )({"domain": "a_share", "report": report, "evidence": [], "results": [], "market_closed": True})
    assert out["critique"].verdict == "pass"


def test_qualified_unverified_entity_is_allowed_but_positive_claim_is_not():
    qualified = {"what_happened": "行云科技尚未验证，无法确认其行情。", "unverified_entities": ["行云科技"]}
    assert _code_level_issues(qualified) == []
    positive = {"what_happened": "行云科技今日领涨。", "unverified_entities": ["行云科技"]}
    issues = _code_level_issues(positive)
    assert any(issue.action == "research_more" for issue in issues)
    assert all(issue.kind != "invalid_entity" for issue in issues)


def test_qualified_clause_does_not_hide_later_positive_market_assertion():
    report = {
        "what_happened": "数据可能不足，今天上涨。",
        "claims": [{"claim": "数据可能不足，今天上涨", "claim_type": "fact", "evidence_ids": []}],
    }
    issues = _coverage_issues(report, [])
    assert any(issue.action == "research_more" for issue in issues)


@pytest.mark.asyncio
async def test_critic_does_not_turn_qualified_entity_into_invalid_entity():
    report = {
        "what_happened": "行云科技尚未验证，无法确认其行情。",
        "unverified_entities": ["行云科技"],
        "claims": [],
    }
    out = await _critic(
        {
            "verdict": "revise",
            "issues": [
                {
                    "kind": "invalid_entity",
                    "claim": "行云科技未验证",
                    "action": "remove_or_qualify",
                    "rationale": "实体尚未验证",
                }
            ],
        }
    )({"domain": "a_share", "report": report, "evidence": [], "results": []})
    assert out["critique"].verdict == "pass"


@pytest.mark.asyncio
async def test_partial_limit_up_pool_cannot_support_strong_market_conclusion():
    evidence = _evidence(metric="limit_up_pool", value=["600519"])
    evidence.partial = True
    report = {
        "what_happened": "涨停池显示市场最强主线已经完成全面反转。",
        "claims": [
            {"claim": "涨停池显示市场最强主线已经完成全面反转", "claim_type": "structure", "evidence_ids": [evidence.id]}
        ],
    }
    out = await _critic({"verdict": "pass", "reason": "partial is enough"})(
        {"question": "今天A股发生了什么？", "domain": "a_share", "report": report, "evidence": [evidence], "results": []}
    )
    assert out["critique"].verdict != "pass"


@pytest.mark.asyncio
async def test_unknown_alias_and_forged_reference_remain_blocking():
    report = {
        "what_happened": "600519今日上涨。",
        "evidence_violations": ["报告引用了不存在的 evidence id: forged-999"],
        "claims": [{"claim": "600519今日上涨", "claim_type": "fact", "evidence_ids": ["forged-999"]}],
    }
    out = await _critic({"verdict": "pass", "reason": "ignored"})(
        {"question": "今天A股发生了什么？", "domain": "a_share", "report": report, "evidence": [], "results": []}
    )
    assert out["critique"].verdict != "pass"
    assert any("invalid_reference" in issue.reason_codes for issue in out["critique"].issues)


@pytest.mark.asyncio
async def test_unknown_alias_and_wrong_symbol_or_date_cannot_pass():
    evidence = _evidence(metric="change_pct", value=2.1)
    wrong_report = {
        "what_happened": "000001在2026-10-08上涨。",
        "claims": [{"claim": "000001在2026-10-08上涨", "claim_type": "fact", "evidence_ids": [evidence.id]}],
    }
    wrong = await _critic({"verdict": "pass", "reason": "ignored"})({
        "question": "q", "domain": "a_share", "report": wrong_report, "evidence": [evidence], "results": []
    })
    assert wrong["critique"].verdict != "pass"

    unknown = await _critic({
        "verdict": "pass",
        "issues": [{"kind": "unsupported_claim", "claim": "引用 E999", "action": "remove_or_qualify"}],
    })({
        "question": "q", "domain": "a_share", "report": {"what_happened": "休市"}, "evidence": [evidence], "results": []
    })
    assert unknown["critique"].verdict != "pass"
    assert any("未知 evidence alias" in issue.claim for issue in unknown["critique"].issues)

