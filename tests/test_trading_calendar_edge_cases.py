"""Date parsing and Gateway contract regressions for A-share planning."""

import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.graph.market_plan import apply_as_of_date_to_plan
from app.models.research import ResearchIntent, ResearchPlan, ToolCallPlan
from app.research.trading_calendar import resolve_trading_date_semantics


def test_today_takes_precedence_over_historical_comparison_date():
    semantics = resolve_trading_date_semantics(
        "今天A股相比2026-10-08有哪些变化？", now=date(2026, 10, 10)
    )
    assert semantics.requested_date == "2026-10-10"
    assert semantics.as_of_date == "2026-10-09"
    assert semantics.market_closed is True


def test_explicit_day_with_that_day_stays_historical():
    semantics = resolve_trading_date_semantics(
        "复盘2026-10-08当日A股", now=date(2026, 10, 10)
    )
    assert semantics.requested_date == "2026-10-08"
    assert semantics.is_today_request is False


def test_known_trading_day_after_qingming_is_not_marked_closed():
    semantics = resolve_trading_date_semantics("今天A股发生了什么？", now=date(2025, 4, 7))
    assert semantics.market_closed is False
    assert semantics.as_of_date == "2025-04-07"


def test_future_year_without_exchange_calendar_is_unknown():
    semantics = resolve_trading_date_semantics("今天A股发生了什么？", now=date(2027, 1, 5))
    assert semantics.requested_date == "2027-01-05"
    assert semantics.market_closed is None
    assert semantics.as_of_date is None
    assert semantics.reason == "calendar_unknown"


@pytest.mark.parametrize(
    ("closed_date", "previous_trading_day"),
    [
        (date(2025, 2, 3), "2025-01-27"),
        (date(2025, 2, 4), "2025-01-27"),
        (date(2026, 1, 2), "2025-12-31"),
        (date(2026, 2, 23), "2026-02-13"),
        (date(2026, 5, 4), "2026-04-30"),
        (date(2026, 5, 5), "2026-04-30"),
    ],
)
def test_known_workday_closure_selects_previous_trading_day(closed_date, previous_trading_day):
    semantics = resolve_trading_date_semantics("今天A股发生了什么？", now=closed_date)
    assert semantics.market_closed is True
    assert semantics.as_of_date == previous_trading_day


def test_unparameterized_limit_up_tools_do_not_receive_planned_date():
    plan = ResearchPlan(
        intent=ResearchIntent(domain="a_share"),
        steps=[
            ToolCallPlan(tool_key="limit_up_count", arguments={"date": "2026-10-09"}, purpose="count"),
            ToolCallPlan(tool_key="limit_up_sectors", arguments={"as_of": "2026-10-09"}, purpose="sectors"),
            ToolCallPlan(tool_key="limit_up_pool", arguments={"date": "2026-10-09"}, purpose="pool"),
            ToolCallPlan(tool_key="quote", arguments={"symbols": ["000300"]}, purpose="quote"),
        ],
    )
    apply_as_of_date_to_plan(plan, "2026-10-09")
    assert plan.as_of_date == "2026-10-09"
    assert plan.intent.as_of_date == "2026-10-09"
    assert [step.arguments for step in plan.steps] == [
        {}, {}, {}, {"symbols": ["000300"]}
    ]


@pytest.mark.asyncio
async def test_supervisor_strips_unsupported_date_from_critic_gap_step():
    from app.graph.nodes.supervisor import SupervisorNode

    async def create(**_kwargs):
        payload = {
            "intent": {"domain": "a_share", "task": "company_research"},
            "steps": [],
        }
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
        )

    node = SupervisorNode.__new__(SupervisorNode)
    node.settings = SimpleNamespace(
        max_conversation_turns=10,
        max_research_steps=8,
        openai_model="test-model",
        gap_max_steps=3,
    )
    node.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    state = {
        "question": "600519今天怎么样",
        "domain": "a_share",
        "results": [],
        "critique": {
            "verdict": "research_more",
            "missing_tool_keys": ["limit_up_pool"],
            "issues": [{
                "action": "research_more",
                "required_tool_keys": ["limit_up_pool"],
                "required_coverage": {"arguments": {"date": "2026-10-09"}},
            }],
        },
    }

    plan, _, _ = await node._plan(state)
    pool_steps = [step for step in plan.steps if step.tool_key == "limit_up_pool"]
    assert len(pool_steps) == 1
    assert pool_steps[0].arguments == {}


@pytest.mark.asyncio
async def test_supervisor_replan_reuses_run_date_across_midnight():
    from app.graph.nodes.supervisor import SupervisorNode

    async def create(**_kwargs):
        payload = {"intent": {"domain": "a_share", "task": "company_research"}, "steps": []}
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
        )

    node = SupervisorNode.__new__(SupervisorNode)
    node.settings = SimpleNamespace(
        max_conversation_turns=10,
        max_research_steps=8,
        openai_model="test-model",
        gap_max_steps=3,
    )
    node.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    plan, _, _ = await node._plan({
        "question": "600519今天怎么样",
        "domain": "a_share",
        "requested_date": "2026-10-03",
        "planned_as_of_date": "2026-09-30",
        "market_closed": True,
        "date_reason": "market_closed_using_previous_trading_day",
        "results": [],
        "critique": {"verdict": "research_more", "missing_tool_keys": []},
    })
    assert plan.requested_date == "2026-10-03"
    assert plan.as_of_date == "2026-09-30"
    assert plan.market_closed is True
    assert plan.intent.requested_date == "2026-10-03"


@pytest.mark.asyncio
async def test_supervisor_preflight_retains_date_semantics():
    from app.graph.nodes.supervisor import SupervisorNode

    node = SupervisorNode.__new__(SupervisorNode)
    node.settings = SimpleNamespace(max_research_steps=8, gap_max_steps=3)
    state = {
        "question": "今天A股发生了什么？",
        "domain": "a_share",
        "requested_date": "2026-10-03",
        "planned_as_of_date": "2026-09-30",
        "market_closed": True,
        "date_reason": "market_closed_using_previous_trading_day",
        "results": [],
        "critique": {"verdict": "research_more", "missing_tool_keys": ["not_registered_tool"]},
    }
    plan, _, _ = await node._plan(state)
    assert plan.steps == []
    assert plan.requested_date == "2026-10-03"
    assert plan.as_of_date == "2026-09-30"
    assert plan.market_closed is True
    assert plan.intent.requested_date == "2026-10-03"
    result = await node(state)
    assert result["requested_date"] == "2026-10-03"
    assert result["planned_as_of_date"] == "2026-09-30"
    assert result["market_closed"] is True
    assert result["date_reason"] == "market_closed_using_previous_trading_day"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("domain", "question", "requested_date", "expected_reason"),
    [
        ("a_share", "复盘2027-01-05的A股", None, "calendar_unknown"),
        ("crypto", "今天BTC怎么样", "2026-10-10", None),
    ],
)
async def test_supervisor_date_reason_respects_calendar_coverage_and_domain(
    domain, question, requested_date, expected_reason
):
    from app.graph.nodes.supervisor import SupervisorNode

    async def create(**_kwargs):
        payload = {"intent": {"domain": domain, "task": "market_summary"}, "steps": []}
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
        )

    node = SupervisorNode.__new__(SupervisorNode)
    node.settings = SimpleNamespace(
        max_conversation_turns=10,
        max_research_steps=8,
        openai_model="test-model",
        gap_max_steps=3,
    )
    node.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    state = {"question": question, "domain": domain}
    if requested_date:
        state["requested_date"] = requested_date

    result = await node(state)
    assert result["date_reason"] == expected_reason
