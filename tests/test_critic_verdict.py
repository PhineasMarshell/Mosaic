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


def _route_state(verdict, revision_count=0, with_report=True):
    return {
        "critique": {"verdict": verdict},
        "revision_count": revision_count,
        "report": {"what_happened": "x"} if with_report else None,
    }


@pytest.mark.parametrize(
    "verdict,expected",
    [
        ("pass", "end"),
        ("PASS", "end"),
        ("revise", "reasoning"),
        ("research_more", "supervisor"),
        ("fail", "end"),
        ("", "end"),
        (None, "end"),
    ],
)
def test_route_decision(verdict, expected):
    settings = Settings()
    assert critic_route_decision(_route_state(verdict), settings) == expected


def test_revise_exhausted_revisions_ends():
    settings = Settings()
    state = _route_state("revise", revision_count=settings.critic_max_revisions)
    assert critic_route_decision(state, settings) == "end"


def test_research_more_without_report_ends():
    settings = Settings()
    state = _route_state("research_more", with_report=False)
    assert critic_route_decision(state, settings) == "end"


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
