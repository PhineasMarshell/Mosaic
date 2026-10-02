"""tests/test_briefs.py — 定时简报走图 + memory 兜底（P5-1）。

验证：
- 图成功时，brief 从 ResearchResponse.report 构建 items，结构不变
- 图失败时，走 memory 模板兜底，结构仍不变
- morning / evening 两种类型各自覆盖
"""

import types

import pytest

from app.config import Settings


class _FakeMemory:
    """最小 fake memory，满足兜底路径的方法调用。"""

    def get_recent_states(self, days=7):
        return [{"state_label": "窄幅震荡", "strong_areas": ["AI", "算力"]}]

    def get_anomalies(self, since=None, limit=3):
        return []

    def get_daily_state(self):
        return {"state_label": "窄幅震荡"}


def _make_fake_report():
    return types.SimpleNamespace(
        state_label="情绪修复",
        what_happened="A股今日放量上涨，科技股领涨，成交额突破万亿。",
        strong_areas=["AI", "算力", "半导体"],
        risks=["外部不确定性升温"],
    )


class _FakeOrchestratorSuccess:
    def __init__(self, settings):
        self.settings = settings

    async def run(self, question, **kwargs):
        return types.SimpleNamespace(report=_make_fake_report())


class _FakeOrchestratorFail:
    def __init__(self, settings):
        self.settings = settings

    async def run(self, question, **kwargs):
        raise RuntimeError("graph down")


# ------------------------------------------------------------------ #
# 图成功路径                                                           #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_morning_brief_uses_graph(monkeypatch):
    """图成功时，晨报从 report 构建 items，结构不变。"""
    from app.scheduler import briefs

    monkeypatch.setattr("app.agent.orchestrator.Orchestrator", _FakeOrchestratorSuccess)

    result = await briefs.generate_morning_brief(
        settings=Settings(), memory=_FakeMemory()
    )

    assert result["title"] == "Good Morning. Here's what matters today."
    assert result["type"] == "morning"
    assert "generated_at" in result
    assert isinstance(result["items"], list)
    assert 0 < len(result["items"]) <= 5
    assert any("情绪修复" in item for item in result["items"])


@pytest.mark.asyncio
async def test_evening_brief_uses_graph(monkeypatch):
    """图成功时，晚报从 report 构建 items，结构不变。"""
    from app.scheduler import briefs

    monkeypatch.setattr("app.agent.orchestrator.Orchestrator", _FakeOrchestratorSuccess)

    result = await briefs.generate_evening_brief(
        settings=Settings(), memory=_FakeMemory()
    )

    assert result["title"] == "What actually happened today?"
    assert result["type"] == "evening"
    assert "generated_at" in result
    assert isinstance(result["items"], list)
    assert 0 < len(result["items"]) <= 5
    assert any("情绪修复" in item for item in result["items"])


# ------------------------------------------------------------------ #
# memory 兜底路径                                                      #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_morning_brief_fallback_to_memory(monkeypatch):
    """图失败时，晨报走 memory 兜底，结构仍不变。"""
    from app.scheduler import briefs

    monkeypatch.setattr("app.agent.orchestrator.Orchestrator", _FakeOrchestratorFail)

    result = await briefs.generate_morning_brief(
        settings=Settings(), memory=_FakeMemory()
    )

    assert result["title"] == "Good Morning. Here's what matters today."
    assert result["type"] == "morning"
    assert "generated_at" in result
    assert isinstance(result["items"], list)
    assert 0 < len(result["items"]) <= 5
    # 兜底路径从 memory 读 state_label
    assert any("窄幅震荡" in item for item in result["items"])


@pytest.mark.asyncio
async def test_evening_brief_fallback_to_memory(monkeypatch):
    """图失败时，晚报走 memory 兜底，结构仍不变。"""
    from app.scheduler import briefs

    monkeypatch.setattr("app.agent.orchestrator.Orchestrator", _FakeOrchestratorFail)

    result = await briefs.generate_evening_brief(
        settings=Settings(), memory=_FakeMemory()
    )

    assert result["title"] == "What actually happened today?"
    assert result["type"] == "evening"
    assert "generated_at" in result
    assert isinstance(result["items"], list)
    assert 0 < len(result["items"]) <= 5
    assert any("窄幅震荡" in item for item in result["items"])
