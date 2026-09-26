"""LangGraph 图的构建工厂。

P0 拓扑 ::

    supervisor ──► kernel ── gate ── reasoning ── critic ── END
                                          ▲            │   /|\
                                          │            │   / | \
                                          └────────────┘ revise/research_more/≤N

节点职责（P0）：
- supervisor: LLM 路由 → intent + _route_raw（复用 planner prompt）
- kernel: 整包调用 MarketDetective.investigate()（兜底执行层）
- gate: 代码级证据门控
- reasoning: 工具结果 → MarketIntelligence report
- critic: Report vs Evidence 审计 → pass / revise / research_more

P3：替换 kernel 为 [technical, fundamental, moneyflow] Send 并行扇出；
P5：删除 KernelNode，Send 边成为唯一执行路径。
"""

from langgraph.graph import END, StateGraph

from app.config import Settings
from app.graph.nodes.critic import CriticNode
from app.graph.nodes.gate import GateNode
from app.graph.nodes.kernel import KernelNode
from app.graph.nodes.reasoning import ReasoningNode
from app.graph.nodes.supervisor import SupervisorNode
from app.graph.state import ResearchState


def build_graph(settings: Settings):
    """构建研究调查图（P0：单节点整包调用 MarketDetective）。

    Args:
        settings: 应用配置对象

    Returns:
        编译后的 CompiledGraph，可直接调用 ainvoke / stream
    """
    builder = StateGraph(ResearchState)

    # ── 注册节点 ────────────────────────────────────────────
    supervisor_node = SupervisorNode(settings)
    kernel_node = KernelNode(settings)
    gate_node = GateNode(settings)
    reasoning_node = ReasoningNode(
        settings, critic_max_revisions=settings.critic_max_revisions
    )
    critic_node = CriticNode(settings)

    builder.add_node("supervisor", supervisor_node)
    builder.add_node("kernel", kernel_node)
    builder.add_node("gate", gate_node)
    builder.add_node("reasoning", reasoning_node)
    builder.add_node("critic", critic_node)

    # ── 入口 ────────────────────────────────────────────────
    builder.set_entry_point("supervisor")

    # ── 线性路径（P0） ─────────────────────────────────────
    builder.add_edge("supervisor", "kernel")
    builder.add_edge("kernel", "gate")
    builder.add_edge("gate", "reasoning")

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
