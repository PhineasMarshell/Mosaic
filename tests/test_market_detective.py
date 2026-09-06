import pytest

from app.models.market import ToolResult
from app.models.response import MarketIntelligence


def test_response_shape():
    report = MarketIntelligence(
        market_state="市场整体偏弱",
        state_label="Risk-Off",
        what_happened="测试",
        confidence="medium",
        used_tools=["sentiment"],
    )
    assert report.state_label == "Risk-Off"
    assert report.used_tools == ["sentiment"]
