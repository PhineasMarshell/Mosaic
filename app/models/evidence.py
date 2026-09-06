from typing import Literal, Any
from pydantic import BaseModel, Field


class Evidence(BaseModel):
    id: str
    source_tool: str
    domain: str = "unknown"
    metric: str
    value: Any = None
    timestamp: str | None = None
    source: str | None = None
    status: str = "success"
    partial: bool = False
    note: str | None = None


class Claim(BaseModel):
    claim: str
    type: Literal["observation", "interpretation", "hypothesis", "conclusion"]
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "low"
