"""Supervisor 回环缺口闭环：planner prompt 注入缺口上下文 + 代码级丢弃重复 / 补齐缺口。

本文件 mock 了什么
------------------
- **LLM**：节点级用例把 ``SupervisorNode.client`` 换成 fake（``chat.completions.
  create`` 返回固定 plan JSON，并把实际发出的 prompt 记进 ``last_prompt``），
  与 ``tests/test_b6_domain_filter.py:79-111`` 的 ``FakeOpenAI`` 同一手法。
- **settings**：用 ``types.SimpleNamespace`` 只给 ``_plan`` 真正读到的字段
  （``max_conversation_turns`` / ``max_research_steps`` / ``openai_model`` /
  ``gap_max_steps``）。被测节点本体（``SupervisorNode`` 真实实例）照常跑。
- **工具 key / tool_name**：不硬编码 —— 一律用 ``ALL_TOOLS`` / ``registry_text()``
  探测式取得，避免工具集演进后断言腐烂。

因此**没有覆盖**
----------------
- 真实 LLM 的实际规划质量（fake 返回的是固定 JSON）；
- 真实 gateway 调用与 gateway 的 operationId 契约（只构造 ``ToolResult`` 值对象）；
- analyst 节点的实际执行链路（``app/graph/nodes/analysts/base.py`` 的 budget/
  signature 去重不在本文件的验证范围）；
- 真实网络、真实 ``AppSettings`` 校验、真实域名过滤之外的图拓扑行为。

T35 约定：本文件不替换任何被测节点的 ``__call__``；节点级用例跑真实
``SupervisorNode.__call__`` / ``_plan`` 骨架，并断言 ``errors == []``。
"""

import types

import pytest

from app.gateway.tool_registry import ALL_TOOLS, registry_text
from app.graph.nodes.supervisor import SupervisorNode
from app.models.market import ToolResult
from app.models.research import ToolCallPlan

# ------------------------------------------------------------------ #
# 探测式取材：不硬编码工具 key / tool_name                              #
# ------------------------------------------------------------------ #


def _probe_news_tool():
    """探测一个需要 ``query`` 的新闻工具（``tool_name`` 以 ``news_`` 开头）。"""
    for meta in ALL_TOOLS:
        if meta.tool_name.startswith("news_"):
            return meta
    pytest.skip("注册表里没有 news_* 工具，无法验证 query 兜底")


_PLAIN_TOOL = ALL_TOOLS[0]
_NEWS_TOOL = next((t for t in ALL_TOOLS if t.tool_name.startswith("news_")), None)


def _result(tool_name: str, status: str = "success", arguments: dict | None = None) -> ToolResult:
    return ToolResult(tool=tool_name, arguments=arguments or {}, status=status)


def _step(tool_key: str, arguments: dict | None = None, priority: str = "medium") -> ToolCallPlan:
    return ToolCallPlan(tool_key=tool_key, arguments=arguments or {}, purpose="p", priority=priority)


# ------------------------------------------------------------------ #
# T1 / T2：已执行索引                                                 #
# ------------------------------------------------------------------ #


def test_executed_index_only_counts_data():
    """T1：只有 success/partial 算「已拿到数据」；error 不进索引（允许回环重试）。"""
    from app.graph.gap_loop import executed_index

    results = [
        _result("a", "success"),
        _result("b", "partial"),
        _result("c", "error"),
    ]
    index = executed_index(results)
    assert set(index) == {"a", "b"}
    assert "c" not in index


def test_executed_keys_maps_tool_name_to_key():
    """T2：ToolResult.tool 是 operationId，executed_keys 必须映射回 registry key。"""
    from app.gateway.tool_registry import resolve_tool_by_name
    from app.graph.gap_loop import executed_keys

    meta = _PLAIN_TOOL
    canonical_key = resolve_tool_by_name(meta.tool_name).key
    assert executed_keys([_result(meta.tool_name)]) == {canonical_key}
    # 未知 operationId 静默忽略，不抛异常
    assert executed_keys([_result("not_a_real_operation_id")]) == set()


# ------------------------------------------------------------------ #
# T3 / T4：重复步骤识别（参数子集规则 / error 可重试）                  #
# ------------------------------------------------------------------ #


def test_drop_repeat_steps_subset_rule():
    """T3：参数被已执行调用覆盖 → 丢弃；参数不同 → 保留（换 symbol 是合法新调用）。"""
    from app.graph.gap_loop import drop_repeat_steps

    meta = _PLAIN_TOOL
    results = [_result(meta.tool_name, "success", {"symbol": "600519"})]
    steps = [
        _step(meta.key, {}),  # 空参 = 总是被覆盖 → 丢
        _step(meta.key, {"symbol": "600519"}),  # 完全覆盖 → 丢
        _step(meta.key, {"symbol": "000001"}),  # 参数不同 → 留
    ]
    kept, dropped = drop_repeat_steps(steps, results)
    assert [s.tool_key for s in kept] == [meta.key]
    assert [s.arguments for s in kept] == [{"symbol": "000001"}]
    assert dropped == [meta.key, meta.key]


def test_drop_repeat_steps_keeps_retry_after_error():
    """T4：上一次 error 的同工具同参数必须保留 —— 这是有意的重试通道。"""
    from app.graph.gap_loop import drop_repeat_steps

    meta = _PLAIN_TOOL
    results = [_result(meta.tool_name, "error", {"symbol": "600519"})]
    kept, dropped = drop_repeat_steps([_step(meta.key, {"symbol": "600519"})], results)
    assert [s.tool_key for s in kept] == [meta.key]
    assert dropped == []


# ------------------------------------------------------------------ #
# 节点级：回环轮 prompt 与 route                                       #
# ------------------------------------------------------------------ #


class _FakeCompletions:
    """返回固定 plan JSON，并记录实际发出的 user prompt。"""

    def __init__(self, payload: str):
        self.payload = payload
        self.last_prompt: str | None = None

    async def create(self, **kwargs):
        messages = kwargs.get("messages") or []
        self.last_prompt = "\n".join(
            m.get("content", "") for m in messages if isinstance(m, dict) and m.get("role") == "user"
        )
        msg = types.SimpleNamespace(content=self.payload)
        choice = types.SimpleNamespace(message=msg)
        return types.SimpleNamespace(choices=[choice])


def _make_node(payload: str, gap_max_steps: int = 3):
    """真实 ``SupervisorNode`` 实例 + fake client（只替换 client，不替换节点）。"""
    node = SupervisorNode.__new__(SupervisorNode)
    node.settings = types.SimpleNamespace(
        max_conversation_turns=10,
        max_research_steps=8,
        openai_model="test-model",
        gap_max_steps=gap_max_steps,
    )
    fake = _FakeCompletions(payload)
    node.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=fake))
    return node, fake


def _plan_payload(*tool_keys: str) -> str:
    steps = ",".join(f'{{"tool_key":"{k}","arguments":{{}},"purpose":"p"}}' for k in tool_keys)
    return (
        '{"intent":{"domain":"a_share","task":"market_diagnosis",'
        '"time_scope":"today","question":"q"},'
        f'"steps":[{steps}]}}'
    )


def _revision_state(critique: dict, results: list | None = None) -> dict:
    return {
        "question": "贵州茅台今天怎么样",
        "domain": "a_share",
        "critique": critique,
        "results": results if results is not None else [],
    }


_GAP_TEXT = "缺少资金费率数据"


@pytest.mark.asyncio
async def test_revision_round_prompt_includes_gap_section():
    """T11：回环轮 planner prompt 必须含 Critic 缺口 + 已执行工具清单。"""
    node, fake = _make_node(_plan_payload(_PLAIN_TOOL.key))
    gap_meta = next(m for m in ALL_TOOLS if m.key != _PLAIN_TOOL.key and m.domain in ("a_share", "cross"))
    executed = _result(_PLAIN_TOOL.tool_name, "success", {"symbol": "600519"})
    state = _revision_state(
        {
            "verdict": "research_more",
            "reason": "证据不足",
            "missing_points": [_GAP_TEXT],
            "missing_tool_keys": [gap_meta.key],
        },
        [executed],
    )

    out = await node(state)

    assert fake.last_prompt is not None
    assert _GAP_TEXT in fake.last_prompt
    assert _PLAIN_TOOL.tool_name in fake.last_prompt
    assert "Critic 建议补充的工具 key" in fake.last_prompt
    assert out.get("errors", []) == []


@pytest.mark.asyncio
async def test_first_round_prompt_has_no_gap_section():
    """T12：首轮（无 critique）只出现「无缺口信息」占位，不得出现回环硬要求文本。"""
    node, fake = _make_node(_plan_payload(_PLAIN_TOOL.key))

    out = await node({"question": "贵州茅台今天怎么样", "domain": "a_share", "results": []})

    assert "（首轮，无缺口信息）" in fake.last_prompt
    assert "Critic 建议补充的工具 key（必须全部列入 steps）" not in fake.last_prompt
    assert _GAP_TEXT not in fake.last_prompt
    assert out.get("errors", []) == []


@pytest.mark.asyncio
async def test_planner_ignoring_gap_still_gets_tool_in_route():
    """T13：LLM 只规划已执行过的重复工具 → 代码级丢弃重复 + 缺口 key 必须进 route。"""
    gap_meta = next(m for m in ALL_TOOLS if m.key != _PLAIN_TOOL.key and m.domain in ("a_share", "cross"))
    node, _fake = _make_node(_plan_payload(_PLAIN_TOOL.key))
    state = _revision_state(
        {
            "verdict": "research_more",
            "reason": "证据不足",
            "missing_points": [_GAP_TEXT],
            "missing_tool_keys": [gap_meta.key],
        },
        [_result(_PLAIN_TOOL.tool_name, "success", {"symbol": "600519"})],
    )

    out = await node(state)

    routed = [c["tool_key"] for r in out["route"] for c in r["tool_calls"]]
    assert gap_meta.key in routed
    assert _PLAIN_TOOL.key not in routed
    assert out.get("errors", []) == []


@pytest.mark.asyncio
async def test_gap_key_not_in_registry_is_not_forced():
    """T14：Critic 给幻觉 key → 不补进 route，只写 logger.warning，errors 仍为空。"""
    node, _fake = _make_node(_plan_payload(_PLAIN_TOOL.key))
    state = _revision_state(
        {
            "verdict": "research_more",
            "reason": "证据不足",
            "missing_points": [_GAP_TEXT],
            "missing_tool_keys": ["totally_hallucinated_key"],
        },
        [],
    )

    out = await node(state)

    routed = [c["tool_key"] for r in out["route"] for c in r["tool_calls"]]
    assert "totally_hallucinated_key" not in routed
    assert out.get("errors", []) == []


# ------------------------------------------------------------------ #
# T5–T7：缺口补齐（头部插入 / 幂等 / 幻觉与截断 / news query 兜底）      #
# ------------------------------------------------------------------ #


def _registry_for(tool_key: str) -> str:
    """取包含该 key 的域过滤注册表文本（a_share + cross）。"""
    return registry_text(domains=["a_share", "cross"])


def _gap_keys_of(registry: str) -> list[str]:
    from app.graph.gap_loop import allowed_keys

    return sorted(k for k in allowed_keys(registry) if k != _PLAIN_TOOL.key)


def test_append_gap_steps_prepends_and_idempotent():
    """T5：缺口 key 补在 steps **首位** 且 priority=high；已含该 key 时不重复补。"""
    from app.graph.gap_loop import append_gap_steps

    gap_key = _gap_keys_of(_registry_for(_PLAIN_TOOL.key))[0]
    original = [_step(_PLAIN_TOOL.key)]
    merged, forced = append_gap_steps(original, [gap_key], _registry_for(gap_key), max_steps=8, max_gap_steps=3)
    assert forced == [gap_key]
    assert [s.tool_key for s in merged] == [gap_key, _PLAIN_TOOL.key]
    assert merged[0].priority == "high"

    # 幂等：对已含该 key 的 steps 再补一次，不重复补
    again, forced_again = append_gap_steps(merged, [gap_key], _registry_for(gap_key), max_steps=8, max_gap_steps=3)
    assert forced_again == []
    assert [s.tool_key for s in again] == [gap_key, _PLAIN_TOOL.key]


def test_append_gap_steps_rejects_unknown_and_truncates():
    """T6：幻觉 key 不补；max_gap_steps 生效；结果长度 ≤ max_steps。"""
    from app.graph.gap_loop import append_gap_steps

    keys = _gap_keys_of(_registry_for(_PLAIN_TOOL.key))
    registry = _registry_for(_PLAIN_TOOL.key)
    merged, forced = append_gap_steps(
        [_step(_PLAIN_TOOL.key)],
        ["hallucinated_key", *keys],
        registry,
        max_steps=3,
        max_gap_steps=2,
    )
    assert "hallucinated_key" not in forced
    assert len(forced) == 2
    assert len(merged) <= 3
    # 补入的在头部（后面的 _PLAIN_TOOL.key 可被 max_steps 截掉）
    assert [s.tool_key for s in merged][:2] == forced


def test_append_gap_steps_fills_query_for_news_tools():
    """T7：query 型新闻工具补齐时必须带 query（用用户问题兜底），否则 gateway 参数校验失败。"""
    from app.graph.gap_loop import append_gap_steps

    meta = _probe_news_tool()
    merged, forced = append_gap_steps(
        [],
        [meta.key],
        registry_text(domains=[meta.domain, "cross"]),
        max_steps=8,
        max_gap_steps=3,
        question="今天AI板块有什么新闻",
    )
    assert forced == [meta.key]
    assert merged[0].arguments.get("query") == "今天AI板块有什么新闻"


# ------------------------------------------------------------------ #
# T8：sanitize_missing_tool_keys                                      #
# ------------------------------------------------------------------ #


def test_sanitize_missing_tool_keys_filters_and_dedups():
    """T8：非字符串/空串/不可见/已执行/重复 → dropped，其余 kept（保持顺序）。"""
    from app.graph.gap_loop import sanitize_missing_tool_keys

    kept, dropped = sanitize_missing_tool_keys(
        ["good", "good", "invisible", "", None, "done"],
        allowed={"good", "done"},
        executed={"done"},
    )
    assert kept == ["good"]
    assert dropped == ["good", "invisible", "done"]
    assert "" not in dropped and None not in dropped  # 非法类型直接跳过，不进 dropped


# ------------------------------------------------------------------ #
# T9–T10：render_gap_context                                          #
# ------------------------------------------------------------------ #


def test_render_gap_context_first_round():
    """T9：无 critique → 明确的「首轮」占位。"""
    from app.graph.gap_loop import render_gap_context

    assert render_gap_context(None, [], 8) == "（首轮，无缺口信息）"


def test_render_gap_context_contains_gap_and_executed():
    """T10：回环轮文本含 reason / missing_points / missing_tool_keys / 已执行 tool_name。"""
    from app.graph.gap_loop import render_gap_context

    text = render_gap_context(
        {
            "verdict": "research_more",
            "reason": "缺少资金面证据",
            "missing_points": [_GAP_TEXT],
            "missing_tool_keys": [_PLAIN_TOOL.key],
        },
        [_result(_PLAIN_TOOL.tool_name, "success", {"symbol": "600519"})],
        max_steps=8,
    )
    assert "缺少资金面证据" in text
    assert _GAP_TEXT in text
    assert _PLAIN_TOOL.key in text
    assert _PLAIN_TOOL.tool_name in text
    assert "600519" in text
    assert "本轮最多规划 8 个步骤。" in text
