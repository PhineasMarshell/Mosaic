"""Finite transport retries and payload-free attempt diagnostics."""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime

from app.gateway.normalizer import normalize_tool_result


def response_diagnostics(text: str, http_status: int | None = None) -> dict:
    lower = text.casefold()
    status = re.search(r'\b(502|503|504|520|521|522|523|524|525|526)\b', text)
    return {
        "http_status": http_status or (int(status.group()) if status else None),
        "payload_chars": len(text),
        "cloudflare": "cloudflare" in lower,
        "origin_incomplete": "incomplete" in lower and ("origin" in lower or "cloudflare" in lower),
        "tls_timeout": "tls" in lower and "timeout" in lower,
    }


async def observed_call(settings, tool: str, arguments: dict, deadline: float | None, request: Callable):
    """request(timeout) returns (normalized result, safe diagnostics).

    Two attempts maximum for the optional abnormal source. Retry only known
    transient errors; never retry malformed args/auth or turn empty into data.
    Split the available time so the first attempt cannot eat the retry budget.
    """
    started = time.monotonic()
    attempts = []
    cap = max(1, min(6, int(settings.max_retry_per_tool) + 1))
    if tool == "get_stock_abnormal_reasons":
        cap = min(2, cap)
    if deadline is None:
        deadline = started + float(settings.research_timeout_seconds) * cap + .5 * (cap - 1)
    last = None
    for number in range(1, cap + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if last is None:
                last = normalize_tool_result(tool, arguments, None, error="budget exhausted before request")
            elif "deadline_exhausted" not in last.reason_codes:
                last.reason_codes.append("deadline_exhausted")
            break
        slots = cap - number + 1
        timeout = min(float(settings.research_timeout_seconds), max(.001, (remaining - .5 * (slots - 1)) / slots))
        attempt_started = time.monotonic()
        diagnostics = {}
        exception_type = None
        try:
            last, diagnostics = await asyncio.wait_for(request(timeout), timeout=timeout)
        except TimeoutError:
            exception_type = "TimeoutError"
            codes = ["gateway_timeout"]
            if getattr(request, "mcp_transport", False):
                codes.append("mcp_upstream_timeout")
            if deadline - time.monotonic() <= 0:
                codes.append("deadline_exhausted")
            last = normalize_tool_result(tool, arguments, None, error="Gateway request timed out",
                                         reason_codes=codes)
        except Exception as exc:
            exception_type = type(exc).__name__
            # Exception strings can echo credentials and query parameters.
            codes = list(getattr(exc, "reason_codes", []) or []) or ["gateway_transport_error"]
            if getattr(exc, "stage", None):
                diagnostics["stage"] = exc.stage
            if getattr(exc, "endpoint", None):
                diagnostics["endpoint"] = exc.endpoint
            diagnostics["attempt"] = getattr(exc, "attempt", number)
            last = normalize_tool_result(tool, arguments, None, error=f"Gateway transport error: {exception_type}",
                                         reason_codes=codes)
        codes = set(last.reason_codes)
        outcome = ("timeout" if "gateway_timeout" in codes else "empty" if codes & {"empty_gateway_response", "empty_normalized"}
                   else last.status)
        attempts.append({"attempt": number, "status": outcome, "reason_codes": last.reason_codes[:],
                         "elapsed_ms": round((time.monotonic()-attempt_started)*1000, 1),
                         "remaining_budget_ms": round(max(0, deadline-time.monotonic())*1000, 1),
                         "exception_type": exception_type, **diagnostics})
        if last.status != "error":
            break
        transient = codes & {"gateway_timeout", "gateway_upstream_error", "gateway_upstream_incomplete",
                             "gateway_rate_limited", "gateway_transport_error"}
        if not transient or "gateway_auth_error" in codes or number == cap:
            break
        backoff = 1.0 if "gateway_rate_limited" in codes else .5
        if deadline - time.monotonic() <= backoff + .01:
            last.error = "budget exhausted before retry"
            last.reason_codes = list(dict.fromkeys([*last.reason_codes, "deadline_exhausted"]))
            break
        await asyncio.sleep(backoff)
    assert last is not None
    last.attempts = attempts
    last.upstream_status = attempts[-1]["status"] if attempts else "deadline_exhausted"
    last.duration_ms = round((time.monotonic()-started)*1000, 1)
    last.retrieved_at = last.retrieved_at or datetime.now(UTC).isoformat()
    if last.status == "error" and deadline <= time.monotonic() and "deadline_exhausted" not in last.reason_codes:
        last.reason_codes.append("deadline_exhausted")
    if last.status == "error":
        last.normalized = []
        last.raw = None
        last.partial = False
    return last
