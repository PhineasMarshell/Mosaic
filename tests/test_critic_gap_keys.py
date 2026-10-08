"""Critic 结构化缺口 key：prompt 列出「可补充的工具」+ 落库前过滤幻觉/已执行 key。

本文件 mock 了什么
------------------
- **LLM**：``CriticNode.client`` 换成 fake（``chat.completions.create`` 返回固定
  Critique JSON），并**记录实际发出的 prompt**（范式照搬
  ``tests/test_critic_verdict.py`` 的 ``_Msg/_Choice/_Resp/_Completions/_Chat/
  _FakeClient/_make_node``，只把 ``_Completions.create`` 改成记录
  ``last_prompt`` —— 原版丢掉 kwargs，无法断言 prompt 内容）。
  按 T35 的「fixture 自洽隔离」约定，这里的 fake 是本文件**独立定义**的，
  不从 ``tests/test_critic_verdict.py`` 跨模块 import，避免两个文件被同一个
  改动互相拖累。
- **工具 key / tool_name**：不硬编码 —— 用 ``registry_text(domains=[...])``
  探测式取得。

因此**没有覆盖**
----------------
- 真实 LLM 的审查判断质量（fake 返回的是固定 JSON）；
- 真实 gateway 调用 / 真实网络；
- ``registry_text`` 自身的域过滤正确性（另有 tests/test_b6_domain_filter.py 覆盖）。

T35 约定：不替换被测节点的 ``__call__``（跑真实 ``CriticNode.__call__`` 骨架），
关键用例断言 ``errors == []``；过滤行为只写 logger.warning，不进 errors。
"""

import json
import types

from app.config import Settings
from app.gateway.tool_registry import registry_text
from app.graph.gap_loop import allowed_keys
from app.graph.nodes.critic import CriticNode
from app.models.market import ToolResult

# ------------------------------------------------------------------ #
# 探测式取材                                                          #
# ------------------------------------------------------------------ #

_DOMAIN = "a_share"
_REGISTRY = registry_text(domains=[_DOMAIN, "cross"])
_CANDIDATES = sorted(allowed_keys(_REGISTRY))
assert _CANDIDATES, "a_share+cross 注册表为空，无法验证缺口工具"
#: Critic 点名的缺口工具（未执行 → 必须被保留）
_GAP_KEY = _CANDIDATES[0]
#: 「已经拿到数据」的工具（行必须从 prompt 里被剔除，且不能再被点名）
_EXECUTED_KEY = _CANDIDATES[1] if len(_CANDIDATES) > 1 else _CANDIDATES[0]


def _tool_name_of(registry_key: str) -> str:
    """从注册表某一行取 operationId（`- {key}: {tool_name} [domain] — ...`）。"""
    for line in _REGISTRY.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and stripped[2:].split(":", 1)[0].strip() == registry_key:
            return stripped.split(":", 1)[1].strip().split()[0]
    raise AssertionError(f"{registry_key} 不在探测到的注册表文本里")


# ------------------------------------------------------------------ #
# fake client（记录 prompt）                                          #
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
    """与 tests/test_critic_verdict.py 同构，但额外记录实际发出的 user prompt。"""

    def __init__(self, content=None, exc=None):
        self._content = content
        self._exc = exc
        self.last_prompt: str | None = None

    async def create(self, **kwargs):
        messages = kwargs.get("messages") or []
        self.last_prompt = "\n".join(
            m.get("content", "") for m in messages if isinstance(m, dict) and m.get("role") == "user"
        )
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
    """真实 ``CriticNode`` 实例 + 记录 prompt 的 fake client（不替换节点 ``__call__``）。"""
    node = CriticNode.__new__(CriticNode)
    node.settings = Settings()
    node.client = _FakeClient(content, exc)
    return node


_REPORT = types.SimpleNamespace(
    what_happened="x", confidence="low", state_label="Neutral", strong_areas=[], risks=[], why=[]
)


def _state(results=None):
    """最小可用 state：report 非空（否则 Critic 直接短路返回 research_more）。"""
    return {
        "report": _REPORT,
        "results": results or [],
        "evidence": [],
        "gate": None,
        "question": "贵州茅台今天怎么样",
        "domain": _DOMAIN,
    }


def _executed_result(registry_key: str) -> ToolResult:
    """构造一条「已拿到数据」的 ToolResult（tool 存的是 operationId，不是 registry key）。"""
    return ToolResult(tool=_tool_name_of(registry_key), arguments={}, status="success")


# ------------------------------------------------------------------ #
# T15 / T16                                                           #
# ------------------------------------------------------------------ #


async def test_critic_prompt_lists_supplementary_tools():
    """T15：prompt 含「可补充的工具」小节；已拿到数据的工具那行被剔除；落库保留合法 key。"""
    node = _make_node(
        json.dumps(
            {
                "verdict": "research_more",
                "reason": "证据不足",
                "missing_points": ["缺少资金面证据"],
                "missing_tool_keys": [_GAP_KEY],
            }
        )
    )
    out = await node(_state([_executed_result(_EXECUTED_KEY)]))

    prompt = node.client.chat.completions.last_prompt
    assert prompt is not None
    assert "=== 可补充的工具（本域注册表，已排除已拿到数据的工具）===" in prompt
    # 未执行的缺口工具必须在 prompt 里（否则 Critic 根本无从点名）
    assert f"- {_GAP_KEY}:" in prompt
    # 已拿到数据的工具那一行必须被剔除（不能提示 Critic 再点它）
    assert f"- {_EXECUTED_KEY}:" not in prompt
    assert out["critique"].missing_tool_keys == [_GAP_KEY]
    assert out.get("errors", []) == []


async def test_critic_drops_hallucinated_and_executed_gap_keys():
    """T16：幻觉 key / 已拿到数据的 key 被丢弃，落库只剩合法 key，且不算错误。"""
    node = _make_node(
        json.dumps(
            {
                "verdict": "research_more",
                "reason": "证据不足",
                "missing_points": ["缺少资金面证据"],
                "missing_tool_keys": ["totally_hallucinated_key", _EXECUTED_KEY, _GAP_KEY],
            }
        )
    )
    out = await node(_state([_executed_result(_EXECUTED_KEY)]))

    assert out["critique"].missing_tool_keys == [_GAP_KEY]
    assert out.get("errors", []) == []
