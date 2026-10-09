"""阶段 3：A 股市场综述的确定性最低计划前缀。

问题（方案 §2 根因 / §4 阶段 3）：planner 是自由 LLM，而"今天 A 股发生了什么"
这种不含个股代码的问题，历史行为是计划 ``overview``（get_company_overview）——
它必须有 symbol，执行层于是静默跳过，报告在**零市场级数据**下撰写。计划层
与执行层对同一份计划给出不同结论，就是"假覆盖"。

做法：
1. 市场级最小证据集由**代码**在 LLM 计划之后注入，并置于计划最前面
   （预算耗尽前优先执行）；planner 只负责补充额外工具。
2. 注入时一并剔除"需要 symbol 却拿不到 symbol"的步骤——宁可计划里没有，
   也不要计划了必跳过。这一步只在市场级问题上做，避免影响个股深度问题。

个股/指数问题（问题里带 6 位代码）不加前缀：那类问题要的是单标的深挖，
硬塞市场级工具会挤掉用户真正要的调查预算。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.models.research import ResearchIntent, ResearchPlan, ToolCallPlan
from app.gateway.arguments import ArgumentValidationError, canonicalize_tool_arguments, semantic_signature

logger = logging.getLogger(__name__)

#: A 股大盘基准：沪深300。腾讯行情对指数用纯数字代码（见 ``stock_codes.INDEX_CODE_MAP_A_SHARE``），
#: PLANNER_PROMPT 也把 "000300" 列为 A 股 quote 的合法取值。
A_SHARE_BENCHMARK_SYMBOL = "000300"

#: 6 位证券代码：命中即视为"点名了具体标的"，不加市场级前缀。
_STOCK_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")

#: 判定为"市场级总结"的 intent.task。
MARKET_TASKS: frozenset[str] = frozenset({"market_summary", "market_diagnosis"})

#: 最低证据集（方案 §3.2）：(tool_key, arguments, purpose)。
#: 有意**不含** ``limit_up_pool``（涨停池明细）——它只在需要点名个股 /
#: 比较板块密度 / Critic 点名缺口时调用，属于"额外工具"，由 planner 决定。
MARKET_SUMMARY_MINIMUM: tuple[tuple[str, dict[str, Any], str], ...] = (
    (
        "quote",
        {"symbols": [A_SHARE_BENCHMARK_SYMBOL]},
        "A股大盘基准：沪深300 指数当日表现（点位与涨跌幅）",
    ),
    ("sentiment", {}, "A股市场情绪：风险偏好 / 涨跌家数"),
    ("limit_up_count", {}, "A股当日涨停家数（市场活跃度）"),
    ("limit_up_sectors", {}, "A股当日涨停题材与板块分布（热点结构）"),
    ("telegraph", {}, "全市场盘面快讯（消息面 / 事件维度）"),
)


def is_market_level_question(intent: ResearchIntent | Any, question: str) -> bool:
    """是否需要注入 A 股市场级最低证据集。

    三个条件全部满足才注入：

    1. ``domain == "a_share"`` —— 最低证据集里的工具都是 A 股口径；
    2. ``task`` 属于 :data:`MARKET_TASKS` —— 个股研究用不上市场级聚合；
    3. 问题里**没有** 6 位证券代码 —— 有点名标的 = 深挖单标的（验收要求
       "输入『今天 A 股发生了什么？』不含 6 位代码时"）。
    """
    if getattr(intent, "domain", None) != "a_share":
        return False
    if getattr(intent, "task", None) not in MARKET_TASKS:
        return False
    return _STOCK_CODE_RE.search(question or "") is None


def _in_registry(tool_key: str, registry: str) -> bool:
    """与 supervisor 的域守卫同一约定：key 必须出现在本次渲染的注册表文本里。"""
    return f"- {tool_key}:" in registry


def unexecutable_steps(plan: ResearchPlan, question: str) -> list[str]:
    """返回"计划了但执行层必然跳过"的 step key。

    与 ``analysts/base.py`` 的符号守卫逐字对应：``requires_symbol=True`` 且
    ``arguments`` 里没有 ``symbol``，且问题里也抽不出 6 位代码 → analyst 会
    打一条 warning 后 ``continue``。这类 step 留在计划里只会制造假覆盖。
    """
    from app.gateway.tool_registry import resolve_tool

    has_code_in_question = _STOCK_CODE_RE.search(question or "") is not None
    dropped: list[str] = []
    for step in plan.steps:
        try:
            meta = resolve_tool(step.tool_key)
        except Exception:
            continue  # 解析不了的 key 交给域守卫处理
        if not getattr(meta, "requires_symbol", True):
            continue
        arguments = step.arguments or {}
        if arguments.get("symbol") or arguments.get("symbols") or has_code_in_question:
            continue
        dropped.append(step.tool_key)
    return dropped


def apply_market_summary_prefix(
    plan: ResearchPlan,
    *,
    question: str,
    registry: str,
    max_steps: int,
) -> tuple[list[str], list[str]]:
    """注入 A 股市场级最低证据集，返回 ``(prefix_keys, dropped_unexecutable_keys)``。

    - 前缀步骤用**代码拥有的** canonical arguments（例如 quote 用沪深300），
      LLM 同名步骤被替换（市场级问题上，指数口径由代码决定，不由 LLM 猜）；
    - 前缀 key 不在本次注册表文本里则跳过（域过滤后不可见 = 不该计划）；
    - 结果整体截断到 ``max_steps``：最低证据集在前，被挤掉的是 LLM 的额外步骤。
    """
    prefix_keys: list[str] = []
    dropped: list[str] = []

    # Canonicalize planner output before any route/unexecutable decision.  This
    # keeps plan logs, route construction and execution on the same arguments.
    canonical_steps: list[ToolCallPlan] = []
    seen: set[str] = set()
    for step in plan.steps:
        try:
            from app.gateway.tool_registry import resolve_tool

            operation_id = resolve_tool(step.tool_key).tool_name
            args = canonicalize_tool_arguments(operation_id, step.arguments or {})
        except (KeyError, ArgumentValidationError):
            args = dict(step.arguments or {})
        step.arguments = args
        signature = semantic_signature(step.tool_key, args)
        if signature not in seen:
            seen.add(signature)
            canonical_steps.append(step)
    plan.steps = canonical_steps

    if not is_market_level_question(plan.intent, question):
        return prefix_keys, dropped

    dropped = unexecutable_steps(plan, question)
    if dropped:
        dropped_set = set(dropped)
        plan.steps = [s for s in plan.steps if s.tool_key not in dropped_set]

    prefix_steps: list[ToolCallPlan] = []
    for tool_key, arguments, purpose in MARKET_SUMMARY_MINIMUM:
        if not _in_registry(tool_key, registry):
            logger.warning("市场级最低证据集跳过 %s（不在本次域过滤后的注册表文本中）", tool_key)
            continue
        replaced = [s for s in plan.steps if s.tool_key == tool_key]
        if replaced:
            plan.steps = [s for s in plan.steps if s.tool_key != tool_key]
            for old in replaced:
                if (old.arguments or {}) and old.arguments != arguments:
                    logger.info(
                        "市场级最低证据集用代码口径覆盖 planner 的 %s 参数：%s → %s",
                        tool_key,
                        old.arguments,
                        arguments,
                    )
        prefix_steps.append(
            ToolCallPlan(
                tool_key=tool_key,
                arguments=dict(arguments),
                purpose=purpose,
                priority="high",
            )
        )
        prefix_keys.append(tool_key)

    if not prefix_steps:
        return prefix_keys, dropped

    plan.steps = (prefix_steps + plan.steps)[:max_steps]
    # A planner quote can be removed and replaced by the canonical prefix.  It
    # is executable now, so it must not remain in unexecutable_keys telemetry.
    present_keys = {step.tool_key for step in plan.steps}
    dropped = [key for key in dropped if key not in present_keys]
    if dropped:
        # 旧行为：这些 step 留在计划里，执行层打一条 warning 后静默跳过 ——
        # 计划文本与实际执行不一致，报告却在"零数据"下撰写。现在如实剔除并记账。
        logger.warning(
            "市场级问题：从计划中剔除 %d 个必然被跳过的步骤（需要 symbol 却拿不到 symbol）: %s",
            len(dropped),
            dropped,
        )
    logger.info(
        "A 股市场级最低证据集已注入: %s（剔除必然跳过的步骤: %s）",
        prefix_keys,
        dropped or "无",
    )
    return prefix_keys, dropped
