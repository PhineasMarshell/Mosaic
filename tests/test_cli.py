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
    # 阶段 5：只有通过审计（delivery_status=verified）的结果才会被当作结论输出；
    # 手工构造且没有审计结论的响应一律按"未验证"处理。
    result = ResearchResponse(question="q", report=report, delivery_status="verified", final_audit_status="pass")
    _stub_run(monkeypatch, result)

    await cli.main("q")

    out = capsys.readouterr().out
    assert "指数普跌" in out


async def test_unverified_report_is_not_rendered_as_conclusion(monkeypatch, capsys):
    """回归：未经审计的报告不许像正常结论一样打印出来（哪怕 errors 为空）。"""
    report = MarketIntelligence.model_validate(
        {"market_state": "走强", "state_label": "RISK_ON", "what_happened": "市场走强", "confidence": "high"}
    )
    result = ResearchResponse(
        question="q",
        report=report,
        delivery_status="blocked",
        final_audit_status="research_exhausted",
        delivery_reason="审计未通过（verdict=research_more）",
        errors=[],
    )
    _stub_run(monkeypatch, result)

    with pytest.raises(SystemExit) as exc_info:
        await cli.main("q")

    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "市场走强" not in captured.out, "blocked 的正文不得作为结论输出"
    assert "未通过证据审计" in captured.err


async def test_degraded_report_is_explicit_and_not_marked_persisted(monkeypatch, capsys):
    report = MarketIntelligence(
        market_state="震荡",
        state_label="Neutral",
        what_happened="仅展示已有证据支持的事实",
        confidence="low",
    )
    result = ResearchResponse(
        question="q",
        report=report,
        critique={"verdict": "revise", "reason": "仍有证据缺口"},
        final_audit_status="revise_exhausted",
        delivery_status="degraded",
        delivery_reason="部分证据缺口未解决",
        unresolved_issues=["missing_evidence | 量能 | action=research_more"],
    )
    _stub_run(monkeypatch, result)

    async def no_persist(*args, **kwargs):
        return False

    monkeypatch.setattr(cli, "persist_research", no_persist)
    await cli.main("q")

    captured = capsys.readouterr()
    assert "仅展示已有证据支持的事实" in captured.out
    assert "降级交付" in captured.err
    assert "persisted=False" in captured.err
