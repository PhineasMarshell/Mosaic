"""EvidenceGate 节点 -- 代码级证据质量硬检查。

包装 evidence_gate.run_evidence_gate() 为 LangGraph 节点。
不依赖 LLM，直接根据 ToolResult 的状态判断证据基本可用性。
如果失败写进 errors，不炸图。
"""

from app.agent.evidence_gate import EvidenceGateResult, run_evidence_gate
from app.config import Settings
from app.models.market import ToolResult


class GateNode:
    """证据门控节点：ToolResult -> EvidenceGateResult。"""

    def __init__(self, settings: Settings):
        self.settings = settings

    async def __call__(self, state):
        """运行证据门控，返回要写入 state 的字段字典。"""
        try:
            # Normalize state to dict (handle both ResearchState and dict)
            if hasattr(state, "model_dump"):
                state = state.model_dump(exclude_none=False)
            results = state.get("results", [])
            # Convert plain dicts back to ToolResult if needed
            tool_results: list[ToolResult] = [ToolResult(**r) if isinstance(r, dict) else r for r in results]
            gate: EvidenceGateResult = run_evidence_gate(tool_results)
            return {
                "gate": gate,
            }
        except Exception as exc:
            # 门控失败降级：继续流程（report 会标注数据不足）
            return {"errors": [f"Gate failed: {exc}"]}
