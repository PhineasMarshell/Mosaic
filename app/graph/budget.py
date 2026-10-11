"""Shared stage-budget helpers for the research graph.

The graph has one absolute deadline.  These helpers reserve downstream time
before starting an LLM call, so a slow plan or tool phase cannot consume the
time needed to emit a Critic/finalize terminal state.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from typing import Any, TypeVar

T = TypeVar("T")


class StageBudgetExceeded(TimeoutError):
    """A stage could not start without violating the absolute deadline."""

    def __init__(self, stage: str, remaining: float | None, reserve: float):
        self.stage = stage
        self.remaining = remaining
        self.reserve = reserve
        super().__init__(f"{stage} budget unavailable")


def remaining_budget(state: Any) -> float | None:
    deadline = state.get("budget_deadline") if isinstance(state, dict) else getattr(state, "budget_deadline", None)
    if deadline is None:
        return None
    try:
        return float(deadline) - time.monotonic()
    except (TypeError, ValueError):
        return None


def stage_timeout(state: Any, settings: Any, *, stage: str, reserve: float = 0.0) -> float:
    """Return the bounded timeout for one stage, or raise before starting it."""
    remaining = remaining_budget(state)
    configured = max(0.1, float(getattr(settings, "llm_timeout_seconds", 90) or 90))
    if remaining is None:
        return configured
    available = remaining - max(0.0, float(reserve))
    if available <= 0:
        raise StageBudgetExceeded(stage, remaining, reserve)
    return min(configured, available)


async def run_stage(awaitable: Awaitable[T], state: Any, settings: Any, *, stage: str, reserve: float = 0.0) -> T:
    """Run an awaitable under both the LLM cap and the absolute deadline."""
    try:
        timeout = stage_timeout(state, settings, stage=stage, reserve=reserve)
    except Exception:
        close = getattr(awaitable, "close", None)
        if close is not None:
            close()
        raise
    return await asyncio.wait_for(awaitable, timeout=timeout)


def elapsed_ms(state: Any) -> float | None:
    started = state.get("started_at") if isinstance(state, dict) else getattr(state, "started_at", None)
    if started is None:
        return None
    try:
        return round(max(0.0, time.monotonic() - float(started)) * 1000, 1)
    except (TypeError, ValueError):
        return None