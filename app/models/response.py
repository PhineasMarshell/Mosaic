from typing import Any, Literal

from pydantic import BaseModel, Field


class EvidenceItem(BaseModel):
    """单条证据。"""

    id: str
    source_tool: str = ""
    domain: str = "unknown"
    metric: str = ""
    value: Any = None
    timestamp: str | None = None
    source: str | None = None
    status: str = "success"
    partial: bool = False
    note: str | None = None


class MarketIntelligence(BaseModel):
    title: str = "今日市场情报"
    market_state: str
    state_label: str = "Unknown"
    what_happened: str
    why: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    strong_areas: list[str] = Field(default_factory=list)  # What's Moving — PRD §26
    what_changed: list[str] = Field(default_factory=list)  # 与之前相比的变化 — PRD §10
    what_matters: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)  # 反证与风险 — PRD §23
    data_caveats: list[str] = Field(default_factory=list)
    anomalies: list[dict[str, Any]] = Field(default_factory=list)  # Anomaly Radar — PRD §29-30
    confidence: Literal["high", "medium", "low"] = "low"
    used_tools: list[str] = Field(default_factory=list)


class ResearchResponse(BaseModel):
    question: str
    report: MarketIntelligence
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    cache_stats: dict[str, int] = Field(default_factory=dict)
    conversation_id: str | None = None
    critique: dict[str, Any] | None = None
    errors: list[str] = Field(default_factory=list)


def build_response_from_state(
    state: dict[str, Any],
    question: str,
    conversation_id: str | None = None,
) -> ResearchResponse:
    """从 LangGraph 终态组装 ResearchResponse。

    同步路径（Orchestrator.run）与 SSE 路径（/api/ask/stream）共用，
    避免 tool_results / critique 的归一化逻辑在两边漂移。

    注意：values 流模式与 ainvoke 的终态里，results 仍是 ToolResult 实例，
    而 ResearchResponse.tool_results 是 list[dict]，这里统一 dump。
    """
    raw_results = state.get("results") or []
    tool_results = [r if isinstance(r, dict) else r.model_dump() for r in raw_results]

    critique = state.get("critique")
    if critique is not None and not isinstance(critique, dict) and hasattr(critique, "model_dump"):
        critique = critique.model_dump()

    return ResearchResponse(
        question=question,
        report=state.get("report"),
        tool_results=tool_results,
        cache_stats=dict(state.get("cache_stats") or {}),
        conversation_id=conversation_id,
        critique=critique,
        errors=list(state.get("errors") or []),
    )
