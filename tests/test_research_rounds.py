"""阶段 6 轮次/预算/指标打点的回归测试。

**mock 范围**：本文件不替换任何节点的 ``__call__``。SupervisorNode 只打桩
``node._plan``（保留 ``__call__`` 的计数与记账逻辑）；ReasoningNode 只打桩
``node._engine``（保留节点自己的计数、日志与终态判断）；LLM 与 Gateway 均不真实调用，
因此本文件**不覆盖**真实 LLM 输出的解析质量（那由 test_critic_verdict /
test_reasoning_parsing 覆盖），也不覆盖真实工具耗时（只验证耗时字段被写入与聚合）。
"""

from __future__ import annotations

import json
import logging
import types

import pytest

from app.config import Settings
from app.graph import run_log
from app.graph.builder import critic_route_decision
from app.graph.nodes.reasoning import ReasoningNode
from app.graph.nodes.supervisor import SupervisorNode
from app.graph.state import ResearchState
from app.models.market import ToolResult
from app.models.research import ResearchPlan
from app.models.response import MarketIntelligence

# ------------------------------------------------------------------ #
# 配置：两条路径的独立上限 + 兼容字段只能收紧                            #
# ------------------------------------------------------------------ #


def test_default_caps_are_one_round_per_path():
    """阶段 6③：默认 max_rewrites=1 / max_research_rounds=1（不盲目设 3+）。"""
    settings = Settings()
    assert settings.max_rewrites == 1
    assert settings.max_research_rounds == 1
    assert settings.effective_max_rewrites == 1
    assert settings.effective_max_research_rounds == 1


def test_legacy_critic_max_revisions_can_only_tighten():
    """旧配置名 `CRITIC_MAX_REVISIONS` 只能收紧，绝不能把上限放大。"""
    # 设成比新上限大 → 被新上限夹住（老部署不会因此多跑轮次）
    assert Settings(critic_max_revisions=5).effective_max_rewrites == 1
    assert Settings(critic_max_revisions=5).effective_max_research_rounds == 1
    # 设成更小 → 生效（收紧）
    assert Settings(critic_max_revisions=0).effective_max_rewrites == 0
    # 两个上限都提高、兼容字段留空 → 以新字段为准
    assert Settings(max_rewrites=3, max_research_rounds=2).effective_max_rewrites == 3
    assert Settings(max_rewrites=3, max_research_rounds=2).effective_max_research_rounds == 2
    # 两个上限都提高、兼容字段给 2 → 取 min
    assert Settings(max_rewrites=3, critic_max_revisions=2).effective_max_rewrites == 2
    # 负值按 0 处理（不允许"永远回环"）
    assert Settings(max_rewrites=-1).effective_max_rewrites == 0


def test_unknown_legacy_env_is_ignored_but_new_keys_work(monkeypatch):
    """环境变量口径：新键直接生效，兼容键只能收紧。"""
    monkeypatch.setenv("MAX_RESEARCH_ROUNDS", "1")
    monkeypatch.setenv("CRITIC_MAX_REVISIONS", "9")
    settings = Settings()
    assert settings.effective_max_research_rounds == 1
    assert settings.effective_max_rewrites == 1


# ------------------------------------------------------------------ #
# 计数拆分：谁在什么时候加一                                            #
# ------------------------------------------------------------------ #


def _plan_with_no_steps() -> ResearchPlan:
    return ResearchPlan.model_validate(
        {
            "intent": {
                "domain": "a_share",
                "task": "market_summary",
                "time_scope": "today",
                "question": "今天 A 股发生了什么？",
            },
            "steps": [],
        }
    )


@pytest.mark.asyncio
async def test_supervisor_counts_research_rounds_only_on_replan(monkeypatch):
    """`research_round_count` 只在带回环 critique 的重新规划时 +1；首次规划不计数。"""
    node = SupervisorNode(Settings())
    node.settings = Settings()
    node.client = types.SimpleNamespace()  # 不真实调用 LLM：_plan 被打桩

    async def fake_plan(state):
        node._last_forced_gap = []
        return _plan_with_no_steps(), [], ([], [])

    monkeypatch.setattr(node, "_plan", fake_plan)

    first = await node({"question": "今天 A 股发生了什么？", "domain": "a_share"})
    assert "research_round_count" not in first, "首次规划不是一轮补研究"

    replan = await node(
        {
            "question": "今天 A 股发生了什么？",
            "domain": "a_share",
            "critique": {"verdict": "research_more"},
            "rewrite_count": 1,
        }
    )
    assert replan["research_round_count"] == 1
    assert replan["revision_count"] == 2, "revision_count 只是两条路径计数之和（兼容/展示用）"


def _fake_engine() -> types.SimpleNamespace:
    async def reason(**kwargs):
        return MarketIntelligence(
            market_state="neutral",
            state_label="测试状态",
            what_happened="测试报告",
            confidence="medium",
        )

    return types.SimpleNamespace(reason=reason, last_llm_usage=None, last_llm_duration_ms=None)


def _reasoning_state(*, report, critique) -> dict:
    return {
        "question": "今天 A 股发生了什么？",
        "evidence": [],
        "results": [],
        "findings": [],
        "gate": {"has_evidence": True},
        "report": report,
        "critique": critique,
    }


@pytest.mark.asyncio
async def test_reasoning_counts_rewrite_only_for_revise():
    """`rewrite_count` 只认 Critic 判 revise 的打回；research_more 回环不算改写额度。"""
    node = ReasoningNode(Settings())
    node._engine = _fake_engine()
    existing = {"what_happened": "上一版"}

    revised = await node(_reasoning_state(report=existing, critique={"verdict": "revise"}))
    assert revised["rewrite_count"] == 1

    async def reason(**kwargs):
        return MarketIntelligence(
            market_state="neutral", state_label="x", what_happened="补完证据后的新版", confidence="medium"
        )

    node._engine = types.SimpleNamespace(reason=reason, last_llm_usage=None, last_llm_duration_ms=None)
    followup = await node(
        _reasoning_state(report=existing, critique={"verdict": "research_more"})
    )
    assert followup["rewrite_count"] == 0, "补证据后的重新成文不该吃掉改写额度"
    assert followup["revision_count"] == 0

    initial = await node(_reasoning_state(report=None, critique=None))
    assert initial["rewrite_count"] == 0


@pytest.mark.asyncio
async def test_revision_count_mirrors_both_paths():
    """`revision_count` = 两条路径计数之和（旧字段保留，仅供日志/展示）。"""
    node = ReasoningNode(Settings())
    node._engine = _fake_engine()
    out = await node(
        _reasoning_state(report={"what_happened": "上一版"}, critique={"verdict": "revise"})
        | {"research_round_count": 1, "rewrite_count": 0}
    )
    assert out["rewrite_count"] == 1
    assert out["revision_count"] == 2


# ------------------------------------------------------------------ #
# 预算预留 + 路由原因码                                                 #
# ------------------------------------------------------------------ #


def test_route_reason_codes_are_logged(caplog):
    """路由日志必须能解释"为什么提前结束"：decision + reason + 当时生效的上限。"""
    settings = Settings()
    state = {
        "run_id": "r1",
        "critique": {"verdict": "revise"},
        "rewrite_count": settings.effective_max_rewrites,
        "report": {"what_happened": "x"},
    }
    with caplog.at_level(logging.INFO, logger="app.graph.run_log"):
        decision = critic_route_decision(state, settings)
    assert decision == "finalize_audit"
    payload = _route_payload(caplog)
    assert payload["reason"] == "rewrite_budget_exhausted"
    assert payload["max_rewrites"] == 1
    assert payload["rewrite_count"] == 1
    assert payload["run_id"] == "r1"


def _route_payload(caplog) -> dict:
    for record in reversed(caplog.records):
        try:
            payload = json.loads(record.getMessage())
        except (ValueError, TypeError):  # pragma: no cover — 非 JSON 日志
            continue
        if isinstance(payload, dict) and payload.get("event") == "route":
            return payload
    raise AssertionError("没有找到 route 事件")


# ------------------------------------------------------------------ #
# 指标数据源：LLM 用量 / 工具耗时                                        #
# ------------------------------------------------------------------ #


def test_tool_result_records_duration_ms():
    """工具结果带 duration_ms；旧构造点为 None（不是 0）。"""
    assert ToolResult(tool="x", arguments={}, status="success").duration_ms is None
    assert ToolResult(tool="x", arguments={}, status="success", duration_ms=12.5).duration_ms == 12.5


def test_executions_log_records_tool_duration(caplog):
    """executions 事件必须带单次工具耗时（阶段 6④ 的"工具耗时"数据源）。"""
    result = ToolResult(tool="get_market_quotes", arguments={"symbol": "000300"}, status="success", duration_ms=42.0)
    result.tool_key = "quote"
    with caplog.at_level(logging.INFO, logger="app.graph.run_log"):
        payload = run_log.log_executions({"run_id": "r1"}, [result], category="technical")
    assert payload["results"][0]["duration_ms"] == 42.0
    assert payload["results"][0]["tool_key"] == "quote"


def test_llm_call_log_separates_unmeasured_from_zero(caplog):
    """LLM 事件：量到 usage 就记，量不到记 None —— 不能把"没量到"写成 0。"""
    with caplog.at_level(logging.INFO, logger="app.graph.run_log"):
        measured = run_log.log_llm_call(
            {"run_id": "r1"},
            node="critic",
            model="m",
            usage=types.SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120),
            duration_ms=1234.56,
        )
        unmeasured = run_log.log_llm_call({"run_id": "r1"}, node="supervisor")
    assert measured["prompt_tokens"] == 100
    assert measured["total_tokens"] == 120
    assert measured["duration_ms"] == 1234.6
    assert unmeasured["prompt_tokens"] is None
    assert unmeasured["total_tokens"] is None
    assert unmeasured["duration_ms"] is None


def test_reasoning_state_defaults_split_counters():
    """state 默认值是两条独立计数器，revision_count 只做兼容镜像。"""
    state = ResearchState(question="q")
    assert (state.rewrite_count, state.research_round_count, state.revision_count) == (0, 0, 0)
