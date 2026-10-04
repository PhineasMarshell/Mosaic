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
    #: reasoning 环节失败时为空。此前该字段是必填的，导致"没产出报告"这种最需要
    #: 被解释的失败反而在组装响应时就抛 ValidationError，调用方只能拿到 500 和
    #: 一段 pydantic 校验文本，state.errors 里真正的原因永远传不出去。
    #: 现在允许为空，调用方必须检查它（见 errors）并给出明确的失败提示，
    #: 不要把空报告当成正常结果渲染。
    report: MarketIntelligence | None = None
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

    report = state.get("report")
    errors = list(state.get("errors") or [])
    if report is None and not errors:
        # 保证 report 为空时调用方手里一定有一条可展示的原因，而不是只有空报告。
        errors.append("reasoning produced no report")

    return ResearchResponse(
        question=question,
        report=report,
        tool_results=tool_results,
        cache_stats=dict(state.get("cache_stats") or {}),
        conversation_id=conversation_id,
        critique=critique,
        errors=errors,
    )
