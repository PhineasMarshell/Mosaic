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

from langgraph.graph import END, StateGraph

from app.config import Settings
from app.graph.nodes.analysts.fundamental import FundamentalAnalystNode
from app.graph.nodes.analysts.moneyflow import MoneyflowAnalystNode
from app.graph.nodes.analysts.news import NewsAnalystNode
from app.graph.nodes.analysts.technical import TechnicalAnalystNode
from app.graph.nodes.critic import CriticNode
from app.graph.nodes.gate import GateNode
from app.graph.nodes.reasoning import ReasoningNode
from app.graph.nodes.supervisor import SupervisorNode, route_candidate_categories
from app.graph.state import ResearchState


def critic_route_decision(state, settings: Settings) -> str:
    """Critic 条件边的路由决策（模块级，便于单元测试）。

    兼容 ResearchState 对象与 dict —— LangGraph reducer / model_dump 会把嵌套的
    Critique 模型序列化为 dict，因此不能用属性访问。

    Returns:
        "end" / "reasoning"（revise 回退）/ "supervisor"（research_more 回退）
    """
    if hasattr(state, "model_dump"):
        state = state.model_dump(exclude_none=False)

    critique = state.get("critique")
    if not critique:
        return "end"

    if isinstance(critique, dict):
        verdict = critique.get("verdict")
    else:
        verdict = getattr(critique, "verdict", None)
    if not verdict:
        return "end"

    revision_count = state.get("revision_count", 0) or 0

    if verdict == "revise":
        if revision_count < settings.critic_max_revisions:
            return "reasoning"
        return "end"

    if verdict == "research_more":
        if revision_count < settings.critic_max_revisions:
            return "supervisor"
        return "end"

    return "end"  # "pass" or unknown


def build_graph(settings: Settings):
    """构建研究调查图。

    拓扑（P5 收尾后）：supervisor → [technical + fundamental + moneyflow]
    （+ news 可选，由 settings.news_enabled 控制）→ gate → reasoning → critic；
    critic 按 verdict 回 reasoning（revise）或回 supervisor（research_more），
    最多 critic_max_revisions 轮。各 analyst 并行，结果通过 reducer 合并到
    state.results / state.evidence。

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
    reasoning_node = ReasoningNode(settings, critic_max_revisions=settings.critic_max_revisions)
    critic_node = CriticNode(settings)

    builder.add_node("supervisor", supervisor_node)
    builder.add_node("technical", technical_node)
    builder.add_node("fundamental", fundamental_node)
    builder.add_node("moneyflow", moneyflow_node)
    if settings.news_enabled:
        news_node = NewsAnalystNode(settings)
        builder.add_node("news", news_node)
    builder.add_node("gate", gate_node)
    builder.add_node("reasoning", reasoning_node)
    builder.add_node("critic", critic_node)

    # ── 入口 ────────────────────────────────────────────────
    builder.set_entry_point("supervisor")

    # ── 条件扇出：supervisor → [route 分配的 analyst]，空路由时三 analyst 兜底 ──
    def _supervisor_fanout(state):
        if hasattr(state, "model_dump"):
            state = state.model_dump(exclude_none=False)
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
        },
    )

    # ── Compile ─────────────────────────────────────────────
    return builder.compile()
