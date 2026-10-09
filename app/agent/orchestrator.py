"""Orchestrator — LangGraph 图入口。

公开签名：question/domain/conversation_id → ResearchResponse。
"""

import logging
import time

from app.config import Settings
from app.graph import run_log
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
        run_id = run_log.new_run_id()
        state = ResearchState(
            question=question,
            domain=str(domain) if domain else None,
            conversation_id=conversation_id,
            run_id=run_id,
            # T23b：预算按"整次调查"起算——research_more 回环的第二轮 analyst
            # 读到的是同一个截止时刻，不会重新获得一整份 research_budget_seconds
            budget_deadline=time.monotonic() + self.settings.research_budget_seconds,
        )
        run_log.log_run_start(state)
        result_state = await graph.ainvoke(
            state,
            config={"recursion_limit": self.settings.graph_recursion_limit},
        )

        # ── 从 state 组装 ResearchResponse（与 SSE 路径共用）────
        result = build_response_from_state(result_state, question=question, conversation_id=conversation_id)

        # 可信与否只看 delivery_status；errors 只是"运行有没有出错"。
        if result.delivery_status != "verified":
            logger.warning(
                "Research delivery_status=%s final_audit_status=%s errors=%d critique=%s",
                result.delivery_status,
                result.final_audit_status,
                len(result.errors),
                (result.critique or {}).get("reason", "") if isinstance(result.critique, dict) else "",
            )

        return result
