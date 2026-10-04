"""T9 回归：CLI 在 report=None 时必须展示真实错误并非0退出，而不是抛 AttributeError。"""

import pytest

import app.cli as cli
from app.models.response import MarketIntelligence, ResearchResponse


def _stub_run(monkeypatch, result: ResearchResponse) -> None:
    async def fake_run(self, question, **kwargs):
        return result

    monkeypatch.setattr(cli.Orchestrator, "run", fake_run)


async def test_none_report_prints_real_error(monkeypatch, capsys):
    result = ResearchResponse(question="q", report=None, errors=["Reasoning engine failed: upstream 500"])
    _stub_run(monkeypatch, result)

    with pytest.raises(SystemExit) as exc_info:
        await cli.main("q")

    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "Reasoning engine failed: upstream 500" in err
    assert "model_dump" not in err


async def test_none_report_without_errors_still_exits(monkeypatch, capsys):
    result = ResearchResponse(question="q", report=None)
    _stub_run(monkeypatch, result)

    with pytest.raises(SystemExit) as exc_info:
        await cli.main("q")

    assert exc_info.value.code != 0
    assert capsys.readouterr().err


async def test_valid_report_renders(monkeypatch, capsys):
    report = MarketIntelligence.model_validate(
        {
            "title": "t",
            "market_state": "弱",
            "state_label": "Neutral",
            "what_happened": "指数普跌",
            "confidence": "low",
        }
    )
    result = ResearchResponse(question="q", report=report)
    _stub_run(monkeypatch, result)

    await cli.main("q")

    out = capsys.readouterr().out
    assert "指数普跌" in out
