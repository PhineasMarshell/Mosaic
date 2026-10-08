"""research_more 回环的缺口上下文与步骤修补（纯函数，无 IO / 无 LLM）。

背景：Critic 判 research_more 时只把控制权交回 Supervisor（回环边不携带 payload），
而 Supervisor._plan() 只读 conversation_id/domain/question —— 第二轮既不知道 Critic
指出的缺口，也不知道上一轮跑过哪些工具（route 被整体覆盖）。本模块渲染 planner 可见的
缺口上下文，并在代码层做两件确定性的事：

1. drop_repeat_steps：丢掉「同工具已拿到数据、且计划参数被已执行参数覆盖」的步骤；
2. append_gap_steps：把 Critic 点名、LLM 没覆盖、且在本次注册表文本里的缺口工具补进 plan 头部。

注意 ToolResult.tool 存的是 gateway operationId（tool_name），不是 registry key；
需要 registry key 时一律走 tool_registry.resolve_tool_by_name()。同一 operationId 可被
多个域条目复用（quote/search、klines/snapshot/window），解析取的是注册表的规范条目
（cross 优先）——因此「已拿到数据」的判定对复用同一 operationId 的兄弟条目是**粗粒度**的：
一个 key 跑过会把兄弟 key 也标成已执行。这只会让补充更保守（少补而不是多补），不会造假。
"""

from __future__ import annotations

from typing import Any

from app.models.research import ToolCallPlan

#: 视为「已拿到数据」的状态 —— error 不算，允许回环轮重试失败的工具
_DONE_STATUSES = ("success", "partial")

#: 这些 tool_name 前缀的工具必须带 query，缺口补齐时用用户问题兜底
_QUERY_TOOL_PREFIXES = ("news_",)


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """兼容 dict / pydantic 对象取值（与 critic._field 同语义）。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def executed_index(results: Any) -> dict[str, list[dict]]:
    """state["results"] → {tool_name: [arguments, ...]}（只含 success/partial）。"""
    index: dict[str, list[dict]] = {}
    for r in results or []:
        if _field(r, "status") not in _DONE_STATUSES:
            continue
        name = _field(r, "tool", "")
        if not name:
            continue
        args = _field(r, "arguments", {}) or {}
        index.setdefault(name, []).append(dict(args))
    return index


def executed_keys(results: Any) -> set[str]:
    """已拿到数据的工具对应的 registry key 集合（不在注册表中的工具名忽略）。"""
    from app.gateway.tool_registry import resolve_tool_by_name

    keys: set[str] = set()
    for name in executed_index(results):
        try:
            keys.add(resolve_tool_by_name(name).key)
        except KeyError:
            continue
    return keys


def allowed_keys(registry: str) -> set[str]:
    """从 registry_text() 输出里抽出全部 key（行格式 `- {key}: {tool_name} [...]`）。"""
    keys: set[str] = set()
    for line in registry.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and ":" in stripped:
            keys.add(stripped[2:].split(":", 1)[0].strip())
    return keys


def render_unexecuted_registry(registry: str, results: Any) -> str:
    """注册表文本去掉「已拿到数据的工具」那些行，供 Critic 挑补充工具。"""
    executed = executed_keys(results)
    lines = [
        line
        for line in registry.splitlines()
        if not (line.strip().startswith("- ") and line.strip()[2:].split(":", 1)[0].strip() in executed)
    ]
    return "\n".join(lines) or "（无可补充的工具）"


def _args_subset(planned: dict, executed: dict) -> bool:
    """planned 的参数是否被 executed 覆盖（`{}` 视为总是被覆盖）。"""
    for k, v in planned.items():
        if k not in executed or executed[k] != v:
            return False
    return True


def is_repeat_step(step: ToolCallPlan, results: Any) -> bool:
    """该 step 是否为「同工具已拿到数据、且参数已被已执行调用覆盖」的重复。"""
    from app.gateway.tool_registry import resolve_tool

    try:
        meta = resolve_tool(step.tool_key)
    except KeyError:
        return False
    for args in executed_index(results).get(meta.tool_name, []):
        if _args_subset(dict(step.arguments or {}), args):
            return True
    return False


def drop_repeat_steps(steps: list[ToolCallPlan], results: Any) -> tuple[list[ToolCallPlan], list[str]]:
    """丢掉重复步骤 → (保留的 steps, 被丢弃的 tool_key 列表)。"""
    kept: list[ToolCallPlan] = []
    dropped: list[str] = []
    for step in steps:
        if is_repeat_step(step, results):
            dropped.append(step.tool_key)
        else:
            kept.append(step)
    return kept, dropped


def sanitize_missing_tool_keys(raw: Any, allowed: set[str], executed: set[str]) -> tuple[list[str], list[str]]:
    """过滤 Critic 给的缺口 key：只留 allowed 里、未拿到数据、去重后的 key。

    Returns:
        (合法 keys, 被丢弃的 keys)
    """
    kept: list[str] = []
    dropped: list[str] = []
    for key in raw or []:
        if not isinstance(key, str) or not key:
            continue
        if key in allowed and key not in executed and key not in kept:
            kept.append(key)
        else:
            dropped.append(key)
    return kept, dropped


def gap_tool_keys(critique: Any, registry: str, results: Any) -> tuple[list[str], list[str]]:
    """Critic 的缺口 key ∩ 本次注册表可见 key − 已拿到数据的 key。"""
    return sanitize_missing_tool_keys(
        _field(critique, "missing_tool_keys", []),
        allowed_keys(registry),
        executed_keys(results),
    )


def append_gap_steps(
    steps: list[ToolCallPlan],
    gap_keys: list[str],
    registry: str,
    max_steps: int,
    max_gap_steps: int,
    question: str = "",
) -> tuple[list[ToolCallPlan], list[str]]:
    """把缺口 key 以 high 优先级补进 steps **头部**（幂等）再截断 → (新 steps, 补入的 key)。"""
    from app.gateway.tool_registry import resolve_tool

    allowed = allowed_keys(registry)
    present = {s.tool_key for s in steps}
    forced: list[ToolCallPlan] = []
    for key in gap_keys:
        if key in present or key not in allowed or len(forced) >= max_gap_steps:
            continue
        try:
            meta = resolve_tool(key)
        except KeyError:
            continue
        arguments: dict[str, Any] = {}
        if meta.tool_name.startswith(_QUERY_TOOL_PREFIXES):
            arguments["query"] = question
        forced.append(
            ToolCallPlan(
                tool_key=key,
                arguments=arguments,
                purpose="Critic 指出的证据缺口（代码级补齐）",
                priority="high",
            )
        )
        present.add(key)
    merged = forced + list(steps)
    return merged[:max_steps], [s.tool_key for s in forced]


def render_gap_context(critique: Any, results: Any, max_steps: int) -> str:
    """渲染回环轮上下文块（塞进 PLANNER_PROMPT 的 {revision_context}）。"""
    if critique is None:
        return "（首轮，无缺口信息）"
    reason = _field(critique, "reason", "") or "（未给出）"
    missing_points = _field(critique, "missing_points", []) or []
    missing_keys = _field(critique, "missing_tool_keys", []) or []
    lines = [
        "Critic 判定: research_more",
        f"Critic 理由: {reason}",
        "Critic 指出的证据缺口（自然语言）:",
    ]
    lines.extend([f"  - {p}" for p in missing_points] or ["  - （无）"])
    lines.append("Critic 建议补充的工具 key（必须全部列入 steps）:")
    lines.extend([f"  - {k}" for k in missing_keys] or ["  - （无）"])
    lines.append("已执行且已拿到数据的工具（不要重复规划这些调用）:")
    index = executed_index(results)
    if index:
        for name, arg_list in sorted(index.items()):
            rendered = "; ".join(str(a) for a in arg_list[:3]) or "{}"
            lines.append(f"  - {name} 参数: {rendered}")
    else:
        lines.append("  - （无）")
    lines.append(f"本轮最多规划 {max_steps} 个步骤。")
    return "\n".join(lines)
