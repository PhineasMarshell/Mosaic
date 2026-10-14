"""Complete deterministic quote fixture for transport/storage tests (no network)."""

from app.models.response import MarketIntelligence

QUOTE_CLAIM = "贵州茅台(600519)在2026-10-09收盘价为1488元"


def verified_quote_report(**fields) -> MarketIntelligence:
    """Exercise success/retry branches with a fully auditable claim."""
    return MarketIntelligence(**{
        "requested_date": "2026-10-09", "as_of_date": "2026-10-09",
        "what_happened": QUOTE_CLAIM,
        "claims": [{"claim": QUOTE_CLAIM, "claim_type": "fact", "evidence_ids": ["fixture-quote-001"]}],
        "evidence": [{
            "id": "fixture-quote-001", "source_tool": "get_market_quotes", "domain": "a_share",
            "metric": "close", "value": 1488.0, "instrument": "SH600519", "instrument_name": "贵州茅台",
            "as_of_date": "2026-10-09", "retrieved_at": "2026-10-14T01:00:00Z",
            "source": "exchange", "authority": "exchange", "completeness": "complete", "coverage": "instrument",
        }], **fields,
    })
