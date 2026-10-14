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
    as_of_date: str | None = None
    authority: str | None = None
    retrieved_at: str | None = None
    completeness: str = "unknown"
    instrument_scope: str = "unknown"
    coverage: str = "unknown"
    instrument_name: str | None = None
    source: str | None = None
    tool: str
    status: Status = "success"
    partial: bool = False
    note: str | None = None


class ToolResult(BaseModel):
    tool: str
    #: Gateway operation id (normally equal to ``tool``; explicit for audit
    #: records and boundary validation failures).
    operation_id: str | None = None
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
    #: Machine-readable transport/audit reasons.  Error results never become
    #: evidence; callers can still distinguish timeout, deadline exhaustion,
    #: and optional-source gaps without parsing free text.
    reason_codes: list[str] = Field(default_factory=list)
    retrieved_at: str | None = None
    as_of_date: str | None = None
    authority: str | None = None
    completeness: str = "unknown"
    coverage: str = "unknown"
    #: 数据不完整的原因（上游自己的 note，或本地截断说明）。
    #: gateway 的契约是 partial=true 时"另有 note 说明"，以前这个字段被
    #: _METADATA_KEYS 直接丢掉了，用户和 LLM 都看不到数据为什么不全。
    note: str | None = None
    normalized: list[NormalizedDatum] = Field(default_factory=list)
    #: 本地为提示词截断展示数据时保留上游覆盖度；None 表示未截断。
    source_datum_count: int | None = None
    source_partial: bool | None = None
    #: 阶段 6：本次执行的墙钟耗时（毫秒）。``None`` = 没有计时点（旧构造点 /
    #: 反序列化的历史结果），与"耗时 0ms"不同 —— 指标聚合要能区分两者。
    duration_ms: float | None = None
    #: 运行级可观测性：命中共享缓存与真实上游响应必须可区分；refresh 请求
    #: 永远把该字段置为 False，即使返回内容与旧缓存相同。
    cache_hit: bool = False
    refresh_requested: bool = False
    transport_exception: bool = False
    upstream_status: str | None = None
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    # P6：统一外部 Skill / Gateway 审计字段。 这些字段只描述本次调用的
    # 链路，不改变 Evidence Gate 的日期、标的、authority 或 partial 判定。
    run_id: str | None = None
    transport: str | None = None
    endpoint: str | None = None
    skill_id: str | None = None
    skill_version: str | None = None
    source_tool: str | None = None
    primary_source: str | None = None
    fallback_source: str | None = None
    fallback_reason: str | None = None
    source_switch: bool = False
    mcp_initialize: bool | None = None
    mcp_list_tools: bool | None = None
    mcp_real_call: bool | None = None

    @property
    def datum_count(self) -> int:
        return len(self.normalized)
