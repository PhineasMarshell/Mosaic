"""Canonical tool arguments and stable semantic signatures.

The planner and analysts use registry keys, while Gateway clients use operation
ids.  This module is the single boundary for the quote argument contract and
for hashing nested argument structures.
"""

from __future__ import annotations

import re
from typing import Any


class ArgumentValidationError(ValueError):
    """Raised when a tool's canonical argument contract cannot be satisfied."""


_FREE_TEXT_ARGUMENTS = frozenset({"q", "query", "keyword", "text", "prompt", "description"})


def summarize_arguments(arguments: Any) -> dict[str, Any]:
    """Return a bounded, log-safe transport summary.

    Free text is always redacted.  Symbols expose only their transport shape
    and count so quote boundary debugging remains possible without logging
    complete user input or arbitrary argument payloads.
    """
    if not isinstance(arguments, dict):
        return {"type": type(arguments).__name__}
    summary: dict[str, Any] = {}
    for key, value in arguments.items():
        name = str(key)
        lower = name.casefold()
        if lower in _FREE_TEXT_ARGUMENTS:
            summary[name] = "<redacted>"
        elif lower in {"symbols", "symbol"}:
            if isinstance(value, str):
                summary[name] = {"type": "string", "count": len([x for x in value.split(",") if x.strip()])}
            elif isinstance(value, (list, tuple, set)):
                summary[name] = {"type": "array", "count": len(value)}
            else:
                summary[name] = {"type": type(value).__name__}
        elif isinstance(value, str):
            summary[name] = "<redacted>"
        elif isinstance(value, (int, float, bool)) or value is None:
            summary[name] = value
        elif isinstance(value, (list, tuple, set)):
            summary[name] = {"type": "array", "count": len(value)}
        elif isinstance(value, dict):
            summary[name] = {"type": "object", "keys": sorted(str(k) for k in value)[:12]}
        else:
            summary[name] = {"type": type(value).__name__}
    return summary


def redact_audit_arguments(arguments: Any) -> dict[str, Any]:
    """Keep validated targets/dates replayable, redact every free-text value."""
    summary = summarize_arguments(arguments)
    if not isinstance(arguments, dict):
        return summary
    for key, value in arguments.items():
        if key in {"symbol", "symbols"}:
            values = re.split(r"[,;]", value) if isinstance(value, str) else value
            if isinstance(values, (list, tuple)) and all(
                isinstance(item, str) and re.fullmatch(r"(?:[A-Z]{0,4}\d{5,6}|[A-Z]{1,6})", item)
                for item in values
            ):
                summary[key] = value
        elif key in {"date", "start_date", "end_date", "as_of_date"} and isinstance(value, str):
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{8}", value):
                summary[key] = value
    return summary


def _symbols(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        if not all(isinstance(part, str) for part in value):
            raise ArgumentValidationError("symbols must be a list[str]")
        values = [part.strip() for part in value]
    else:
        raise ArgumentValidationError("symbols must be a list[str]")
    values = [item for item in values if item]
    if not values:
        raise ArgumentValidationError("symbols must contain at least one non-empty symbol")
    # Quote targets are a set semantically; sorting makes planner/runtime/cache
    # signatures independent of the order supplied by an LLM.
    return sorted(dict.fromkeys(values), key=str.casefold)


def canonicalize_tool_arguments(tool_name: str, arguments: Any) -> dict[str, Any]:
    """Return a canonical copy of arguments for a Gateway operation.

    ``symbol`` remains accepted only as a compatibility input for
    ``get_market_quotes``.  It never leaves this function.
    """
    if not isinstance(arguments, dict):
        raise ArgumentValidationError("arguments must be an object")
    result = dict(arguments)
    if tool_name == "get_market_quotes":
        if "symbols" in result:
            values = _symbols(result["symbols"])
            if "symbol" in result and result["symbol"]:
                legacy = _symbols(result["symbol"])
                if values != legacy:
                    raise ArgumentValidationError("symbol and symbols specify different quote targets")
            result["symbols"] = values
            result.pop("symbol", None)
        elif "symbol" in result:
            result["symbols"] = _symbols(result.pop("symbol"))
        else:
            raise ArgumentValidationError("missing required argument: symbols")
    return result


def quote_transport_symbols(symbols: list[str]) -> list[str]:
    """Tencent requires an exchange prefix for known index codes.

    Preserve canonical arguments and code-only equities; 000300 otherwise
    becomes an empty quote rather than the requested CSI 300 index.
    """
    from app.gateway.stock_codes import INDEX_CODE_MAP_A_SHARE, _infer_index_exchange

    indices = set(INDEX_CODE_MAP_A_SHARE.values())
    return [f"{_infer_index_exchange(symbol)}{symbol}" if symbol in indices else symbol for symbol in symbols]


def freeze_argument(value: Any) -> Any:
    """Recursively convert nested values into deterministic hashable values."""
    if isinstance(value, dict):
        return tuple(sorted((str(key), freeze_argument(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(freeze_argument(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted((freeze_argument(item) for item in value), key=repr))
    if isinstance(value, (str, int, float, bool, type(None), bytes)):
        return value
    return repr(value)


def freeze_arguments(arguments: dict[str, Any]) -> tuple:
    return freeze_argument(arguments)


def semantic_signature(tool: str, arguments: dict[str, Any]) -> str:
    try:
        arguments = canonicalize_tool_arguments(tool, arguments)
    except Exception:
        pass
    return f"{tool}:{freeze_arguments(arguments)!r}"
