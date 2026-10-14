"""Fixed official query2data Skill identities (no user-selected endpoints)."""

# operationId -> (Mosaic logical key, official Skill ID)
QUERY_TOOLS: dict[str, tuple[str, str]] = {
    "internal_iwencai_finance_query": ("iwencai_finance", "hithink-finance-query"),
    "internal_iwencai_event_query": ("iwencai_events", "hithink-event-query"),
    "internal_iwencai_basicinfo_query": ("iwencai_basicinfo", "hithink-basicinfo-query"),
    "internal_iwencai_market_query": ("iwencai_market", "hithink-market-query"),
    "internal_iwencai_macro_query": ("iwencai_macro", "hithink-macro-query"),
    "internal_iwencai_industry_query": ("iwencai_industry", "hithink-industry-query"),
    "internal_iwencai_management_query": ("iwencai_management", "hithink-management-query"),
    "internal_iwencai_index_query": ("iwencai_index", "hithink-zhishu-query"),
    "internal_iwencai_astock_selector": ("iwencai_astock_selector", "hithink-astock-selector"),
    "internal_iwencai_sector_selector": ("iwencai_sector_selector", "hithink-sector-selector"),
    "internal_iwencai_business_query": ("iwencai_business", "hithink-business-query"),
    "internal_iwencai_insresearch_query": ("iwencai_insresearch", "hithink-insresearch-query"),
}

# operationId -> (Mosaic logical key, official Skill ID, fixed channel)
SEARCH_TOOLS: dict[str, tuple[str, str, str]] = {
    "internal_iwencai_announcement_search": ("announcement_search", "announcement-search", "announcement"),
    "internal_iwencai_report_search": ("iwencai_report_search", "report-search", "report"),
    "internal_iwencai_news_search": ("iwencai_news_search", "news-search", "news"),
}

IWENCAI_TOOL_KEYS = {op: spec[0] for op, spec in {**QUERY_TOOLS, **SEARCH_TOOLS}.items()}
