"""T11 回归：Critic verdict 必须受约束，非法裁决 / 审计自身失败不得被当成通过。

- 路由层：合法值走对应路由；大小写/空白归一化后仍可识别。
- 节点层：非法 verdict 必须产出内部 ``verdict="error"`` 且写入 ``errors``；
  审计自身失败也是 error（不再 research_more 重跑、不伪造 missing_points）。
"""

import json
import types

import pytest

from app.config import Settings
from app.errors import LLMOutputError
from app.graph.builder import critic_route_decision
from app.graph.nodes.critic import CriticNode

# ------------------------------------------------------------------ #
# 路由层                                                              #
# ------------------------------------------------------------------ #


def _route_state(verdict, rewrite_count=0, research_round_count=0, with_report=True):
    """路由输入：阶段 6 拆成两个独立计数器（`revision_count` 只是它们的和）。"""
    return {
        "critique": {"verdict": verdict},
        "rewrite_count": rewrite_count,
        "research_round_count": research_round_count,
        "revision_count": rewrite_count + research_round_count,
        "report": {"what_happened": "x"} if with_report else None,
    }


@pytest.mark.parametrize(
    "verdict,expected",
    [
        ("pass", "end"),
        ("PASS", "end"),
        ("revise", "reasoning"),
        ("research_more", "supervisor"),
        # 阶段 5：非法 / 缺失 verdict 不再静默 END（那会被当成正常完成），
        # 而是进 finalize_audit 落一个可解释的终态。
        ("fail", "finalize_audit"),
        ("", "finalize_audit"),
        (None, "finalize_audit"),
    ],
)
def test_route_decision(verdict, expected):
    settings = Settings()
    assert critic_route_decision(_route_state(verdict), settings) == expected


def test_revise_exhausted_revisions_goes_to_finalize_audit():
    """阶段 5：轮次耗尽后必须经过 finalize_audit 落终态，而不是静默 END。

    阶段 6：revise 的额度是 `max_rewrites`（默认 1），与 research_more 的额度分开。
    """
    settings = Settings()
    state = _route_state("revise", rewrite_count=settings.effective_max_rewrites)
    assert critic_route_decision(state, settings) == "finalize_audit"


def test_research_more_without_report_goes_to_finalize_audit():
    """report 为 None 时不进 supervisor 回环，但仍必须落终态。"""
    settings = Settings()
    state = _route_state("research_more", with_report=False)
    assert critic_route_decision(state, settings) == "finalize_audit"


# ------------------------------------------------------------------ #
# 节点层 test helpers                                                 #
# ------------------------------------------------------------------ #


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _Completions:
    def __init__(self, content=None, exc=None):
        self._content = content
        self._exc = exc

    async def create(self, **kwargs):
        if self._exc is not None:
            raise self._exc
        return _Resp(self._content)


class _Chat:
    def __init__(self, content=None, exc=None):
        self.completions = _Completions(content, exc)


class _FakeClient:
    def __init__(self, content=None, exc=None):
        self.chat = _Chat(content, exc)


def _make_node(content=None, exc=None):
    settings = Settings()
    node = CriticNode.__new__(CriticNode)
    node.settings = settings
    node.client = _FakeClient(content, exc)
    return node


_BASE_STATE = {
    "report": types.SimpleNamespace(
        what_happened="x", confidence="low", state_label="Neutral", strong_areas=[], risks=[], why=[]
    ),
    "results": [],
    "evidence": [],
    "gate": None,
    "question": "q",
    "domain": "a_share",
}


# ------------------------------------------------------------------ #
# 节点层：非法 verdict                                                #
# ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({"verdict": "fail"}),
        json.dumps({"verdict": "reject"}),
        json.dumps({"verdict": ""}),
        json.dumps({"verdict": "bogus"}),
        "{}",
    ],
)
async def test_invalid_verdict_becomes_error_with_errors(content):
    node = _make_node(content=content)
    out = await node(_BASE_STATE)
    assert out["critique"].verdict == "error"
    assert out.get("errors"), "非法 verdict 必须写入 errors，不能静默终止"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  PASS  ", "pass"),
        (" pass", "pass"),
        ("REVISE", "revise"),
        ("Research_More", "research_more"),
    ],
)
async def test_verdict_is_normalized(raw, expected):
    node = _make_node(content=json.dumps({"verdict": raw}))
    out = await node(_BASE_STATE)
    assert out["critique"].verdict == expected
    assert not out.get("errors")


# ------------------------------------------------------------------ #
# 节点层：审计自身失败                                                #
# ------------------------------------------------------------------ #


@pytest.mark.parametrize("exc", [TimeoutError("llm timeout"), LLMOutputError("bad json")])
async def test_audit_failure_is_error_not_research_more(exc):
    node = _make_node(exc=exc)
    out = await node(_BASE_STATE)
    assert out["critique"].verdict == "error"
    assert out["critique"].missing_points == []
    assert out.get("errors")
