"""受控数据源路由与 fallback。

Fallback 只在 primary 明确 timeout/error 时考虑，并要求备用结果具有可
审计 provenance。它不会覆盖权威冲突，也不会把 Skill 成功包装成 MCP
修复；调用方始终能看到 primary/fallback/source_switch/run_id。
"""

from __future__ import annotations

import re
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.models.market import ToolResult

_RETRYABLE_ERRORS = {
    "gateway_timeout",
    "gateway_upstream_error",
    "gateway_upstream_incomplete",
    "gateway_transport_error",
    "gateway_tls_error",
    "gateway_tool_unavailable",
    "deadline_exhausted",
    "empty_normalized",
    "empty_gateway_response",
    "unsupported",
    "skill_unsupported",
    "gateway_http_error",
    "gateway_invalid_response",
}
_MARKET_CAPABILITIES = {"quote", "kline", "market_breadth", "limit_up", "sector_theme", "abnormal_reasons"}


def _code(value: Any) -> str:
    text = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    match = re.search(r"\d{6}", text)
    return match.group() if match else text


def _requested_instruments(arguments: dict[str, Any]) -> set[str]:
    raw = arguments.get("symbol") or arguments.get("symbols") or []
    values = raw if isinstance(raw, (list, tuple, set)) else [raw]
    return {_code(value) for value in values if _code(value)}


def _requested_date(arguments: dict[str, Any]) -> str | None:
    raw = arguments.get("as_of_date") or arguments.get("date") or arguments.get("end") or arguments.get("end_date")
    text = str(raw or "")[:10].replace("/", "-")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


@dataclass
class RouteObservation:
    run_id: str
    source: str
    transport: str
    capability: str
    result: ToolResult
    latency_ms: float
    error_type: str | None = None
    tls_or_upstream: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result = self.result
        return {
            "run_id": self.run_id,
            "source": self.source,
            "transport": self.transport,
            "capability": self.capability,
            "status": result.status,
            "datum_count": len(result.normalized),
            "evidence_created": bool(result.normalized) and result.status != "error",
            "latency_ms": self.latency_ms,
            "error_type": self.error_type,
            "tls_or_upstream": self.tls_or_upstream,
            "reason_codes": result.reason_codes,
            "endpoint": result.endpoint,
            "skill_id": result.skill_id,
        }


@dataclass
class CapabilityMetrics:
    capability: str
    source: str
    observations: list[RouteObservation] = field(default_factory=list)

    def add(self, observation: RouteObservation) -> None:
        self.observations.append(observation)

    def summary(self) -> dict[str, Any]:
        rows = self.observations
        latencies = sorted(row.latency_ms for row in rows)
        successes = sum(row.result.status == "success" and bool(row.result.normalized) for row in rows)
        errors = sum(row.result.status == "error" for row in rows)
        partial = sum(row.result.status == "partial" or row.result.partial for row in rows)
        dated = sum(bool(row.result.as_of_date or any(d.as_of_date for d in row.result.normalized)) for row in rows)
        instruments = sum(any(d.instrument for d in row.result.normalized) for row in rows)
        evidence = sum(bool(row.result.normalized) and row.result.status != "error" for row in rows)
        return {
            "capability": self.capability,
            "source": self.source,
            "calls": len(rows),
            "success_rate": successes / len(rows) if rows else 0.0,
            "error_rate": errors / len(rows) if rows else 0.0,
            "timeout_error_rate": sum("gateway_timeout" in row.result.reason_codes for row in rows) / len(rows) if rows else 0.0,
            "p50_latency_ms": _percentile(latencies, 0.50),
            "p95_latency_ms": _percentile(latencies, 0.95),
            "date_completeness": dated / len(rows) if rows else 0.0,
            "instrument_match_rate": instruments / len(rows) if rows else 0.0,
            "partial_rate": partial / len(rows) if rows else 0.0,
            "evidence_availability": evidence / len(rows) if rows else 0.0,
        }


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 1)
    return round(statistics.quantiles(values, n=100, method="inclusive")[max(0, int(fraction * 100) - 1)], 1)


def _has_provenance(result: ToolResult, capability: str, arguments: dict[str, Any]) -> bool:
    if result.status == "error" or not result.normalized:
        return False
    expected_codes = _requested_instruments(arguments)
    expected_date = _requested_date(arguments) if capability in _MARKET_CAPABILITIES else None
    for datum in result.normalized:
        if not datum.source or not datum.authority or not (datum.retrieved_at or result.retrieved_at):
            return False
        observed_date = datum.as_of_date or (datum.timestamp or "")[:10] or result.as_of_date
        if capability in _MARKET_CAPABILITIES and not observed_date:
            return False
        if expected_date and observed_date != expected_date:
            return False
        if expected_codes and (not datum.instrument or _code(datum.instrument) not in expected_codes):
            return False
        coverage = str(getattr(datum, "coverage", "") or "")
        scope = str(getattr(datum, "instrument_scope", "") or "")
        if coverage == "unknown" and scope == "unknown" and not expected_codes:
            return False
    return True


def fallback_allowed(primary: ToolResult, fallback: ToolResult, capability: str, arguments: dict[str, Any]) -> bool:
    """Return true only for an explicit primary transport failure + provenance."""
    if primary.status != "error":
        return False
    if "conflicting_evidence" in primary.reason_codes or "authority_conflict" in primary.reason_codes:
        return False
    if not set(primary.reason_codes).intersection(_RETRYABLE_ERRORS):
        return False
    return _has_provenance(fallback, capability, arguments)


class ControlledFallbackRouter:
    """Route one capability while retaining both sides of the decision."""

    def __init__(self):
        self.observations: list[RouteObservation] = []

    async def call(
        self,
        *,
        run_id: str,
        capability: str,
        arguments: dict[str, Any],
        primary_source: str,
        primary_transport: str,
        primary: Callable[[], Awaitable[ToolResult]],
        fallback_source: str | None = None,
        fallback_transport: str = "http_api",
        fallback: Callable[[], Awaitable[ToolResult]] | None = None,
    ) -> ToolResult:
        started = time.monotonic()
        primary_result = await primary()
        self.observations.append(RouteObservation(
            run_id, primary_source, primary_transport, capability, primary_result,
            round((time.monotonic() - started) * 1000, 1),
            error_type=primary_result.reason_codes[0] if primary_result.reason_codes else None,
            tls_or_upstream="tls" if any("tls" in code for code in primary_result.reason_codes) else None,
        ))
        if not fallback or not fallback_source:
            return _annotate(primary_result, run_id=run_id, primary_source=primary_source)

        # Authentication failures are configuration/identity problems.  A
        # blind MCP retry would hide the actionable THS diagnosis and could
        # produce an unrelated datum, so this policy deliberately keeps the
        # result zero-datum and records why fallback was skipped.
        if "gateway_auth_error" in primary_result.reason_codes:
            primary_result.reason_codes = list(dict.fromkeys([
                *primary_result.reason_codes,
                "mcp_fallback_not_attempted_auth",
            ]))
            return _annotate(
                primary_result,
                run_id=run_id,
                primary_source=primary_source,
                fallback_reason="authentication_failure_no_fallback",
                source_switch=False,
            )

        # Do not probe/consume the fallback on a healthy primary path.
        if primary_result.status != "error" or not set(primary_result.reason_codes).intersection(_RETRYABLE_ERRORS):
            return _annotate(primary_result, run_id=run_id, primary_source=primary_source)
        try:
            fallback_result = await fallback()
        except Exception as exc:  # fallback errors stay zero-datum and auditable
            fallback_result = ToolResult(
                tool=capability,
                arguments=arguments,
                status="error",
                normalized=[],
                error=f"fallback transport failed: {type(exc).__name__}",
                reason_codes=["fallback_transport_error"],
                transport_exception=True,
            )
        self.observations.append(RouteObservation(
            run_id, fallback_source, fallback_transport, capability, fallback_result,
            round((time.monotonic() - started) * 1000, 1),
            error_type=fallback_result.reason_codes[0] if fallback_result.reason_codes else None,
            tls_or_upstream="upstream" if any("upstream" in code for code in fallback_result.reason_codes) else None,
        ))
        if not fallback_allowed(primary_result, fallback_result, capability, arguments):
            if fallback_result.status == "error":
                # Preserve the fallback transport diagnosis (TLS/upstream/
                # deadline) while keeping the zero-datum invariant.  The
                # primary failure is still visible in fallback_reason.
                fallback_result.attempts = [
                    *fallback_result.attempts,
                    {"stage": "fallback", "endpoint": fallback_result.endpoint,
                     "attempt": 1, "elapsed_ms": fallback_result.duration_ms,
                     "reason_codes": fallback_result.reason_codes},
                ]
                return _annotate(
                    fallback_result,
                    run_id=run_id,
                    primary_source=primary_source,
                    fallback_source=fallback_source,
                    fallback_reason=",".join(primary_result.reason_codes) or "primary_error",
                    source_switch=True,
                )
            # Preserve primary failure; a fallback that fails provenance cannot
            # hide it or create evidence.
            return _annotate(primary_result, run_id=run_id, primary_source=primary_source,
                             fallback_source=fallback_source, fallback_reason="fallback_provenance_insufficient",
                             source_switch=True)
        return _annotate(
            fallback_result,
            run_id=run_id,
            primary_source=primary_source,
            fallback_source=fallback_source,
            fallback_reason=",".join(primary_result.reason_codes) or "primary_error",
            source_switch=True,
        )


def _annotate(result: ToolResult, **fields: Any) -> ToolResult:
    for key, value in fields.items():
        setattr(result, key, value)
    return result
