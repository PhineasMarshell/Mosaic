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
    #: 阶段 2：本次调用**在计划里的 registry key**（analyst 从 route 写入）。
    #: 缺口判定必须读它——``tool`` 存的是 gateway operationId，而同一个
    #: operationId 会被多个 registry key 复用（quote/search、klines/snapshot/window），
    #: 用 operationId 反查只会命中规范条目，把兄弟 key 的缺口误判成"已满足"。
    #: 老结果/其他构造点可能为 None，此时覆盖度判定一律按"不满足"处理（保守）。
    tool_key: str | None = None
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
    #: 阶段 6：本次执行的墙钟耗时（毫秒）。``None`` = 没有计时点（旧构造点 /
    #: 反序列化的历史结果），与"耗时 0ms"不同 —— 指标聚合要能区分两者。
    duration_ms: float | None = None
