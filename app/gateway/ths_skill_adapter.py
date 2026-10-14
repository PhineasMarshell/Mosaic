"""统一同花顺 Skill Adapter。

适配器把已注册官方 Skill 的 transport 结果收敛成 ``ToolResult``，并负责
统一审计字段和失败不变式。具体字段解析仍由各 Skill 的官方契约模块完成；
这里不把自然语言文本猜成行情、标的或因果证据。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from app.gateway.iwencai_contracts import QUERY_TOOLS
from app.gateway.ths_skill_registry import THS_SKILL_REGISTRY, SkillSpec, get_skill_spec
from app.models.market import ToolResult


def _error(spec: SkillSpec, arguments: dict[str, Any], message: str, reason: str, *, run_id: str | None = None) -> ToolResult:
    return ToolResult(
        tool=spec.operation_id,
        operation_id=spec.operation_id,
        tool_key=spec.logical_key,
        arguments=arguments,
        status="error",
        normalized=[],
        raw=None,
        partial=False,
        error=message,
        reason_codes=[reason],
        run_id=run_id,
        transport=spec.transport,
        endpoint=spec.endpoint,
        skill_id=spec.skill_id,
        skill_version=spec.version,
        source_tool=spec.operation_id,
    )


def _provenance_reasons(result: ToolResult) -> list[str]:
    reasons: list[str] = []
    for datum in result.normalized:
        if not datum.source and not datum.authority:
            reasons.append("missing_source_provenance")
        if not datum.retrieved_at and not result.retrieved_at:
            reasons.append("missing_retrieved_at")
        # Do not invent dates/instruments.  Their absence is retained as a
        # quality gap so claim matching can block date/identity assertions.
        if not datum.as_of_date and not datum.timestamp:
            reasons.extend(("missing_as_of_date", "unknown_date"))
        if datum.instrument_scope in {"instrument", "index", "unverified_instrument"} and not datum.instrument:
            reasons.append("missing_instrument")
    return list(dict.fromkeys(reasons))


class THSSkillAdapter:
    """Dispatch registered Skills through one auditable boundary."""

    def __init__(self, settings):
        self.settings = settings

    async def call(
        self,
        operation_id: str,
        arguments: dict[str, Any],
        *,
        deadline: float | None = None,
        refresh: bool = False,
        run_id: str | None = None,
    ) -> ToolResult:
        spec = get_skill_spec(operation_id)
        started = time.monotonic()
        try:
            # Imports stay local to avoid a registry ↔ adapter ↔ research cycle.
            if operation_id in QUERY_TOOLS:
                from app.research.iwencai_query import query_data

                result = await query_data(self.settings, operation_id, arguments, deadline=deadline, refresh=refresh)
            elif operation_id == "internal_iwencai_announcement_search":
                from app.research.iwencai import search_announcements

                result = await search_announcements(self.settings, arguments, deadline=deadline)
            else:
                from app.research.iwencai_search import search_content

                result = await search_content(self.settings, operation_id, arguments, deadline=deadline)
        except TimeoutError:
            result = _error(spec, arguments, "同花顺 Skill 请求超时", "gateway_timeout", run_id=run_id)
        except ValueError as exc:
            # Validation errors are stable and contain no raw query/secret.
            result = _error(spec, arguments, f"同花顺 Skill 参数无效: {type(exc).__name__}", "invalid_arguments", run_id=run_id)
        except Exception as exc:  # transport/auth/upstream failures stay zero-datum errors
            result = _error(spec, arguments, f"同花顺 Skill transport failed: {type(exc).__name__}", "gateway_transport_error", run_id=run_id)

        result.run_id = run_id
        result.transport = spec.transport
        result.primary_source = result.primary_source or "ths_skill"
        result.endpoint = spec.endpoint
        result.skill_id = spec.skill_id
        result.skill_version = spec.version
        result.source_tool = spec.operation_id
        result.refresh_requested = bool(refresh)
        result.cache_hit = False
        result.operation_id = result.operation_id or spec.operation_id
        result.tool_key = result.tool_key or spec.logical_key
        result.duration_ms = result.duration_ms if result.duration_ms is not None else round((time.monotonic() - started) * 1000, 1)
        result.retrieved_at = result.retrieved_at or datetime.now(UTC).isoformat()

        if result.status == "error" or not result.normalized:
            # A Skill envelope with no usable datum is never evidence.
            result.status = "error"
            result.partial = False
            result.normalized = []
            result.raw = None if result.status == "error" else result.raw
            if not result.reason_codes:
                result.reason_codes = ["empty_normalized"]
            return result

        result.authority = result.authority or spec.authority
        reasons = _provenance_reasons(result)
        if reasons:
            result.reason_codes = list(dict.fromkeys([*result.reason_codes, *reasons]))
            result.partial = True
            if result.status == "success":
                result.status = "partial"
            result.note = "; ".join(filter(None, [result.note, "Skill provenance fields incomplete: " + ",".join(reasons)]))
        # Every datum keeps the provider chain and operation id.  Missing
        # values remain missing; no value is inferred from query text.
        for datum in result.normalized:
            datum.tool = datum.tool or spec.operation_id
            datum.source = datum.source or None
            datum.authority = datum.authority or result.authority
            datum.retrieved_at = datum.retrieved_at or result.retrieved_at
            if datum.coverage == "unknown":
                datum.coverage = datum.instrument_scope if datum.instrument_scope != "unknown" else (
                    "instrument" if datum.instrument else "market"
                )
        if result.coverage == "unknown":
            scopes = {datum.coverage for datum in result.normalized if datum.coverage != "unknown"}
            result.coverage = next(iter(scopes)) if len(scopes) == 1 else ("mixed" if scopes else "unknown")
        return result


def skill_adapter_for(settings) -> THSSkillAdapter:
    return THSSkillAdapter(settings)


def is_registered_skill(operation_id: str) -> bool:
    return operation_id in THS_SKILL_REGISTRY
