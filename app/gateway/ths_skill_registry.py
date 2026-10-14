"""同花顺问财 Skill 的单一注册表。

P5/P6 的十个官方 Skill 与后续查询/筛选 Skill 的真实调用形态、能力和契约集中
声明在这里，运行时适配器只按 operation id 查表，不在 Evidence Gate
里为每个 Skill 增加特殊分支。

真实形态：官方 HTTPS API。query2data Skill 使用 ``/v1/query2data``；
公告/新闻/研报 Skill 使用 ``/v1/comprehensive/search``。它们不经过
iiix/MCP，Skill 返回成功也不能证明 MCP TLS 或上游已经修复。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SkillSpec:
    name: str
    operation_id: str
    logical_key: str
    version: str
    transport: str
    endpoint: str
    skill_id: str
    invocation: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    capabilities: tuple[str, ...]
    authority: str = "同花顺问财"
    depends_on_mcp: bool = False
    returns_raw_datum: bool = True


_QUERY_INPUT = {
    "type": "object",
    "required": ["query"],
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 2000},
        "page": {"type": "integer", "minimum": 1, "default": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
        "is_cache": {"type": "boolean", "default": True},
        "expand_index": {"type": "boolean", "default": True},
    },
}
_SEARCH_INPUT = {
    "type": "object",
    "required": ["query"],
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 2000},
        "size": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
    },
}
_QUERY_OUTPUT = {
    "type": "object",
    "required": ["datas"],
    "properties": {
        "status_code": {"type": ["integer", "null"]},
        "code_count": {"type": ["integer", "string", "null"]},
        "columns": {"type": ["array", "null"]},
        "datas": {"type": "array", "items": {"type": "object"}},
    },
}
_SEARCH_OUTPUT = {
    "type": "object",
    "required": ["status_code", "data"],
    "properties": {
        "status_code": {"type": "integer"},
        "data": {"type": "array", "items": {"type": "object"}},
    },
}


def _query(name: str, operation_id: str, logical_key: str, skill_id: str, *capabilities: str) -> SkillSpec:
    return SkillSpec(
        name=name,
        operation_id=operation_id,
        logical_key=logical_key,
        version="1.0.0",
        transport="http_api",
        endpoint="/v1/query2data",
        skill_id=skill_id,
        invocation="POST JSON query/page/limit/is_cache/expand_index with X-Claw-Skill-* headers",
        input_schema=_QUERY_INPUT,
        output_schema=_QUERY_OUTPUT,
        capabilities=tuple(capabilities),
    )


def _search(name: str, operation_id: str, logical_key: str, skill_id: str, channel: str, *capabilities: str) -> SkillSpec:
    return SkillSpec(
        name=name,
        operation_id=operation_id,
        logical_key=logical_key,
        version="1.0.0",
        transport="http_api",
        endpoint="/v1/comprehensive/search",
        skill_id=skill_id,
        invocation=f"POST JSON query/channels=[{channel}]/app_id=AIME_SKILL/size with X-Claw-Skill-* headers",
        input_schema=_SEARCH_INPUT,
        output_schema=_SEARCH_OUTPUT,
        capabilities=tuple(capabilities),
    )


THS_SKILL_SPECS: tuple[SkillSpec, ...] = (
    _query("finance", "internal_iwencai_finance_query", "iwencai_finance", "hithink-finance-query", "finance"),
    _query("events", "internal_iwencai_event_query", "iwencai_events", "hithink-event-query", "abnormal_reasons", "events"),
    _query("basicinfo", "internal_iwencai_basicinfo_query", "iwencai_basicinfo", "hithink-basicinfo-query", "entity_lookup"),
    _query("market", "internal_iwencai_market_query", "iwencai_market", "hithink-market-query", "quote", "kline", "market_breadth", "limit_up_count", "limit_up_pool"),
    _query("macro", "internal_iwencai_macro_query", "iwencai_macro", "hithink-macro-query", "macro"),
    _query("industry", "internal_iwencai_industry_query", "iwencai_industry", "hithink-industry-query", "sector_theme", "market_breadth", "limit_up_sectors"),
    _query("management", "internal_iwencai_management_query", "iwencai_management", "hithink-management-query", "ownership"),
    _search("announcement", "internal_iwencai_announcement_search", "announcement_search", "announcement-search", "announcement", "news"),
    _search("report", "internal_iwencai_report_search", "iwencai_report_search", "report-search", "report", "news"),
    _search("news", "internal_iwencai_news_search", "iwencai_news_search", "news-search", "news", "news"),
    _query("index", "internal_iwencai_index_query", "iwencai_index", "hithink-zhishu-query", "index_quote"),
    _query("astock_selector", "internal_iwencai_astock_selector", "iwencai_astock_selector", "hithink-astock-selector", "stock_screening"),
    _query("sector_selector", "internal_iwencai_sector_selector", "iwencai_sector_selector", "hithink-sector-selector", "sector_screening"),
    _query("business", "internal_iwencai_business_query", "iwencai_business", "hithink-business-query", "company_operations"),
    _query("insresearch", "internal_iwencai_insresearch_query", "iwencai_insresearch", "hithink-insresearch-query", "institutional_opinions"),
)

# The original P6 acceptance remains a ten-Skill scope; additions have their
# own probes and do not retroactively change historical acceptance records.
P6_BASELINE_SKILLS: tuple[SkillSpec, ...] = THS_SKILL_SPECS[:10]

THS_SKILL_REGISTRY: dict[str, SkillSpec] = {item.operation_id: item for item in THS_SKILL_SPECS}
THS_SKILL_BY_KEY: dict[str, SkillSpec] = {item.logical_key: item for item in THS_SKILL_SPECS}
THS_CAPABILITY_MATRIX: dict[str, tuple[str, ...]] = {
    capability: tuple(item.logical_key for item in THS_SKILL_SPECS if capability in item.capabilities)
    for capability in {cap for item in THS_SKILL_SPECS for cap in item.capabilities}
}

THS_PRIMARY_ROUTE: dict[str, tuple[str, str]] = {
    "get_market_quotes": ("internal_iwencai_market_query", "quote"),
    "get_market_klines": ("internal_iwencai_market_query", "kline"),
    "get_ashare_sentiment": ("internal_iwencai_market_query", "market_breadth"),
    "get_limit_up_count": ("internal_iwencai_market_query", "limit_up_count"),
    "list_limit_up_stocks": ("internal_iwencai_market_query", "limit_up_pool"),
    "list_limit_up_sectors": ("internal_iwencai_industry_query", "limit_up_sectors"),
    "get_company_overview": ("internal_iwencai_market_query", "quote"),
    "get_company_finance": ("internal_iwencai_finance_query", "finance"),
    "get_company_business": ("internal_iwencai_basicinfo_query", "entity_lookup"),
    "get_company_concepts": ("internal_iwencai_industry_query", "sector_theme"),
    "get_company_shareholders": ("internal_iwencai_management_query", "ownership"),
    "get_company_survey": ("internal_iwencai_event_query", "events"),
    "news_search": ("internal_iwencai_news_search", "news"),
    "internal_symbol_news": ("internal_iwencai_news_search", "news"),
    "internal_news_digest": ("internal_iwencai_news_search", "news"),
    "internal_market_telegraph": ("internal_iwencai_news_search", "news"),
    "internal_iwencai_announcement_search": ("internal_iwencai_announcement_search", "announcement"),
    "internal_iwencai_finance_query": ("internal_iwencai_finance_query", "finance"),
    "internal_iwencai_macro_query": ("internal_iwencai_macro_query", "macro"),
    "internal_iwencai_industry_query": ("internal_iwencai_industry_query", "sector_theme"),
}


def get_skill_spec(operation_id: str) -> SkillSpec:
    try:
        return THS_SKILL_REGISTRY[operation_id]
    except KeyError as exc:
        raise KeyError(f"Unknown Tonghuashun Skill operation: {operation_id}") from exc


def is_ths_skill(operation_id: str) -> bool:
    return operation_id in THS_SKILL_REGISTRY


def registry_snapshot() -> list[dict[str, Any]]:
    """Return a JSON-safe, secret-free registry snapshot for acceptance logs."""
    return [
        {
            "name": spec.name,
            "operation_id": spec.operation_id,
            "logical_key": spec.logical_key,
            "version": spec.version,
            "transport": spec.transport,
            "endpoint": spec.endpoint,
            "skill_id": spec.skill_id,
            "invocation": spec.invocation,
            "input_schema": spec.input_schema,
            "output_schema": spec.output_schema,
            "capabilities": list(spec.capabilities),
            "authority": spec.authority,
            "depends_on_mcp": spec.depends_on_mcp,
            "returns_raw_datum": spec.returns_raw_datum,
        }
        for spec in THS_SKILL_SPECS
    ]
