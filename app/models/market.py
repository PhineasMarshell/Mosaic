from typing import Any, Literal
from pydantic import BaseModel, Field


Status = Literal["success", "partial", "error"]


class NormalizedDatum(BaseModel):
    domain: str = "unknown"
    instrument: str | None = None
    metric: str
    value: Any
    unit: str | None = None
    timestamp: str | None = None
    source: str | None = None
    tool: str
    status: Status = "success"
    partial: bool = False
    note: str | None = None


class ToolResult(BaseModel):
    tool: str
    arguments: dict[str, Any]
    raw: Any = None
    status: Status
    partial: bool = False
    error: str | None = None
    normalized: list[NormalizedDatum] = Field(default_factory=list)
