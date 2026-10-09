"""LangGraph 图的构建工厂。

拓扑 ::

    supervisor ──┬──► technical     ──┐
                 ├──► fundamental   ──├──► gate ──► reasoning ──► critic ──► END
                 ├──► moneyflow     ──┘                 ▲            │   /|\
                 └──► news (可选)                       │            │  / | \
                                                       └────────────┘ revise/research_more/≤N

节点职责：
- supervisor: LLM 路由 → intent + route（复用 planner prompt）
- technical / fundamental / moneyflow: 按 category 分类执行工具
- news（可选，NEWS_ENABLED 控制）: DDGS 新闻搜索 + 事件归因
- gate: 代码级证据门控
- reasoning: 工具结果 → MarketIntelligence report
- critic: Report vs Evidence 审计 → pass / revise / research_more
"""

import logging
import time

from langgraph.graph import END, StateGraph

from app.config import Settings
from app.graph.nodes.analysts.fundamental import FundamentalAnalystNode
from app.graph.nodes.analysts.moneyflow import MoneyflowAnalystNode
from app.graph.nodes.analysts.news import NewsAnalystNode
from app.graph.nodes.analysts.sentiment import SentimentAnalystNode
from app.graph.nodes.analysts.technical import TechnicalAnalystNode
from app.graph.nodes.critic import CriticNode
from app.graph.nodes.finalize import FinalizeAuditNode
from app.graph.nodes.gate import GateNode
from app.graph.nodes.reasoning import ReasoningNode
from app.graph.nodes.supervisor import SupervisorNode, route_candidate_categories
from app.graph.state import ResearchState

logger = logging.getLogger(__name__)


def remaining_budget_seconds(state: dict) -> float | None:
    """整次调查的剩余预算（秒）。

    没有 ``budget_deadline`` 时返回 ``None`` —— 不把"没有预算信息"当成"预算充足"，
    也不伪造一个数字；调用方必须显式决定这种情况怎么处理。
    """
    deadline = state.get("budget_deadline")
    if deadline is None:
        return None
    try:
        return float(deadline) - time.monotonic()
    except (TypeError, ValueError):
        return None


def research_round_budget(state: dict, settings: Settings) -> dict[str, object]:
    """确定性计算 research_more 是否还值得启动。

    同时检查墙钟预算、工具调用余额和 reasoning/critic 的最低保留时间。
    这只是门控，不会启动任何工具或 LLM。
    """
    remaining = remaining_budget_seconds(state)
    results = state.get("results") or []
    used_calls = len(results)
    max_calls = int(getattr(settings, "max_tool_calls", 0) or 0)
    tool_remaining = max_calls - used_calls if max_calls > 0 else None
    required_calls = max(1, int(getattr(settings, "research_round_min_tool_calls", 1) or 1))
    required_minimum = max(
        float(getattr(settings, "research_round_min_remaining_seconds", 0) or 0),
        float(getattr(settings, "research_tool_min_budget_seconds", 0) or 0)
        + float(getattr(settings, "reasoning_min_budget_seconds", 0) or 0)
        + float(getattr(settings, "critic_min_budget_seconds", 0) or 0),
    )
    reasons: list[str] = []
    if remaining is not None and remaining < required_minimum:
        reasons.append("time_budget_insufficient")
    if tool_remaining is not None and tool_remaining < required_calls:
        reasons.append("tool_call_budget_insufficient")
    return {
        "budget_remaining": remaining,
        "required_minimum": required_minimum,
        "tool_calls_remaining": tool_remaining,
        "required_tool_calls": required_calls,
        "decision": "allow" if not reasons else "deny",
        "reason": reasons[0] if reasons else "research_budget_available",
    }


def critic_route_decision(state, settings: Settings) -> str:
    """Critic 条件边的路由决策（模块级，便于单元测试）。

    兼容 ResearchState 对象与 dict —— LangGraph reducer / model_dump 会把嵌套的
    Critique 模型序列化为 dict，因此不能用属性访问。

    阶段 6：两条回环路径**分别计数、分别设限**，并且 ``research_more`` 在剩余预算
    不足以跑完"半轮工具 + reasoning + critic"时不再启动回环（直接进安全终态）。

    Returns:
        "end"（仅 verdict=pass 走这里）/ "reasoning"（revise 回退）/
        "supervisor"（research_more 回退）/ "finalize_audit"（非 pass 的安全终态）
    """
    if hasattr(state, "model_dump"):
        state = state.model_dump(exclude_none=False)

    from app.graph import run_log

    rewrite_count = int(state.get("rewrite_count", 0) or 0)
    research_round_count = int(state.get("research_round_count", 0) or 0)
    max_rewrites = settings.effective_max_rewrites
    max_research_rounds = settings.effective_max_research_rounds

    def _decide(decision: str, reason: str) -> str:
        budget_info = research_round_budget(state, settings) if decision in ("supervisor", "finalize_audit") else {}
        gap_decisions = state.get("gap_key_decisions") or []
        if not gap_decisions:
            critique_obj = state.get("critique")
            if isinstance(critique_obj, dict):
                gap_decisions = critique_obj.get("gap_key_decisions") or []
            else:
                gap_decisions = getattr(critique_obj, "gap_key_decisions", []) or []
        executable = state.get("executable_gap_steps") or []
        blocked_reason = state.get("blocked_reason")
        run_log.log_route(
            state,
            decision,
            rewrite_count=rewrite_count,
            research_round_count=research_round_count,
            revision_count=rewrite_count + research_round_count,
            max_rewrites=max_rewrites,
            max_research_rounds=max_research_rounds,
            reason=reason,
            budget_remaining=budget_info.get("budget_remaining"),
            required_minimum=budget_info.get("required_minimum"),
            tool_calls_remaining=budget_info.get("tool_calls_remaining"),
            required_tool_calls=budget_info.get("required_tool_calls"),
            gap_key_decisions=gap_decisions,
            executable_gap_steps=executable,
            blocked_reason=blocked_reason or (reason if decision == "finalize_audit" else None),
        )
        return decision

    critique = state.get("critique")
    if not critique:
        return _decide("end", "no_critique")

    if isinstance(critique, dict):
        verdict = str(critique.get("verdict", "")).strip().lower()
    else:
        verdict = str(getattr(critique, "verdict", "")).strip().lower()

    # T11：内部 error（审计自身失败 / verdict 无法识别）或空 verdict：
    # 交给 finalize_audit 判 failed —— 绝不静默 END 冒充正常完成。
    if not verdict or verdict == "error":
        return _decide("finalize_audit", "critic_error")

    if verdict == "revise":
        if rewrite_count < max_rewrites:
            return _decide("reasoning", "revise_allowed")
        # 阶段 5：轮次耗尽不再静默 END —— 先过 finalize_audit 落终态。
        return _decide("finalize_audit", "rewrite_budget_exhausted")

    if verdict == "research_more":
        # report 为 None 说明是 reasoning 引擎失败（非缺数据），
        # 重新走 supervisor 无法解决，直接进终态避免无限循环。
        if not state.get("report"):
            return _decide("finalize_audit", "no_report_to_revise")
        if research_round_count >= max_research_rounds:
            return _decide("finalize_audit", "research_budget_exhausted")
        gap_decisions = state.get("gap_key_decisions") or (
            critique.get("gap_key_decisions", []) if isinstance(critique, dict)
            else getattr(critique, "gap_key_decisions", [])
        )
        if gap_decisions and not any(
            isinstance(row, dict) and row.get("decision") == "kept" for row in gap_decisions
        ):
            return _decide("finalize_audit", "no_executable_gap_steps")
        # 回环前同时检查墙钟、工具调用余额和 reasoning/critic 最低预算。
        budget_info = research_round_budget(state, settings)
        if budget_info["decision"] == "deny":
            return _decide("finalize_audit", str(budget_info["reason"]))
        if state.get("finalize_after_gap"):
            return _decide("finalize_audit", "no_executable_gap_steps")
        return _decide("supervisor", "research_allowed")

    if verdict == "pass":
        return _decide("end", "audit_passed")

    # T11：理论上 Critic 已拦截非法 verdict；兜底仍按安全终止处理（不当 pass）。
    logger.error("critic_route_decision 遇到未预期 verdict %r，安全终止", verdict)
    return _decide("finalize_audit", "unknown_verdict")


def build_graph(settings: Settings):
    """构建研究调查图。

    拓扑（P5 收尾后）：supervisor → [technical + fundamental + moneyflow]
    （+ news 可选，由 settings.news_enabled 控制）→ gate → reasoning → critic；
    critic 按 verdict 回 reasoning（revise）或回 supervisor（research_more），
    两条路径**分别**最多 `max_rewrites` / `max_research_rounds` 轮（默认各 1）。
    各 analyst 并行，结果通过 reducer 合并到 state.results / state.evidence。

    Args:
        settings: 应用配置对象

    Returns:
        编译后的 CompiledGraph，可直接调用 ainvoke / stream
    """
    builder = StateGraph(ResearchState)

    # ── 注册节点 ────────────────────────────────────────────
    supervisor_node = SupervisorNode(settings)
    technical_node = TechnicalAnalystNode(settings)
    fundamental_node = FundamentalAnalystNode(settings)
    moneyflow_node = MoneyflowAnalystNode(settings)
    gate_node = GateNode(settings)
    reasoning_node = ReasoningNode(settings, max_rewrites=settings.effective_max_rewrites)
    critic_node = CriticNode(settings)

    builder.add_node("supervisor", supervisor_node)
    builder.add_node("technical", technical_node)
    builder.add_node("fundamental", fundamental_node)
    builder.add_node("moneyflow", moneyflow_node)
    if settings.news_enabled:
        news_node = NewsAnalystNode(settings)
        builder.add_node("news", news_node)
    if settings.sentiment_enabled:
        sentiment_node = SentimentAnalystNode(settings)
        builder.add_node("sentiment", sentiment_node)
    builder.add_node("gate", gate_node)
    builder.add_node("reasoning", reasoning_node)
    builder.add_node("critic", critic_node)
    # 阶段 5：非 pass 的安全终态节点（只有"没通过审计"的路径会经过它）
    builder.add_node("finalize_audit", FinalizeAuditNode(settings))

    # ── 入口 ────────────────────────────────────────────────
    builder.set_entry_point("supervisor")

    # ── 条件扇出：supervisor → [route 分配的 analyst]，空路由时三 analyst 兜底 ──
    def _supervisor_fanout(state):
        if hasattr(state, "model_dump"):
            state = state.model_dump(exclude_none=False)
        if state.get("finalize_after_gap"):
            return "finalize_audit"
        route = state.get("route") or []
        assigned = {a.get("analyst") for a in route if isinstance(a, dict)}
        candidates = route_candidate_categories(settings)
        names = [n for n in candidates if n in assigned]
        if not names:
            return ["technical", "fundamental", "moneyflow"]
        return names

    _fanout_map = {
        "technical": "technical",
        "fundamental": "fundamental",
        "moneyflow": "moneyflow",
        "finalize_audit": "finalize_audit",
    }
    if settings.news_enabled:
        _fanout_map["news"] = "news"
    if settings.sentiment_enabled:
        _fanout_map["sentiment"] = "sentiment"

    builder.add_conditional_edges(
        "supervisor",
        _supervisor_fanout,
        _fanout_map,
    )

    # ── 执行节点 → gate ──────────────────────────────────
    builder.add_edge("technical", "gate")
    builder.add_edge("fundamental", "gate")
    builder.add_edge("moneyflow", "gate")
    if settings.news_enabled:
        builder.add_edge("news", "gate")
    if settings.sentiment_enabled:
        builder.add_edge("sentiment", "gate")

    # ── 线性路径：gate → reasoning → critic ─────────────────
    builder.add_edge("gate", "reasoning")
    builder.add_edge("reasoning", "critic")

    # ── Critic 条件边（决策逻辑见模块级 critic_route_decision）──
    def _critic_route(state):
        return critic_route_decision(state, settings)

    builder.add_conditional_edges(
        "critic",
        _critic_route,
        {
            "end": END,
            "reasoning": "reasoning",
            "supervisor": "supervisor",
            "finalize_audit": "finalize_audit",
        },
    )

    # ── 终态：finalize_audit → END（写 delivery_status，不产生新数据）──
    builder.add_edge("finalize_audit", END)

    # ── Compile ─────────────────────────────────────────────
    return builder.compile()
