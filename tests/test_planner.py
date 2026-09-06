import json
import pytest

from app.models.research import ResearchPlan
from app.gateway.tool_registry import resolve_tool


def test_plan_model():
    payload = {
        "intent": {
            "domain": "a_share",
            "task": "market_diagnosis",
            "time_scope": "today",
            "question": "今天A股为什么弱？",
            "needs_comparison": True,
            "needs_evidence": True,
        },
        "steps": [
            {
                "tool_key": "sentiment",
                "arguments": {},
                "purpose": "查看情绪",
                "priority": "high",
            }
        ],
    }
    plan = ResearchPlan.model_validate(payload)
    assert plan.steps[0].tool_key == "sentiment"
    assert resolve_tool(plan.steps[0].tool_key).domain == "a_share"
