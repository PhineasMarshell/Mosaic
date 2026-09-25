"""Orchestrator — LangGraph 图入口。

P0：build_graph() + ainvoke(state) 整包替换原有 MarketDetective.investigate()，
公开签名（question/domain/conversation_id → ResearchResponse）完全不变 ——
main.py 与现有测试零改动。
"""

from openai import AsyncOpenAI

from app.config import Settings
from app.graph.builder import build_graph
from app.graph.state import ResearchState
from app.models.research import MarketDomain
from app.models.response import ResearchResponse


class Orchestrator:
    def __init__(self, settings: Settings):
        self.settings = settings
        # ── P0：懒构建 LangGraph 图 ────────────────────────
        self._graph = None  # CompiledGraph — 首次调用时构建
        # 保留旧路径供 _stream_research 使用（SSE progress 事件）
        from app.research.market_detective import MarketDetective
        self.market_detective = MarketDetective(settings)

    def _ensure_graph(self):
        if self._graph is None:
            self._graph = build_graph(self.settings)
        return self._graph

    async def run(
        self, question: str, domain: MarketDomain | None = None,
        conversation_id: str | None = None,
    ) -> ResearchResponse:
        """运行研究调查。

        Args:
            question: 用户问题
            domain: 可选的强制市场域；None 表示让 Supervisor 自动判断
            conversation_id: 可选的对话会话 ID，用于注入历史上下文
        """
        question = question.strip()
        if not question:
            raise ValueError("question cannot be empty")

        # ── LangGraph 图调用 ────────────────────────────────
        graph = self._ensure_graph()
        state = ResearchState(
            question=question,
            domain=str(domain) if domain else None,
            conversation_id=conversation_id,
        )
        result_state = await graph.ainvoke(state)

        # ── 从 state 提取结果，转换为 ResearchResponse ────
        report = result_state.get("report")
        results = list(result_state.get("results", []))
        cache_stats = dict(result_state.get("cache_stats", {}))

        return ResearchResponse(
            question=question,
            report=report,
            tool_results=results,
            cache_stats=cache_stats,
            conversation_id=conversation_id,
        )
