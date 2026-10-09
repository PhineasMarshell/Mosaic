"""Orchestrator 对最终研究响应的组装测试。"""

import logging

import pytest

from app.agent.orchestrator import Orchestrator
from app.config import Settings
from app.models.market import ToolResult
from app.models.response import MarketIntelligence


class FakeGraph:
    def __init__(self, final_state):
        self.final_state = final_state

    async def ainvoke(self, state, config=None):
        return self.final_state


def _report() -> MarketIntelligence:
    return MarketIntelligence.model_validate(
        {
            "title": "测试情报",
            "market_state": "震荡",
            "state_label": "Neutral",
            "what_happened": "测试用最小报告",
            "confidence": "low",
        }
    )


@pytest.mark.asyncio
async def test_orchestrator_includes_critique_and_errors(caplog):
    # 真实 ainvoke 的终态里 results 是 ToolResult 实例，不是 dict —
    # 这里必须放非空的实例，否则抓不到 tool_results 的类型转换。
    tool_result = ToolResult(tool="public_limit_up_pool", arguments={"date": "2026-10-04"}, status="success")
    final_state = {
        "report": _report(),
        "results": [tool_result],
        "cache_stats": {},
        "critique": {
            "verdict": "research_more",
            "reason": "Critic audit failed: boom",
        },
        "errors": ["Critic audit failed: boom"],
    }
    orchestrator = Orchestrator(Settings())
    orchestrator._graph = FakeGraph(final_state)

    caplog.set_level(logging.WARNING, logger="app.agent.orchestrator")
    result = await orchestrator.run("q")

    assert result.critique == final_state["critique"]
    assert result.errors == ["Critic audit failed: boom"]
    assert result.tool_results == [tool_result.model_dump()]
    assert all(isinstance(item, dict) for item in result.tool_results)
    # 阶段 5：非 pass 的审计终态是 blocked，且不返回正文报告；
    # 日志按 delivery_status 判断可信与否（不再按 errors / verdict 文案）。
    assert result.delivery_status == "blocked"
    assert result.final_audit_status == "research_exhausted"
    assert result.report is None
    assert any("delivery_status=blocked" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_orchestrator_marks_pass_run_as_verified(caplog):
    """快乐路径保持兼容：verdict=pass → verified + 正文报告正常返回。"""
    final_state = {
        "report": _report(),
        "results": [],
        "cache_stats": {},
        "critique": {"verdict": "pass", "reason": "ok"},
        "errors": [],
    }
    orchestrator = Orchestrator(Settings())
    orchestrator._graph = FakeGraph(final_state)

    caplog.set_level(logging.WARNING, logger="app.agent.orchestrator")
    result = await orchestrator.run("q")

    assert result.delivery_status == "verified"
    assert result.final_audit_status == "pass"
    assert result.report is not None
    assert not [r for r in caplog.records if "delivery_status" in r.message]
