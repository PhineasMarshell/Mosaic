"""Authoritative A-share security-name registry helpers.

The small mapping in :mod:`app.gateway.stock_codes` is useful for planning
requests, but it is deliberately not treated as the A-share universe.  This
module wraps the project's existing AkShare loader and gives callers a
source/date-bearing snapshot.  Entity checks can also consume a snapshot
already attached to a successful tool response, which keeps the audit path
deterministic when the upstream registry is unavailable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.cache import market_cache

_CACHE_KEY = "internal:a_share_entity_registry"


def normalize_security_code(value: object) -> str | None:
    """Return a comparable six-digit A-share code, or ``None``."""

    text = str(value or "").strip().upper()
    if text.startswith(("SH", "SZ", "BJ")):
        text = text[2:]
    if len(text) == 6 and text.isdigit():
        return text
    return None


@dataclass(frozen=True)
class RegistrySnapshot:
    """A code/name snapshot with provenance."""

    mapping: Mapping[str, str] = field(default_factory=dict)
    source: str = "unavailable"
    as_of: str | None = None
    available: bool = False
    market: str = "a_share"
    listing_status: str = "unknown"
    as_of_basis: str = "retrieval_date"
    authority: str = "reference"

    @property
    def name_to_code(self) -> dict[str, str]:
        return {name: code for code, name in self.mapping.items() if name and code}


def snapshot_from_mapping(
    mapping: Mapping[object, object] | None,
    *,
    source: str,
    as_of: str | None = None,
    authority: str = "reference",
) -> RegistrySnapshot:
    """Normalize a loader result while retaining source metadata."""

    normalized: dict[str, str] = {}
    for raw_code, raw_name in (mapping or {}).items():
        code = normalize_security_code(raw_code)
        name = str(raw_name or "").strip()
        if code and name:
            normalized[code] = name
    return RegistrySnapshot(
        mapping=normalized,
        source=source,
        as_of=as_of or datetime.now(UTC).date().isoformat(),
        available=bool(normalized),
        authority=authority,
    )


async def fetch_full_a_share_registry(*, timeout: float = 60.0) -> RegistrySnapshot:
    """Load the full A-share code/name table through the existing adapter.

    Failure is represented as an unavailable snapshot.  Callers must preserve
    that state as ``unverified``; they must never infer non-existence from it.
    """

    try:
        from app.research.news_sources.akshare_sources import fetch_code_name_map

        mapping = await fetch_code_name_map(timeout=timeout)
    except Exception:  # noqa: BLE001 - data source failure is an audit state
        return RegistrySnapshot(source="akshare.stock_info_a_code_name", available=False, authority="unavailable")
    return snapshot_from_mapping(
        mapping,
        source="akshare.stock_info_a_code_name",
        as_of=datetime.now(UTC).date().isoformat(),
    )


async def get_full_a_share_registry(*, timeout: float = 15.0, ttl: float = 86400.0) -> RegistrySnapshot:
    """Return a cached snapshot; failed loads expire quickly for later retry."""

    cached = market_cache.get(_CACHE_KEY)
    if isinstance(cached, RegistrySnapshot):
        return cached
    snapshot = await fetch_full_a_share_registry(timeout=timeout)
    market_cache.set(_CACHE_KEY, snapshot, ttl=ttl if snapshot.available else 60.0)
    return snapshot
