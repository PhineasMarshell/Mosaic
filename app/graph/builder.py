"""LangGraph 图的构建工厂。

P3 拓扑 ::

    supervisor ──┬──► kernel       ──┐
                 ├──► technical     ──┤
                 ├──► fundamental   ──├──► gate ──► reasoning ──► critic ──► END
                 └──► moneyflow    ──┘                 ▲            │   /|\\
                                                       │            │   / | \\
                                                       └────────────┘ revise/research_more/≤N

节点职责：
- supervisor: LLM 路由 → intent + _route_raw（复用 planner prompt）
- kernel: 整包调用 MarketDetective.investigate()（兜底执行层，P5 删除）
- technical / fundamental / moneyflow: 按 category 分类执行工具（P3 新增）
- gate: 代码级证据门控
- reasoning: 工具结果 → MarketIntelligence report
- critic: Report vs Evidence 审计 → pass / revise / research_more

P5：删除 KernelNode，三 analyst Send 边成为唯一执行路径。
"""

from langgraph.graph import END, StateGraph

from app.config import Settings
from app.graph.nodes.analysts.fundamental import FundamentalAnalystNode
from app.graph.nodes.analysts.moneyflow import MoneyflowAnalystNode
from app.graph.nodes.analysts.technical import TechnicalAnalystNode
from app.graph.nodes.critic import CriticNode
from app.graph.nodes.gate import GateNode
from app.graph.nodes.kernel import KernelNode
from app.graph.nodes.reasoning import ReasoningNode
from app.graph.nodes.supervisor import SupervisorNode
from app.graph.state import ResearchState


def build_graph(settings: Settings):
    """构建研究调查图。

    P3 拓扑：supervisor → [kernel + technical + fundamental + moneyflow] → gate → reasoning → critic
    四个执行节点并行，结果通过 reducer 合并到 state.results / state.evidence。

    Args:
        settings: 应用配置对象

    Returns:
        编译后的 CompiledGraph，可直接调用 ainvoke / stream
    """
    builder = StateGraph(ResearchState)

    # ── 注册节点 ────────────────────────────────────────────
    supervisor_node = SupervisorNode(settings)
    kernel_node = KernelNode(settings)
    technical_node = TechnicalAnalystNode(settings)
    fundamental_node = FundamentalAnalystNode(settings)
    moneyflow_node = MoneyflowAnalystNode(settings)
    gate_node = GateNode(settings)
    reasoning_node = ReasoningNode(
        settings, critic_max_revisions=settings.critic_max_revisions
    )
    critic_node = CriticNode(settings)

    builder.add_node("supervisor", supervisor_node)
    builder.add_node("kernel", kernel_node)
    builder.add_node("technical", technical_node)
    builder.add_node("fundamental", fundamental_node)
    builder.add_node("moneyflow", moneyflow_node)
    builder.add_node("gate", gate_node)
    builder.add_node("reasoning", reasoning_node)
    builder.add_node("critic", critic_node)

    # ── 入口 ────────────────────────────────────────────────
    builder.set_entry_point("supervisor")

    # ── P3 并行扇出：supervisor → 四个执行节点 ──────────────
    # kernel（整包兜底）与三个 analyst 同时执行，
    # 各自写 results / evidence / findings（reducer 合并）
    builder.add_edge("supervisor", "kernel")
    builder.add_edge("supervisor", "technical")
    builder.add_edge("supervisor", "fundamental")
    builder.add_edge("supervisor", "moneyflow")

    # ── 四个执行节点 → gate ──────────────────────────────────
    builder.add_edge("kernel", "gate")
    builder.add_edge("technical", "gate")
    builder.add_edge("fundamental", "gate")
    builder.add_edge("moneyflow", "gate")

    # ── 线性路径：gate → reasoning → critic ─────────────────
    builder.add_edge("gate", "reasoning")
    builder.add_edge("reasoning", "critic")

    # ── Critic 条件边 ──────────────────────────────────────
    def _critic_route(state):
        # Normalize state to dict (handle both ResearchState and dict)
        if hasattr(state, "model_dump"):
            state = state.model_dump(exclude_none=False)

        critique = state.get("critique")
        if not critique or not hasattr(critique, "verdict"):
            return "end"

        revision_count = state.get("revision_count", 0)
        verdict = critique.verdict

        if verdict == "revise":
            if revision_count < settings.critic_max_revisions:
                return "reasoning"
            return "end"

        if verdict == "research_more":
            if revision_count < settings.critic_max_revisions:
                return "supervisor"
            return "end"

        return "end"  # "pass" or unknown

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
