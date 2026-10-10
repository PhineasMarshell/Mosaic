"""A-share trading-day and request/as-of date semantics.

The calendar is deliberately deterministic and dependency-free.  It covers
weekends plus the project-maintained holiday set; callers may provide a newer
holiday set or an explicit list of available dates when an upstream calendar
is available.  Retrieval time is never used as an observation date.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

try:  # pragma: no cover - Windows and Linux both provide zoneinfo
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment,misc]


# Cross-checked against AkShare's Sina trading-day history through 2026-12-31:
# https://finance.sina.com.cn/realstock/company/klc_td_sh.txt
# Renew and verify against exchange notices before extending coverage;
# ordinary weekdays in later years are not presumed open.
CALENDAR_COVERED_YEARS: frozenset[int] = frozenset({2024, 2025, 2026})
KNOWN_A_SHARE_HOLIDAYS: frozenset[str] = frozenset(
    {
        # 2024
        "2024-01-01", "2024-02-09", "2024-02-12", "2024-02-13", "2024-02-14", "2024-02-15", "2024-02-16",
        "2024-04-04", "2024-04-05", "2024-05-01", "2024-05-02", "2024-05-03", "2024-06-10",
        "2024-09-16", "2024-09-17", "2024-10-01", "2024-10-02", "2024-10-03", "2024-10-04", "2024-10-07",
        # 2025
        "2025-01-01", "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31", "2025-02-03", "2025-02-04", "2025-04-04",
        "2025-05-01", "2025-05-02", "2025-05-05", "2025-05-31", "2025-06-02",
        "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-06", "2025-10-07", "2025-10-08",
        # 2026 (the project acceptance date 2026-10-10 is a Saturday)
        "2026-01-01", "2026-01-02", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23",
        "2026-04-06", "2026-05-01", "2026-05-04", "2026-05-05", "2026-06-19", "2026-09-25", "2026-10-01", "2026-10-02",
        "2026-10-03", "2026-10-05", "2026-10-06", "2026-10-07",
    }
)

_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-/]?(\d{1,2})[-/]?(\d{1,2})(?!\d)")
_CN_DATE_RE = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日?")
_TODAY_TERMS = ("今天", "今日", "today")
_RELATIVE_DAY_TERMS = ("当天", "当日")


def normalize_date(value: date | datetime | str | None) -> str | None:
    """Return ``YYYY-MM-DD`` or ``None`` for an unknown/invalid date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    match = _CN_DATE_RE.search(text) or _DATE_RE.search(text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return None


def requested_date_from_question(question: str, *, now: date | datetime | None = None) -> tuple[str | None, bool]:
    """Extract the user's requested date and whether the request means today."""
    text = str(question or "")
    explicit = _CN_DATE_RE.search(text) or _DATE_RE.search(text)
    mentions_today = any(term in text.casefold() for term in _TODAY_TERMS)
    # A comparison date does not replace the requested day in "today versus
    # 2026-10-08".  "That day" alongside a date still refers to that date.
    if explicit and not mentions_today:
        requested = normalize_date(explicit.group(0))
        return requested, False
    if mentions_today or any(term in text for term in _RELATIVE_DAY_TERMS):
        if now is None:
            if ZoneInfo is not None:
                now = datetime.now(ZoneInfo("Asia/Shanghai"))
            else:  # pragma: no cover
                now = datetime.now()
        return normalize_date(now), True
    return None, False


def _trading_day_status(value: date | datetime | str, *, holidays: Iterable[str] | None = None) -> bool | None:
    """Return a known trading-day status, or None beyond calendar coverage."""
    normalized = normalize_date(value)
    if normalized is None:
        return None
    parsed = date.fromisoformat(normalized)
    closure_set = set(KNOWN_A_SHARE_HOLIDAYS)
    closure_set.update(normalize_date(item) for item in (holidays or ()))
    if parsed.weekday() >= 5 or normalized in closure_set:
        return False
    if parsed.year not in CALENDAR_COVERED_YEARS:
        return None
    return True


def is_a_share_trading_day(value: date | datetime | str, *, holidays: Iterable[str] | None = None) -> bool:
    """Return True only for a confirmed A-share trading day."""
    return _trading_day_status(value, holidays=holidays) is True


def nearest_previous_trading_day(
    value: date | datetime | str,
    *,
    available_dates: Iterable[str] | None = None,
    holidays: Iterable[str] | None = None,
    lookback_days: int = 370,
) -> str | None:
    """Select the nearest known/available trading day on or before ``value``.

    ``available_dates`` is treated as an evidence availability boundary.  If
    supplied, an empty list means there is no available trading day; it does
    not silently fall back to a guessed calendar date.
    """
    normalized = normalize_date(value)
    if normalized is None:
        return None
    available = None if available_dates is None else {
        item for item in (normalize_date(value) for value in available_dates) if item
    }
    current = date.fromisoformat(normalized)
    if available is None and current.year not in CALENDAR_COVERED_YEARS:
        return None
    for _ in range(max(0, int(lookback_days)) + 1):
        candidate = current.isoformat()
        status = _trading_day_status(candidate, holidays=holidays)
        if (status is True or (status is None and available is not None and candidate in available)) and (
            available is None or candidate in available
        ):
            return candidate
        current -= timedelta(days=1)
    return None


@dataclass(frozen=True)
class TradingDateSemantics:
    requested_date: str | None
    as_of_date: str | None
    market_closed: bool | None
    is_today_request: bool = False
    reason: str = ""

    @property
    def data_gap(self) -> bool:
        return self.as_of_date is None

    def model_dump(self) -> dict[str, object]:
        return asdict(self) | {"data_gap": self.data_gap}


def resolve_trading_date_semantics(
    question: str,
    *,
    now: date | datetime | None = None,
    available_dates: Iterable[str] | None = None,
    holidays: Iterable[str] | None = None,
) -> TradingDateSemantics:
    """Resolve requested date, market closure, and a safe target as-of date."""
    requested, is_today = requested_date_from_question(question, now=now)
    if requested is None:
        return TradingDateSemantics(None, None, None, False, "requested_date_unknown")
    trading_status = _trading_day_status(requested, holidays=holidays)
    if trading_status is None:
        as_of = nearest_previous_trading_day(
            requested, available_dates=available_dates, holidays=holidays
        ) if available_dates is not None else None
        return TradingDateSemantics(requested, as_of, None, is_today, "calendar_unknown")
    closed = not trading_status
    if available_dates is not None:
        as_of = nearest_previous_trading_day(
            requested, available_dates=available_dates, holidays=holidays
        )
    elif closed:
        as_of = nearest_previous_trading_day(requested, holidays=holidays)
    else:
        as_of = requested
    if as_of is None:
        reason = "no_available_trading_day"
    elif closed:
        reason = "market_closed_using_previous_trading_day"
    elif as_of != requested:
        reason = "evidence_as_of_differs_from_requested_date"
    else:
        reason = "requested_date_is_trading_day"
    return TradingDateSemantics(requested, as_of, closed, is_today, reason)


# Friendly aliases for callers/tests that use shorter names.
is_trading_day = is_a_share_trading_day
resolve_date_semantics = resolve_trading_date_semantics
