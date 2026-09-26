"""Kernel 节点 — 包装现有 MarketDetective.investigate()。

P0 阶段整图的唯一工作节点。逻辑完全不动，只是把原来的同步调用
包成 async 函数、接受/返回 ResearchState。
"""

from app.config import Settings
from app.graph.state import ResearchState
from app.research.market_detective import MarketDetective


class KernelNode:
    """执行研究调查的整图兜底节点。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._detective = MarketDetective(settings)

    async def __call__(self, state):
        """运行调查，返回要写入 state 的字段字典。"""
        try:
            # Normalize state to dict (handle both ResearchState and dict)
            if hasattr(state, "model_dump"):
                s = state.model_dump(exclude_none=False)
            else:
                s = state
            response = await self._detective.investigate(
                question=s.get("question", ""),
                domain=s.get("domain"),
                conversation_id=s.get("conversation_id"),
            )
            tool_results = list(response.tool_results) if response.tool_results else []
            return {
                "report": response.report,
                "tool_results": tool_results,
                "cache_stats": response.cache_stats,
                # GateNode 和 ReasoningNode 读 results 字段 — kernel 必须写
                "results": tool_results,
            }
        except Exception as exc:
            # 节点异常不炸图，写进 errors 让上层处理
            return {"errors": [str(exc)]}
