from typing import Any, Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    id: str
    source_tool: str
    tool_key: str | None = None
    operation_id: str | None = None
    domain: str = "unknown"
    metric: str
    value: Any = None
    timestamp: str | None = None
    source: str | None = None
    status: str = "success"
    partial: bool = False
    note: str | None = None
    #: 阶段 4：这条证据针对的标的（工具调用参数里的 ``symbol``）。
    #: 报告里的公司名必须对得上某个 ``instrument``，否则只能是"未验证实体"。
    instrument: str | None = None


class Claim(BaseModel):
    claim: str
    type: Literal["observation", "interpretation", "hypothesis", "conclusion"]
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "low"
