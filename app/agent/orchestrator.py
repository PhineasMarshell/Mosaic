"""Orchestrator — LangGraph 图入口。

公开签名：question/domain/conversation_id → ResearchResponse。
"""

import logging

from app.config import Settings
from app.graph.builder import build_graph
from app.graph.state import ResearchState
from app.models.research import MarketDomain
from app.models.response import ResearchResponse, build_response_from_state

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, settings: Settings):
        self.settings = settings
        # ── 懒构建 LangGraph 图 ────────────────────────
        self._graph = None  # CompiledGraph — 首次调用时构建

    def _ensure_graph(self):
        if self._graph is None:
            self._graph = build_graph(self.settings)
        return self._graph

    async def run(
        self,
        question: str,
        domain: MarketDomain | None = None,
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
        result_state = await graph.ainvoke(
            state,
            config={"recursion_limit": self.settings.graph_recursion_limit},
        )

        # ── 从 state 组装 ResearchResponse（与 SSE 路径共用）────
        result = build_response_from_state(result_state, question=question, conversation_id=conversation_id)

        verdict = result.critique.get("verdict") if isinstance(result.critique, dict) else None
        if result.errors or verdict not in (None, "pass"):
            logger.warning("Research completed with audit verdict=%s errors=%d", verdict, len(result.errors))

        return result
