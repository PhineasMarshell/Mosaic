"""Canonical tool arguments and stable semantic signatures.

The planner and analysts use registry keys, while Gateway clients use operation
ids.  This module is the single boundary for the quote argument contract and
for hashing nested argument structures.
"""

from __future__ import annotations

from typing import Any


class ArgumentValidationError(ValueError):
    """Raised when a tool's canonical argument contract cannot be satisfied."""


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
