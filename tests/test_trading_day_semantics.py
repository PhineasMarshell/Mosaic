"""P0-C regressions: requested date, trading date, and evidence as-of semantics."""

from datetime import date
from types import SimpleNamespace

import pytest

from app.agent.evidence_gate import match_claims_to_evidence, run_evidence_gate
from app.graph.market_plan import apply_as_of_date_to_plan
from app.graph.nodes.critic import CriticNode
from app.graph.nodes.gate import GateNode
from app.models.evidence import Evidence
from app.models.market import NormalizedDatum, ToolResult
from app.models.research import ResearchIntent, ResearchPlan, ToolCallPlan
from app.research.evidence import build_evidence
from app.research.trading_calendar import (
    is_a_share_trading_day,
    nearest_previous_trading_day,
    resolve_trading_date_semantics,
)


def test_weekend_today_keeps_requested_date_and_uses_previous_trading_day():
    semantics = resolve_trading_date_semantics("今天A股发生了什么？", now=date(2026, 10, 10))
    assert semantics.requested_date == "2026-10-10"
    assert semantics.market_closed is True
    assert semantics.as_of_date == "2026-10-09"


def test_known_holiday_today_is_closed():
    semantics = resolve_trading_date_semantics("今日A股市场怎么样？", now=date(2026, 10, 1))
    assert semantics.requested_date == "2026-10-01"
    assert semantics.market_closed is True
    assert semantics.as_of_date == "2026-09-30"


def test_no_available_trading_day_is_explicit_unknown():
    semantics = resolve_trading_date_semantics(
        "今天A股发生了什么？", now=date(2026, 10, 10), available_dates=[]
    )
    assert semantics.requested_date == "2026-10-10"
    assert semantics.market_closed is True
    assert semantics.as_of_date is None
    assert semantics.reason == "no_available_trading_day"


def test_explicit_historical_date_is_preserved():
    semantics = resolve_trading_date_semantics("请复盘2026-10-08的A股")
    assert semantics.requested_date == "2026-10-08"
    assert semantics.as_of_date == "2026-10-08"
    assert semantics.market_closed is False
    assert semantics.is_today_request is False


def test_unknown_date_semantics_use_nulls_instead_of_retrieval_time():
    semantics = resolve_trading_date_semantics("请分析这家公司")
    assert semantics.requested_date is None
    assert semantics.as_of_date is None
    assert semantics.market_closed is None


def test_date_aware_plan_uses_as_of_date_without_changing_live_quote_contract():
    plan = ResearchPlan(
        intent=ResearchIntent(domain="a_share"),
        steps=[
            ToolCallPlan(tool_key="limit_up_count", arguments={}, purpose="count"),
            ToolCallPlan(tool_key="quote", arguments={"symbols": ["000300"]}, purpose="quote"),
        ],
    )
    apply_as_of_date_to_plan(plan, "2026-10-09")
    assert plan.as_of_date == "2026-10-09"
    assert plan.intent.as_of_date == "2026-10-09"
    assert plan.steps[0].arguments == {}
    assert plan.steps[1].arguments == {"symbols": ["000300"]}


def test_retrieved_at_never_becomes_as_of_date():
    result = ToolResult(
        tool="quote",
        tool_key="quote",
        operation_id="get_market_quotes",
        arguments={"symbol": "600519"},
        status="success",
        retrieved_at="2026-10-10T09:00:00Z",
        normalized=[
            NormalizedDatum(
                tool="quote",
                metric="change_pct",
                value=2,
                instrument="600519",
                as_of_date="2026-10-09",
                retrieved_at="2026-10-10T09:00:00Z",
                source="exchange",
            )
        ],
    )
    gate = run_evidence_gate([result], requested_date="2026-10-09")
    assert gate.valid_evidence_count == 1
    assert not gate.stale_evidence
    evidence = build_evidence([result], id_prefix="quote")
    assert evidence[0].as_of_date == "2026-10-09"
    assert evidence[0].retrieved_at == "2026-10-10T09:00:00Z"


def test_critic_rejects_expired_evidence_for_today_request():
    evidence = [
        Evidence(
            id="q-1",
            source_tool="quote",
            tool_key="quote",
            metric="change_pct",
            value=2,
            instrument="600519",
            as_of_date="2026-10-09",
            retrieved_at="2026-10-10T09:00:00Z",
            source="exchange",
            coverage="instrument",
        )
    ]
    claim = {"claims": [{"claim": "600519今天上涨", "evidence_ids": ["q-1"]}]}
    row = match_claims_to_evidence(claim, evidence, expected_date="2026-10-10")[0]
    assert row["supported"] is False
    assert "stale_date" in row["reason_codes"]


def test_weekend_report_date_fields_are_explicit():
    semantics = resolve_trading_date_semantics("今日A股发生了什么？", now=date(2026, 10, 10))
    assert semantics.model_dump() == {
        "requested_date": "2026-10-10",
        "as_of_date": "2026-10-09",
        "market_closed": True,
        "is_today_request": True,
        "reason": "market_closed_using_previous_trading_day",
        "data_gap": False,
    }


def test_known_calendar_weekend_and_holiday_are_not_trading_days():
    assert not is_a_share_trading_day("2026-10-10")
    assert not is_a_share_trading_day("2026-10-01")
    assert is_a_share_trading_day("2026-10-09")
    assert nearest_previous_trading_day("2026-10-10") == "2026-10-09"


@pytest.mark.asyncio
async def test_report_title_what_happened_and_caveats_explain_closed_date():
    import json

    from app.config import Settings
    from app.graph.nodes.reasoning import ReasoningNode

    payload = {
        "title": "今日A股市场情报",
        "market_state": "休市",
        "state_label": "Neutral",
        "what_happened": "没有新的盘中行情",
        "why": [],
        "strong_areas": [],
        "what_changed": [],
        "what_matters": [],
        "risks": [],
        "data_caveats": [],
        "confidence": "low",
        "used_tools": [],
        "evidence": [],
        "claims": [],
    }

    async def create(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)))]
        )

    node = ReasoningNode(Settings())
    node._engine.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    out = await node(
        {
            "question": "今天A股发生了什么？",
            "requested_date": "2026-10-10",
            "planned_as_of_date": "2026-10-09",
            "as_of_date": "2026-10-09",
            "market_closed": True,
            "results": [],
            "evidence": [],
            "findings": [],
            "gate": {"has_evidence": False},
        }
    )
    report = out["report"]
    assert "2026-10-10" in report.title and "休市" in report.title and "2026-10-09" in report.title
    assert "2026-10-10" in report.what_happened and "2026-10-09" in report.what_happened
    assert any("requested_date=2026-10-10" in caveat for caveat in report.data_caveats)
    assert any("market_closed=true" in caveat for caveat in report.data_caveats)


@pytest.mark.asyncio
async def test_closed_day_report_does_not_call_older_evidence_the_latest_session():
    import json

    from app.config import Settings
    from app.graph.nodes.reasoning import ReasoningNode

    payload = {
        "title": "A股市场情报",
        "market_state": "休市",
        "what_happened": "行情待核实",
        "confidence": "low",
        "claims": [],
    }

    async def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps(payload, ensure_ascii=False)
        ))])

    node = ReasoningNode(Settings())
    node._engine.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    out = await node({
        "question": "今天A股发生了什么？",
        "requested_date": "2026-10-10",
        "planned_as_of_date": "2026-10-09",
        "as_of_date": "2026-10-08",
        "market_closed": True,
        "results": [],
        "evidence": [],
        "findings": [],
        "gate": {"has_evidence": False},
    })
    report = out["report"]
    assert "最近交易日 2026-10-09 的行情尚无可核验证据" in report.what_happened
    assert "现有证据截至 2026-10-08" in report.what_happened
    assert "使用最近交易日 2026-10-08 数据" not in report.what_happened


@pytest.mark.asyncio
async def test_closed_day_without_market_data_has_no_unknown_title_date():
    import json

    from app.config import Settings
    from app.graph.nodes.reasoning import ReasoningNode

    async def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"title": "A股市场情报", "market_state": "休市", "what_happened": "", "claims": []})
        ))])

    node = ReasoningNode(Settings())
    node._engine.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    out = await node({
        "question": "今天A股发生了什么？",
        "requested_date": "2026-10-10",
        "planned_as_of_date": "2026-10-09",
        "as_of_date": None,
        "market_closed": True,
        "results": [],
        "evidence": [],
        "findings": [],
        "gate": {"has_evidence": False},
    })
    report = out["report"]
    assert "最近交易日 2026-10-09 行情未核实" in report.title
    assert "unknown" not in report.title
    assert "最近交易日 2026-10-09 暂无可核验行情数据" in report.what_happened


@pytest.mark.asyncio
async def test_gate_to_critic_rejects_old_quote_for_last_trading_day():
    import json

    from app.config import Settings

    result = ToolResult(
        tool="get_market_quotes",
        tool_key="quote",
        operation_id="get_market_quotes",
        arguments={"symbols": ["600519"]},
        status="success",
        normalized=[NormalizedDatum(
            tool="get_market_quotes", metric="change_pct", value=2,
            instrument="600519", as_of_date="2026-10-08", source="exchange",
        )],
    )
    evidence = build_evidence([result], id_prefix="quote")
    state = {
        "question": "今天A股发生了什么？",
        "domain": "a_share",
        "requested_date": "2026-10-10",
        "planned_as_of_date": "2026-10-09",
        "market_closed": True,
        "results": [result],
        "evidence": evidence,
    }
    state.update(await GateNode(Settings())(state))
    assert state["as_of_date"] == "2026-10-08"
    assert state["gate"].stale_evidence
    state["report"] = {
        "title": "A股市场情报",
        "what_happened": "600519最近交易日上涨。",
        "claims": [{
            "claim": "600519最近交易日上涨",
            "evidence_ids": [evidence[0].id],
            "claim_type": "fact",
        }],
    }

    async def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"issues": [], "verdict": "pass", "reason": "ok"})
        ))])

    critic = CriticNode.__new__(CriticNode)
    critic.settings = Settings()
    critic.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    out = await critic(state)
    assert out["critique"].verdict != "pass"
    assert any("stale_date" in issue.rationale for issue in out["critique"].issues)
