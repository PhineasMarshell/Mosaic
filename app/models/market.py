from typing import Any, Literal

from pydantic import BaseModel, Field

Status = Literal["success", "partial", "error"]

# 字符串常量 — graph/tool_runtime 等模块用这些做 status 比较
STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_ERROR = "error"


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
    #: 数据不完整的原因（上游自己的 note，或本地截断说明）。
    #: gateway 的契约是 partial=true 时"另有 note 说明"，以前这个字段被
    #: _METADATA_KEYS 直接丢掉了，用户和 LLM 都看不到数据为什么不全。
    note: str | None = None
    normalized: list[NormalizedDatum] = Field(default_factory=list)
