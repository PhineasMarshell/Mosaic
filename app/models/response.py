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
    what_changed: list[str] = Field(default_factory=list)   # 与之前相比的变化 — PRD §10
    what_matters: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)          # 反证与风险 — PRD §23
    data_caveats: list[str] = Field(default_factory=list)
    anomalies: list[dict[str, Any]] = Field(default_factory=list)  # Anomaly Radar — PRD §29-30
    confidence: Literal["high", "medium", "low"] = "low"
    used_tools: list[str] = Field(default_factory=list)


class ResearchResponse(BaseModel):
    question: str
    report: MarketIntelligence
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    cache_stats: dict[str, int] = Field(default_factory=dict)
