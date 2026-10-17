"""Canonical tool arguments and stable semantic signatures.

The planner and analysts use registry keys, while Gateway clients use operation
ids.  This module is the single boundary for the quote argument contract and
for hashing nested argument structures.
"""

from __future__ import annotations

import re
from typing import Any

from app.gateway.iwencai_contracts import QUERY_TOOLS, SEARCH_TOOLS


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
    if tool_name in QUERY_TOOLS:
        if set(result) - {"query", "page", "limit", "is_cache", "expand_index"}:
            raise ArgumentValidationError("Iwencai query accepts only query, page, limit, is_cache and expand_index")
        query = result.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ArgumentValidationError("query must be a non-empty string of at most 2000 characters")
        canonical = {"query": query.strip()}
        for key, default, high in (("page", 1, 10000), ("limit", 10, 20)):
            value = result.get(key, default)
            if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
                value = int(value)
            if type(value) is not int or not 1 <= value <= high:
                raise ArgumentValidationError(f"{key} must be a positive integer from 1 to {high}")
            canonical[key] = value
        for key, default in (("is_cache", True), ("expand_index", True)):
            value = result.get(key, default)
            if isinstance(value, str) and value.lower() in {"0", "1", "true", "false"}:
                value = value.lower() in {"1", "true"}
            if type(value) is not bool:
                raise ArgumentValidationError(f"{key} must be a boolean")
            canonical[key] = value
        result = canonical
    if tool_name in SEARCH_TOOLS:
        unexpected = set(result) - {"query", "size"}
        if unexpected:
            raise ArgumentValidationError("Iwencai search accepts only query and size")
        query = result.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ArgumentValidationError("query must be a non-empty string of at most 2000 characters")
        size = result.get("size", 10)
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 20:
            raise ArgumentValidationError("size must be an integer from 1 to 20")
        result = {"query": query.strip(), "size": size}
    if tool_name == "get_market_quotes":
        if "symbols" in result:
            values = _symbols(result["symbols"])
            if "symbol" in result and result["symbol"]:
                legacy = _symbols(result["symbol"])
                if values != legacy:
                    raise ArgumentValidationError("symbol and symbols specify different quote targets")
            result["symbols"] = [_canonical_quote_symbol(symbol) for symbol in values]
            result.pop("symbol", None)
        elif "symbol" in result:
            result["symbols"] = [_canonical_quote_symbol(symbol) for symbol in _symbols(result.pop("symbol"))]
        else:
            raise ArgumentValidationError("missing required argument: symbols")
    return result


def _canonical_quote_symbol(symbol: str) -> str:
    """Return the canonical Quote target: an uppercase exchange-prefixed code.

    Bare six-digit codes stay untouched for real equities (``600519``), but a
    bare A-share **index** code is only ever one thing (``000300`` is the CSI
    300, never a stock), so it is promoted to its exchange form here.  Fixing
    that once at canonicalization time keeps planner, critic, route, cache and
    transport encoding on one variant instead of two.
    """
    from app.gateway.stock_codes import INDEX_CODE_MAP_A_SHARE, _infer_index_exchange

    text = symbol.strip()
    if not text:
        return symbol
    upper = text.upper()
    if re.fullmatch(r"(?:SH|SZ|BJ)\d{6}", upper):
        return upper
    if re.fullmatch(r"\d{6}", upper) and upper in set(INDEX_CODE_MAP_A_SHARE.values()):
        return f"{_infer_index_exchange(upper).upper()}{upper}"
    return text


def quote_transport_symbols(symbols: list[str]) -> list[str]:
    """Tencent requires an exchange prefix for known index codes.

    Canonical arguments already carry the prefix; a bare index code arriving
    from a legacy caller is promoted here too so it cannot silently become an
    empty quote.
    """
    encoded = [_canonical_quote_symbol(symbol) for symbol in symbols]
    # Tencent wire format is lowercase. Canonical plan/cache/audit targets stay
    # uppercase; a bare 000001 remains ambiguous and must never become SH000001.
    return [symbol.lower() if re.fullmatch(r"(?:SH|SZ|BJ)\d{6}", symbol) else symbol for symbol in encoded]


def gateway_transport_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Remove research-only parameters from current-only Gateway endpoints.

    Research plans and THS queries retain historical targets. This encoding is
    deliberately separate from canonical arguments used for cache and audit.
    """
    if tool_name in {"list_limit_up_stocks", "get_limit_up_count", "list_limit_up_sectors", "get_ashare_sentiment", "get_telegraph"}:
        return {}
    return dict(arguments)



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
