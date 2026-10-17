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

from app.gateway.arguments import ArgumentValidationError, canonicalize_tool_arguments, semantic_signature
from app.models.research import ResearchIntent, ResearchPlan, ToolCallPlan

logger = logging.getLogger(__name__)

#: A 股大盘基准：上证指数 / 沪深300 / 创业板指。腾讯行情对指数要求带交易所
#: 前缀，裸 ``000300`` 在传输层会变成空行情，因此代码口径一律用规范写法。
A_SHARE_BENCHMARK_SYMBOLS: tuple[str, ...] = ("SH000001", "SH000300", "SZ399006")

#: 向后兼容别名：旧代码（与旧测试）把它当作单个沪深300 代码使用。
A_SHARE_BENCHMARK_SYMBOL = "SH000300"

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
        {"symbols": list(A_SHARE_BENCHMARK_SYMBOLS)},
        "A股大盘基准：上证指数 / 沪深300 / 创业板指当日表现（点位与涨跌幅）",
    ),
    ("sentiment", {}, "A股市场情绪：风险偏好 / 涨跌家数"),
    ("limit_up_count", {}, "A股当日涨停家数（市场活跃度）"),
    ("limit_up_sectors", {}, "A股当日涨停题材与板块分布（热点结构）"),
    ("telegraph", {}, "全市场盘面快讯（消息面 / 事件维度）"),
)

# The allowed Gateway contract lists no query parameters for these endpoints.
# A planned observation date is an audit target, not a supported transport arg.
UNPARAMETERIZED_LIMIT_UP_TOOLS: frozenset[str] = frozenset({"limit_up_count", "limit_up_sectors"})

#: 这些市场级端点在 Gateway 契约里同样没有日期参数；planner/critic 偶尔会带上
#: ``{"date": ...}``，清成 ``{}`` 才能与代码口径的前缀步骤合并为同一个变体，
#: 避免"同一工具两个参数变体"造成重复调用。
UNPARAMETERIZED_DATE_TOOLS: frozenset[str] = UNPARAMETERIZED_LIMIT_UP_TOOLS | frozenset({"sentiment", "telegraph", "market_breadth"})
# ``limit_up_pool`` retains its requested date in the research plan: distinct
# historical coverage issues are independent variants, and THS consumes the
# date in its query. A current-only fallback cannot satisfy that variant unless
# the returned observation date matches it.


def _default_quote_arguments() -> dict[str, Any]:
    return {"symbols": list(A_SHARE_BENCHMARK_SYMBOLS)}


def quote_symbols(step: Any) -> list[str]:
    """Read the quote targets of a planned step without inventing any."""
    arguments = getattr(step, "arguments", None) or {}
    values = arguments.get("symbols")
    if isinstance(values, (list, tuple)):
        return [str(value) for value in values if str(value).strip()]
    single = arguments.get("symbol")
    if single:
        return [str(single)]
    return []


def _is_explicit_multi_index_quote(step: Any) -> bool:
    """True for a quote step the planner/critic deliberately asked for.

    A single symbol (often one stock code) is not a market-level benchmark
    request; two or more targets are an explicit multi-index ask and must not
    be overwritten by the code-owned default.
    """
    return len(quote_symbols(step)) >= 2


def apply_as_of_date_to_plan(plan: ResearchPlan, as_of_date: str | None) -> ResearchPlan:
    """Keep the date target on the plan without inventing Gateway parameters."""
    plan.as_of_date = as_of_date
    plan.intent.as_of_date = as_of_date
    for step in plan.steps:
        if step.tool_key in UNPARAMETERIZED_DATE_TOOLS:
            step.arguments = {}
    return plan


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
    from app.gateway.stock_codes import STOCK_CODE_MAP_A_SHARE

    return _STOCK_CODE_RE.search(question or "") is None and not any(
        name in (question or "") and symbol.startswith(("SH", "SZ")) for name, symbol in STOCK_CODE_MAP_A_SHARE.items()
    )


def _in_registry(tool_key: str, registry: str) -> bool:
    """与 supervisor 的域守卫同一约定：key 必须出现在本次渲染的注册表文本里。"""
    return f"- {tool_key}:" in registry


def _dedupe_steps(plan: ResearchPlan) -> None:
    """One tool + one argument variant = at most one planned step.

    The ``{date}`` variant of an unparameterized market tool and its ``{}``
    variant are the same call; keeping both would execute the same endpoint
    twice and inflate the budget ledger.  First occurrence wins (prefix steps
    come first, so the code-owned variant survives).
    """
    from app.gateway.tool_registry import resolve_tool

    unique: list[ToolCallPlan] = []
    seen: set[str] = set()
    for step in plan.steps:
        try:
            operation_id = resolve_tool(step.tool_key).tool_name
            signature = semantic_signature(operation_id, canonicalize_tool_arguments(operation_id, step.arguments or {}))
        except (KeyError, ArgumentValidationError):
            signature = semantic_signature(step.tool_key, step.arguments or {})
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(step)
    plan.steps = unique


def unexecutable_steps(plan: ResearchPlan, question: str) -> list[str]:
    """返回"计划了但执行层必然跳过"的 step key。

    与 ``analysts/base.py`` 的符号守卫逐字对应：``requires_symbol=True`` 且
    ``arguments`` 里没有 ``symbol``，且问题里也抽不出 6 位代码 → analyst 会
    打一条 warning 后 ``continue``。这类 step 留在计划里只会制造假覆盖。
    """
    from app.gateway.stock_codes import STOCK_CODE_MAP_A_SHARE
    from app.gateway.tool_registry import resolve_tool

    has_code_in_question = _STOCK_CODE_RE.search(question or "") is not None or any(
        name in (question or "") and symbol.startswith(("SH", "SZ")) for name, symbol in STOCK_CODE_MAP_A_SHARE.items()
    )
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
    include_sentiment: bool | None = None,
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
        # Supervisor passes the runtime switch.  ``None`` preserves the
        # historical direct-call behavior used by plan-level tests and tools.
        if tool_key == "sentiment" and include_sentiment is False:
            logger.info("市场级最低证据集跳过 sentiment（sentiment_enabled=False）")
            continue
        if not _in_registry(tool_key, registry):
            logger.warning("市场级最低证据集跳过 %s（不在本次域过滤后的注册表文本中）", tool_key)
            continue
        existing = next((s for s in plan.steps if s.tool_key == tool_key), None)
        if existing is not None:
            if tool_key == "quote" and _is_explicit_multi_index_quote(existing):
                # Planner/critic 已明确点名多个指数口径 —— 保留它，不再用代码
                # 口径覆盖（否则会把 critic 的多指数请求压成单一口径）。
                prefix_keys.append(tool_key)
                logger.info("市场级最低证据集保留 planner/critic 的多指数 quote 参数：%s", existing.arguments)
                continue
            if (existing.arguments or {}) == arguments:
                # 已经是同一变体：原地复用，只把它升为高优先级，绝不复制成两条。
                existing.priority = "high"
                prefix_keys.append(tool_key)
                continue
            # 单个标的口径（常是个股）不是市场级基准请求：删除并由代码口径基准承担。
            plan.steps = [s for s in plan.steps if s.tool_key != tool_key]
            if existing.arguments:
                logger.info(
                    "市场级最低证据集用代码口径覆盖 planner 的 %s 参数：%s → %s",
                    tool_key,
                    existing.arguments,
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
    _dedupe_steps(plan)
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
