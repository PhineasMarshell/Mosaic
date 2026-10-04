"""T13 回归：evaluation 必须记录真实错误、工具名取自 report，且空集不得判 PASS。"""

import pytest

from app.evaluation import CaseResult, print_summary, run_case
from app.models.response import MarketIntelligence, ResearchResponse


class _FakeOrch:
    def __init__(self, response):
        self._response = response

    async def run(self, question):
        return self._response


def _report(**overrides) -> MarketIntelligence:
    payload = {"market_state": "弱", "what_happened": "跌"}
    payload.update(overrides)
    return MarketIntelligence.model_validate(payload)


async def test_valid_report_counts_tools_from_report():
    response = ResearchResponse(question="q", report=_report(used_tools=["a", "b"]))
    result = await run_case({"id": "001", "question": "q"}, _FakeOrch(response))

    assert result.success is True
    assert result.tool_count == 2
    assert result.tools_used == ["a", "b"]


async def test_none_report_records_real_error():
    response = ResearchResponse(question="q", report=None, errors=["reasoning failed: real-cause"])
    result = await run_case({"id": "001", "question": "q"}, _FakeOrch(response))

    assert result.success is False
    assert "real-cause" in (result.error or "")
    assert "model_dump" not in (result.error or "")


def test_no_successes_yields_fail():
    results = [CaseResult(case_id=f"00{i}", question=f"q{i}", success=False, error="boom") for i in range(1, 5)]
    summary = print_summary(results)
    assert "Tool Accuracy: FAIL" in summary
    assert "Tool Accuracy: PASS" not in summary


@pytest.mark.parametrize("tool_count,expected_pass", [(4, True), (1, False)])
def test_expected_looked_up_by_id(tool_count, expected_pass):
    # case 003 expected_min_tools == 4
    results = [CaseResult(case_id="003", question="q", success=True, tool_count=tool_count)]
    summary = print_summary(results)
    assert ("Tool Accuracy: PASS" in summary) is expected_pass
