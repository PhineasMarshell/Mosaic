"""Supervisor 节点 — LLM 路由：解析意图 + 选 analyst + 分配工具预算。

复用 PLANNER_PROMPT + llm_json.parse_json_object，
输出 ResearchState.intent + route，被各 analyst 节点消费。
"""

import logging
import time

from openai import AsyncOpenAI

from app.agent.prompts_graph import PLANNER_PROMPT
from app.config import Settings
from app.gateway.tool_registry import registry_text
from app.graph.gap_loop import (
    append_gap_steps,
    coverage_requirements,
    drop_repeat_steps,
    gap_key_decision,
    render_gap_context,
)
from app.graph.market_plan import apply_as_of_date_to_plan, apply_market_summary_prefix
from app.llm_json import parse_json_object
from app.models.research import DEFAULT_DOMAINS, ResearchPlan
from app.research.trading_calendar import TradingDateSemantics, resolve_trading_date_semantics

logger = logging.getLogger(__name__)

# 默认全部启用；后续可通过配置关闭某些域
_ENABLED_DOMAINS: list = DEFAULT_DOMAINS


def set_enabled_domains(domains: list) -> None:
    """动态设置启用的市场域（供外部覆盖）。"""
    global _ENABLED_DOMAINS
    _ENABLED_DOMAINS = domains


def route_candidate_categories(settings) -> tuple[str, ...]:
    """返回当前启用的 analyst category 元组。

    supervisor._build_route 分组与 builder._supervisor_fanout 扇出共用此函数，
    避免两处写死白名单。news/sentiment 开关关闭时，对应 category 的工具
    会被 _build_route 归入 technical（维持 P2.5 行为）。
    """
    cats = ["technical", "fundamental", "moneyflow"]
    if getattr(settings, "news_enabled", False):
        cats.append("news")
    if getattr(settings, "sentiment_enabled", False):
        cats.append("sentiment")
    return tuple(cats)


class SupervisorNode:
    """LLM 路由节点：用户问题 → intent + route。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=settings.llm_timeout_seconds,
        )
        #: 阶段 0：最近一次 _plan 代码级补齐的缺口 key（写进 run log 的 plan 事件）
        self._last_forced_gap: list[str] = []
        self._gap_state: dict = {}

    async def __call__(self, state):
        """执行路由决策，返回要写入 state 的字段字典。"""
        try:
            from app.graph import run_log

            # Normalize state to dict (handle both ResearchState and dict)
            if hasattr(state, "model_dump"):
                s = state.model_dump(exclude_none=False)
            else:
                s = state
            if not s.get("run_id"):
                run_log.log_run_start(s)
            plan, filtered_keys, plan_adjustments = await self._plan(s)
            if s.get("date_reason") is not None:
                date_reason = s["date_reason"]
            elif plan.intent.domain != "a_share":
                date_reason = None
            elif plan.requested_date is None:
                date_reason = "requested_date_unknown"
            elif plan.market_closed is None:
                date_reason = "calendar_unknown"
            elif plan.as_of_date is None:
                date_reason = "no_available_trading_day"
            elif plan.market_closed:
                date_reason = "market_closed_using_previous_trading_day"
            elif plan.as_of_date != plan.requested_date:
                date_reason = "evidence_as_of_differs_from_requested_date"
            else:
                date_reason = "requested_date_is_trading_day"
            result: dict = {
                "intent": plan.intent,
                "route": self._build_route(plan),
                "requested_date": plan.requested_date,
                "planned_as_of_date": plan.as_of_date,
                "market_closed": plan.market_closed,
                "date_reason": date_reason,
            }
            result.update(getattr(self, "_gap_state", {}))
            # 阶段 6：本节点是 `research_more → supervisor` 回环的落点，只在这里增
            # `research_round_count`（首次规划没有 critique，不算一轮研究）。
            # 它与 `rewrite_count` 各自独立设限，互不挤占额度。
            if s.get("critique"):
                research_round_count = int(s.get("research_round_count", 0) or 0) + 1
                result["research_round_count"] = research_round_count
                result["revision_count"] = int(s.get("rewrite_count", 0) or 0) + research_round_count
            # 阶段 0：把"计划了什么 / 哪些 key 被域守卫挡掉 / 代码级补了哪些缺口"
            # 写进结构化运行日志——这是事后解释"为什么没补工具"的第一现场。
            from app.graph import run_log

            run_log.log_plan(
                s,
                plan,
                filtered_keys=filtered_keys,
                forced_gap=self._last_forced_gap,
                prefix_keys=plan_adjustments[0],
                unexecutable_keys=plan_adjustments[1],
            )
            # B6 步 3：域守卫 —— 被过滤的 step 不执行，错误记账（不新增 LLM 调用）
            if filtered_keys:
                result["errors"] = [
                    f"tool_call_filtered: {key}（不在本次域过滤后的注册表文本中，疑似幻觉/串域，已跳过）"
                    for key in filtered_keys
                ]
            return result
        except Exception as exc:
            # 路由失败写进 errors，kernel 仍可继续尝试（只是没有显式路由）
            return {"errors": [f"Supervisor routing failed: {exc}"]}

    async def _plan(self, state):
        """生成研究计划（包装 Planner.plan 逻辑）。

        Returns:
            (plan, 被域守卫过滤掉的 step key 列表, (市场级前缀 key 列表, 必然跳过的 step key 列表))
        """
        conv_history = ""
        conv_id = state.get("conversation_id") if isinstance(state, dict) else getattr(state, "conversation_id", None)
        if conv_id:
            from app.memory.storage import get_memory

            memory = get_memory()
            conv_history = memory.get_conversation_history(conv_id, self.settings.max_conversation_turns)

        # B6 步 1：显式域存在时按域过滤注册表（planner 只看到该域 + cross 的工具）；
        # 无显式域时保持全量 —— planner 要自己判域，此时不能预先过滤。
        # ⚠️ "cross" 必须带上：klines/snapshot/window 的注册域是 cross，crypto 侧的
        # 行情能力全靠它们；registry_text 是严格等值匹配，不会自动带上 cross。
        explicit_domain = state.get("domain") if isinstance(state, dict) else getattr(state, "domain", None)
        if explicit_domain:
            registry = registry_text(domains=[explicit_domain, "cross"])
        else:
            registry = registry_text()
        question = state.get("question", "") if isinstance(state, dict) else getattr(state, "question", "")

        # 回环预检：Critic 的所有缺口都不可见/已满足/被截断时，不再调用 Planner。
        # 这条路径没有任何可执行补查，直接把空计划交给 finalize，避免烧掉一轮
        # 无法执行的 LLM 规划。
        critique = state.get("critique") if isinstance(state, dict) else getattr(state, "critique", None)
        results = state.get("results", []) if isinstance(state, dict) else getattr(state, "results", [])
        preflight = gap_key_decision(
            critique,
            registry,
            results,
            max_keys=getattr(self.settings, "gap_max_steps", 3),
        ) if critique else None
        verdict = str(critique.get("verdict", "") if isinstance(critique, dict) else getattr(critique, "verdict", "")).lower()
        if preflight and verdict == "research_more" and preflight.raw and not preflight.kept:
            from app.models.research import ResearchIntent

            intent = state.get("intent") if isinstance(state, dict) else getattr(state, "intent", None)
            if isinstance(intent, dict):
                intent = ResearchIntent.model_validate(intent)
            if intent is None:
                intent = ResearchIntent(domain=explicit_domain or "a_share", question=question)
            intent.requested_date = state.get("requested_date")
            intent.as_of_date = state.get("planned_as_of_date")
            intent.market_closed = state.get("market_closed")
            self._last_forced_gap = []
            self._gap_state = {
                "gap_key_decisions": preflight.as_state(),
                "executable_gap_steps": [],
                "finalize_after_gap": True,
                "blocked_reason": "no_executable_gap_steps",
            }
            return ResearchPlan(
                intent=intent,
                steps=[],
                requested_date=state.get("requested_date"),
                as_of_date=state.get("planned_as_of_date"),
                market_closed=state.get("market_closed"),
            ), [], ([], [])

        # research_more 回环：Critic 的缺口与上一轮已执行的工具都不在回环边上，
        # 必须由本节点从 state 读出后渲染进 planner prompt（信息闭环）。
        revision_context = render_gap_context(critique, results, self.settings.max_research_steps)

        prompt = PLANNER_PROMPT.format(
            question=question,
            registry=registry,
            max_steps=self.settings.max_research_steps,
            enabled_domains=", ".join(_ENABLED_DOMAINS),
            conversation_history=conv_history if conv_history else "（无）",
            revision_context=revision_context,
        )

        _llm_started = time.perf_counter()
        response = await self.client.chat.completions.create(
            model=self.settings.openai_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是 Mosaic 的 Supervisor。"
                        "必须只返回合法 JSON object，不要包含任何 Markdown 代码块标记、"
                        "不要加开头或结尾的文字解释。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        # 阶段 6：规划这次 LLM 调用的用量/耗时（指标数据源；回环轮记 phase=replan）。
        from app.graph import run_log

        run_log.log_llm_call(
            state,
            node="supervisor",
            phase="replan" if state.get("critique") else "initial",
            model=self.settings.openai_model,
            usage=getattr(response, "usage", None),
            duration_ms=(time.perf_counter() - _llm_started) * 1000,
        )

        content = response.choices[0].message.content or "{}"
        data = parse_json_object(content, source="Supervisor")

        plan = ResearchPlan.model_validate(data)

        # 如果显式指定了域且 planner 自动判断不一致，以显式指定为准
        if explicit_domain is not None:
            plan.intent.domain = explicit_domain

        # A research_more round can cross midnight.  Once a run has resolved
        # "today", reuse that date instead of consulting the clock again.
        date_semantics = (
            TradingDateSemantics(
                requested_date=state.get("requested_date"),
                as_of_date=state.get("planned_as_of_date"),
                market_closed=state.get("market_closed"),
                reason=state.get("date_reason") or "",
            )
            if state.get("date_reason") is not None
            else resolve_trading_date_semantics(question)
        )
        # The calendar is A-share specific.  Keep non-A-share market closure
        # unknown rather than treating a weekend as a crypto/US closure.
        if plan.intent.domain == "a_share":
            plan.intent.requested_date = date_semantics.requested_date
            plan.intent.as_of_date = date_semantics.as_of_date
            plan.intent.market_closed = date_semantics.market_closed
            plan.requested_date = date_semantics.requested_date
            plan.as_of_date = date_semantics.as_of_date
            plan.market_closed = date_semantics.market_closed
        else:
            plan.intent.requested_date = state.get("requested_date")
            plan.intent.as_of_date = None
            plan.intent.market_closed = None
            plan.requested_date = state.get("requested_date")
            plan.as_of_date = None
            plan.market_closed = None

        # 截断步骤数
        plan.steps = plan.steps[: self.settings.max_research_steps]

        # B6 步 3：零成本域守卫 —— 每个 step 的 key 必须出现在本次渲染出来的
        # 注册表文本里；不在则不执行该 step（防 LLM 幻觉出旧名或串域工具，
        # 如给美股问题挑 binance 的 snapshot）。不拦「在文本里但不适用于该
        # symbol」的工具——那靠 cross 条目的 purpose 边界声明 + critic 纠偏。
        filtered_keys = [s.tool_key for s in plan.steps if f"- {s.tool_key}:" not in registry]
        if filtered_keys:
            plan.steps = [s for s in plan.steps if f"- {s.tool_key}:" in registry]

        # 阶段 3 ②③：A 股市场综述的**确定性最低计划前缀**。LLM 只负责补充额外
        # 工具；市场级最小证据集由代码注入并置于计划最前面，同时剔除"需要 symbol
        # 却拿不到 symbol"的步骤（旧行为是计划 overview → 执行层静默跳过 →
        # 零市场级数据下撰写报告，即"假覆盖"）。个股/指数问题不受影响。
        prefix_keys, unexecutable_keys = apply_market_summary_prefix(
            plan,
            question=question,
            registry=registry,
            max_steps=self.settings.max_research_steps,
        )
        apply_as_of_date_to_plan(plan, plan.as_of_date)

        # 回环轮（research_more）的代码级闭环：先丢掉已拿到数据的重复步骤，
        # 再把 Critic 点名、LLM 没覆盖、且在本次注册表文本里的缺口工具补到最前面
        # （预算耗尽前优先执行）。快乐路径不写 errors，保住 errors == [] 断言。
        decision = gap_key_decision(
            critique,
            registry,
            results,
            max_keys=getattr(self.settings, "gap_max_steps", 3),
        )
        gap_keys = decision.kept
        if decision.dropped:
            logger.warning(
                "Supervisor: Critic 缺口 key 分类 raw=%s kept=%s dropped=%s reasons=%s",
                decision.raw,
                decision.kept,
                decision.dropped,
                decision.reason_counts(),
            )

        plan.steps, dropped_repeat = drop_repeat_steps(plan.steps, results)
        if dropped_repeat:
            logger.info("Supervisor: 回环轮丢弃已拿到数据的重复步骤: %s", dropped_repeat)

        plan.steps, forced_gap = append_gap_steps(
            plan.steps,
            gap_keys,
            registry,
            max_steps=self.settings.max_research_steps,
            max_gap_steps=getattr(self.settings, "gap_max_steps", 3),
            question=question,
            # 逐条 issue 的覆盖要求（含互相冲突的 arguments 变体）：每个尚未被满足的
            # 变体各补一条独立步骤，让"同一工具、不同参数"的多次调用真实进入计划。
            requirements=coverage_requirements(critique),
            results=results,
        )
        # Gap steps are appended after the initial plan pass.  Apply the same
        # transport contract to those steps before routing them to Gateway.
        apply_as_of_date_to_plan(plan, plan.as_of_date)
        if forced_gap:
            logger.info("Supervisor: 代码级补齐 Critic 缺口工具: %s", forced_gap)
        self._last_forced_gap = list(forced_gap)
        prior_disposition = []
        if critique is not None:
            raw_prior = critique.get("gap_key_decisions", []) if isinstance(critique, dict) else getattr(critique, "gap_key_decisions", [])
            prior_disposition = list(raw_prior or [])
        planned_gap = [step.tool_key for step in plan.steps if step.tool_key in gap_keys]
        current_disposition = decision.as_state()
        for row in current_disposition:
            if row["decision"] == "kept" and row["key"] not in planned_gap:
                row.update(decision="dropped", reason="budget_truncated")
        disposition = prior_disposition + [row for row in current_disposition if row not in prior_disposition]
        # Critic 要求补证据但本轮没有任何可执行步骤时，直接进入 finalize，
        # 避免空 route 触发默认三 analyst 扇出再烧一轮。
        no_executable_gap = bool(critique and (decision.raw or prior_disposition) and not planned_gap)
        self._gap_state = {
            "gap_key_decisions": disposition,
            "executable_gap_steps": planned_gap,
            "finalize_after_gap": no_executable_gap,
            "blocked_reason": "no_executable_gap_steps" if no_executable_gap else None,
        }
        return plan, filtered_keys, (prefix_keys, unexecutable_keys)

    def _build_route(self, plan: ResearchPlan) -> list:
        """将 ResearchPlan.steps 按工具 category 分组为 AnalystAssignment 列表。"""
        from app.gateway.arguments import canonicalize_tool_arguments
        from app.gateway.tool_registry import resolve_tool

        groups: dict[str, list] = {}
        allowed = route_candidate_categories(self.settings)
        for step in plan.steps:
            try:
                meta = resolve_tool(step.tool_key)
                step.arguments = canonicalize_tool_arguments(meta.tool_name, dict(step.arguments or {}))
                cat = meta.category
            except Exception:
                cat = "technical"  # 无法解析的工具键兜底给技术面
            if cat not in allowed:
                cat = "technical"  # 未启用的 category 暂归技术面
            groups.setdefault(cat, []).append(step)
        return [
            {"analyst": cat, "tool_calls": [s.model_dump() for s in steps], "budget": len(steps)}
            for cat, steps in groups.items()
        ]
