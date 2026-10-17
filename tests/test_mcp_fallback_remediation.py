"""MCP normalize / provenance / controlled fallback / 证据审计的离线回归测试。

对应 ``docs/mcp-fallback-evidence-remediation.md`` 的 P0/P1 契约。本文件**全部离线**：

- 不调用真实网络、THS、MCP 或 live acceptance；
- 只构造上游响应体（``source_used`` / ``items`` / ``data`` / ``rows``）走真实的
  ``normalize_tool_result`` 与 ``ControlledFallbackRouter``；
- LLM 只在 supervisor 用例里换成固定 JSON 的 fake client
  （与 ``tests/test_supervisor_gap_loop.py`` 同一手法），节点本体照常跑。

覆盖的九项契约：

1. MCP quote / limit-up sectors 的 source / authority / retrieved_at provenance；
2. 结构性顶层 count/total 元数据不参与逐条 provenance，真实业务 count 仍参与；
3. primary 缺能力必需字段（quote 缺日期、sentiment 缺涨跌家数、
   limit_up_count 缺涨停家数）触发 fallback；
4. ``gateway_business_error`` 与原 retryable 集合可回退，auth/conflict 仍禁止；
5. limit_up_sectors 不被压成不存在的 ``limit_up``；
6. 多指数 quote 参数保留、裸代码只转 ``SH000300``；
7. sentiment / limit_up_count 的 ``{date}`` 与 ``{}`` 是同一个变体；
8. ``budget_truncated`` 按精确 tool_key + canonical arguments 记账，且与最终 route 一致；
9. news 单篇 ``completeness=search_results`` 可在严格审计下支持窄事实，
   但通用 evidence-only fallback 仍拒绝 ``search_results``。
"""

from __future__ import annotations

import asyncio
import re
import types
from typing import Any

import pytest

from app.gateway.arguments import canonicalize_tool_arguments, semantic_signature
from app.gateway.controlled_fallback import (
    ControlledFallbackRouter,
    _has_provenance,
    _missing_required_field_groups,
    _required_field_groups,
    fallback_allowed,
    fallback_satisfies_provenance,
    is_market_capability,
    needs_fallback,
    primary_trigger_reason,
)
from app.gateway.normalizer import is_structural_metadata_datum, normalize_tool_result
from app.gateway.tool_registry import registry_text
from app.graph.gap_loop import append_gap_variants, coverage_requirements
from app.graph.market_plan import A_SHARE_BENCHMARK_SYMBOLS, apply_market_summary_prefix
from app.graph.nodes.supervisor import SupervisorNode
from app.models.market import NormalizedDatum, ToolResult
from app.models.research import ResearchPlan, ToolCallPlan
from app.research.evidence import build_evidence
from app.research.reasoning import build_evidence_only_fallback

# ------------------------------------------------------------------ #
# 上游响应体 fixture（全部离线，字段名取自真实 MCP / Gateway 响应）      #
# ------------------------------------------------------------------ #

#: 腾讯行情：2 个指数标的，逐条带 date；顶层还有信封级 code/count。
QUOTE_RAW: dict[str, Any] = {
    "code": 0,
    "source_used": "tencent",
    "count": 2,
    "data": [
        {"code": "SH000001", "name": "上证指数", "price": 3210.5, "change_pct": 0.42, "date": "2026-10-14"},
        {"code": "SH000300", "name": "沪深300", "price": 4021.3, "change_pct": 0.31, "date": "2026-10-14"},
    ],
}
QUOTE_ARGS = {"symbols": ["SH000001", "SH000300"]}

#: 同花顺涨停板块：421 条里截取 3 条；顶层 count 是 row_count 型信封计数。
SECTORS_RAW: dict[str, Any] = {
    "source": "ashare-master",
    "count": 421,
    "rows": [
        {"板块": "人形机器人", "涨停家数": 12, "涨停原因": "特斯拉 Optimus 量产", "date": "2026-10-14"},
        {"板块": "固态电池", "涨停家数": 8, "涨停原因": "量产装车预期", "date": "2026-10-14"},
        {"板块": "算力租赁", "涨停家数": 6, "涨停原因": "算力涨价", "date": "2026-10-14"},
    ],
}

#: 缺日期的行情：quote 能力判定必须因此触发 fallback。
QUOTE_NO_DATE_RAW: dict[str, Any] = {
    "source_used": "tencent",
    "data": [{"code": "SH000300", "name": "沪深300", "price": 4021.3, "change_pct": 0.31}],
}

#: 缺涨跌家数的市场情绪：sentiment 能力判定必须因此触发 fallback。
SENTIMENT_MISSING_FIELDS_RAW: dict[str, Any] = {
    "source_used": "ashare-master",
    "data": [{"code": "000001.SH", "price": 3210.5, "change_pct": 0.42}],
}

#: 缺涨停家数的涨停统计。
LIMIT_UP_COUNT_MISSING_RAW: dict[str, Any] = {
    "source_used": "ashare-master",
    "data": [{"date": "2026-10-14", "limit_up_rate": 0.61}],
}


def _normalize(tool: str, arguments: dict[str, Any], raw: Any, *, error: str | None = None) -> ToolResult:
    return normalize_tool_result(tool, arguments, raw, error=error)


def _quote_result(*, source: str = "tencent", date: str = "2026-10-14") -> ToolResult:
    return _normalize(
        "get_market_quotes",
        {"symbols": ["SH000300"]},
        {
            "source_used": source,
            "data": [{"code": "SH000300", "name": "沪深300", "price": 4021.3, "change_pct": 0.31, "date": date}],
        },
    )


def _datum(
    metric: str,
    value: Any,
    *,
    instrument: str | None = "SH000300",
    date: str | None = "2026-10-14",
    source: str | None = "tencent",
    authority: str | None = "tencent",
    status: str = "success",
    completeness: str = "complete",
    structural: bool = False,
) -> NormalizedDatum:
    return NormalizedDatum(
        tool="get_market_quotes",
        metric=metric,
        value=value,
        instrument=instrument,
        as_of_date=date,
        timestamp=date,
        retrieved_at="2026-10-14T09:00:00+00:00",
        source=source,
        authority=authority,
        status=status,
        completeness=completeness,
        structural_metadata=structural,
        instrument_scope="instrument",
        coverage="instrument",
    )


def _result(datums: list[NormalizedDatum], *, status: str = "success", partial: bool = False,
            reason_codes: list[str] | None = None, retrieved_at: str | None = "2026-10-14T09:00:00+00:00",
            tool: str = "get_market_quotes") -> ToolResult:
    return ToolResult(
        tool=tool,
        tool_key=tool,
        arguments={},
        status=status,
        partial=partial,
        reason_codes=list(reason_codes or []),
        normalized=datums,
        retrieved_at=retrieved_at,
    )


# ------------------------------------------------------------------ #
# 1 / 2：MCP provenance 与结构性元数据                                  #
# ------------------------------------------------------------------ #


def test_mcp_quote_datums_get_source_authority_and_retrieved_at():
    """P0-A：MCP 已返回的 quote 不能再因缺 authority 被整批退回。"""
    result = _normalize("get_market_quotes", QUOTE_ARGS, QUOTE_RAW)

    assert result.status == "success"
    assert result.authority == "tencent"
    assert result.as_of_date == "2026-10-14"
    assert _has_provenance(result, "quote", QUOTE_ARGS) is True

    business = [d for d in result.normalized if not is_structural_metadata_datum(d)]
    assert business, "quote 响应里必须有可交付的业务 datum"
    for datum in business:
        assert datum.source == "tencent"
        assert datum.authority == "tencent"
        assert datum.retrieved_at
        # items/data/rows 里的 date 必须传播到对应 datum
        assert datum.as_of_date == "2026-10-14"


def test_fallback_provenance_requires_both_source_and_authority():
    """来源标签与可追责 provider 缺一不可。"""
    source_only = _quote_result()
    for datum in source_only.normalized:
        datum.authority = None
    assert _has_provenance(source_only, "quote", {"symbols": ["SH000300"]}) is False

    authority_only = _quote_result()
    for datum in authority_only.normalized:
        datum.source = None
    assert _has_provenance(authority_only, "quote", {"symbols": ["SH000300"]}) is False


def test_mcp_limit_up_sectors_provenance_and_capability_names():
    """P0-A/B：涨停板块 datum 带 authority；能力名不得被压成不存在的 ``limit_up``。"""
    result = _normalize("list_limit_up_sectors", {}, SECTORS_RAW)

    assert result.status == "success"
    assert result.authority == "ashare-master"
    assert _has_provenance(result, "limit_up_sectors", {}) is True
    # 缺省必需字段判定不再把 limit_up_sectors 压成 limit_up（后者根本不存在）
    assert _missing_required_field_groups(result, "limit_up_sectors") == []
    assert "limit_up" not in _required_field_groups("limit_up_sectors")
    assert "limit_up_count" not in _required_field_groups("limit_up_sectors")
    assert is_market_capability("limit_up_sectors")
    assert is_market_capability("limit_up_count")

    sectors = [d for d in result.normalized if not is_structural_metadata_datum(d) and "板块" in d.metric]
    assert {d.value for d in sectors} == {"人形机器人", "固态电池", "算力租赁"}
    for datum in sectors:
        assert datum.authority == "ashare-master"
        assert datum.as_of_date == "2026-10-14"


def test_structural_top_level_metadata_is_ignored_but_business_counts_are_not():
    """P0-A：顶层 count/total 是信封元数据；真实业务 count 仍必须可归因。"""
    result = _normalize("get_market_quotes", QUOTE_ARGS, QUOTE_RAW)
    structural = {d.metric for d in result.normalized if is_structural_metadata_datum(d)}
    assert {"code", "count"} <= structural, "顶层 code/count 必须被标记成结构性元数据"
    # 信封元数据没有自己的日期，但业务 datum 有 → 整批仍可归因
    assert _has_provenance(result, "quote", QUOTE_ARGS) is True

    # 真实业务字段名里带 count（sentiment 的 up_count/down_count）不许被当成信封
    # 元数据：它没有自己的日期，quote 这类按标的取数的口径下就该判定为不可归因。
    business_count = _normalize("get_ashare_sentiment", {}, {"source_used": "ashare-master", "up_count": 3000})
    datum = next(d for d in business_count.normalized if d.metric == "up_count")
    assert is_structural_metadata_datum(datum) is False, "业务 count 不得被当成信封元数据"
    assert _has_provenance(business_count, "sentiment", {}) is False
    assert _has_provenance(business_count, "quote", {}) is False
    # 补上上游声明的日期后，聚合型市场级口径即可归因
    dated = _normalize(
        "get_ashare_sentiment", {}, {"source_used": "ashare-master", "up_count": 3000, "date": "2026-10-14"}
    )
    assert _has_provenance(dated, "sentiment", {}) is True


def test_datum_partial_is_separated_from_result_partial():
    """P0-A：整批 partial 不能把每条 datum 都变成 partial。"""
    raw = {
        "source_used": "tencent",
        "partial": True,
        "count": 2,
        "data": [
            {"code": "SH000300", "price": 4021.3, "date": "2026-10-14"},
            {"code": "SZ399006", "price": 2100.1},  # 这一条没有自己的日期
        ],
    }
    result = _normalize("get_market_quotes", {"symbols": ["SH000300", "SZ399006"]}, raw)

    assert result.partial is True
    assert result.status == "partial"
    date_complete = next(d for d in result.normalized if d.metric.endswith(".price") and d.as_of_date)
    date_missing = next(d for d in result.normalized if d.metric.endswith(".price") and not d.as_of_date)
    assert date_complete.partial is False
    assert date_complete.completeness == "complete"
    assert date_missing.partial is True
    assert date_missing.completeness == "partial"


def test_business_count_without_container_is_not_structural_metadata():
    """唯一的 count 响应是涨停家数业务事实，不是 envelope 计数。

    语义补标注（mcp-semantic-annotation-execution-plan）把契约允许的顶层 count
    归一为标准 metric ``limit_up_count``，且只生成一条业务 datum，原始路径记录在
    ``original_field_path``。
    """
    result = _normalize(
        "get_limit_up_count",
        {},
        {"source_used": "ashare-master", "count": 59, "date": "2026-10-14"},
    )
    count = next(d for d in result.normalized if d.metric == "limit_up_count")
    assert is_structural_metadata_datum(count) is False
    assert count.value == 59
    assert count.original_field_path == "count"
    assert count.date_annotation == "observed"
    assert count.semantic_annotation == "contract"
    # canonical limit_up_count 与原始 count 不重复生成两条业务 datum。
    assert len([d for d in result.normalized if not is_structural_metadata_datum(d)]) == 1
    assert _has_provenance(result, "limit_up_count", {}) is True
    assert primary_trigger_reason(result, "limit_up_count", {}) is None


def test_complete_datum_survives_result_partial_in_strict_evidence_chain():
    """真实 normalize → build_evidence → strict matcher 链路保留窄事实。"""
    from app.agent.evidence_gate import match_claims_to_evidence
    from app.models.response import ClaimEvidence, MarketIntelligence

    result = _normalize(
        "get_market_quotes",
        {"symbols": ["SH000300"]},
        {
            "source_used": "tencent",
            "partial": True,
            "data": [{"code": "SH000300", "name": "沪深300", "price": 4021.3, "date": "2026-10-14"}],
        },
    )
    evidence = build_evidence([result])
    price = next(item for item in evidence if item.metric.endswith("price"))
    assert price.partial is False and price.result_partial is True
    report = MarketIntelligence(
        requested_date="2026-10-14",
        as_of_date="2026-10-14",
        claims=[ClaimEvidence(claim="沪深300在2026-10-14点位为4021.3", evidence_ids=[price.id])],
    )
    rows = match_claims_to_evidence(
        report,
        [price],
        expected_date="2026-10-14",
        requested_date="2026-10-14",
        strict_provenance=True,
        expected_domain="a_share",
    )
    assert rows[0]["supported"] is True


def test_fallback_candidate_must_also_have_required_quote_field():
    primary = _normalize("get_market_quotes", {"symbols": ["SH000300"]}, None, error="timeout")
    fallback = _result(
        [_datum("volume", 100, source="tencent", authority="tencent")],
        tool="get_market_quotes",
    )
    assert fallback_satisfies_provenance(primary, fallback, "quote", {"symbols": ["SH000300"]}) is False


def test_quote_requires_numeric_market_field_for_every_requested_instrument():
    metadata_only = _result([_datum("code", "SH000300"), _datum("name", "沪深300")])
    assert needs_fallback(metadata_only, "quote", {"symbols": ["SH000300"]}) is True
    missing_one_index = _quote_result()
    assert needs_fallback(missing_one_index, "quote", {"symbols": ["SH000300", "SZ399006"]}) is True
    empty_price = _result([_datum("price", "missing")])
    assert needs_fallback(empty_price, "quote", {"symbols": ["SH000300"]}) is True


def test_sentiment_requires_both_numeric_up_and_down_counts():
    only_up = _normalize(
        "get_ashare_sentiment", {},
        {"source_used": "ashare-master", "up_count": 3000, "date": "2026-10-14"},
    )
    assert needs_fallback(only_up, "sentiment", {}) is True
    not_numeric = _normalize(
        "get_ashare_sentiment", {},
        {"source_used": "ashare-master", "up_count": 3000, "down_count": "missing", "date": "2026-10-14"},
    )
    assert needs_fallback(not_numeric, "sentiment", {}) is True


@pytest.mark.parametrize("capability", ["sector_theme", "abnormal_reasons"])
def test_nonaggregate_capabilities_cannot_borrow_result_date(capability):
    result = _result([_datum("reason", "业绩上升", date=None)])
    result.as_of_date = "2026-10-14"
    assert _has_provenance(result, capability, {}) is False


def test_metadata_only_result_cannot_satisfy_provenance():
    result = _result([_datum("count", 23, structural=True, date=None)])
    assert _has_provenance(result, "quote", {}) is False


def test_limit_up_pool_dates_remain_independent_in_plan():
    from app.graph.market_plan import apply_as_of_date_to_plan

    plan = _market_plan(
        ToolCallPlan(tool_key="limit_up_pool", arguments={"date": "2026-10-07"}, purpose="前一日"),
        ToolCallPlan(tool_key="limit_up_pool", arguments={"date": "2026-10-08"}, purpose="后一日"),
    )
    apply_as_of_date_to_plan(plan, "2026-10-08")
    assert [step.arguments for step in plan.steps] == [{"date": "2026-10-07"}, {"date": "2026-10-08"}]


def test_complete_datum_with_result_partial_still_rejects_exhaustive_claim():
    from app.agent.evidence_gate import match_claims_to_evidence

    price = next(item for item in build_evidence([_quote_result()]) if item.metric.endswith("price"))
    price.result_partial = True
    rows = match_claims_to_evidence(
        {"claims": [{"claim": "2026-10-14全市场所有指数上涨", "evidence_ids": [price.id]}]},
        [price], expected_date="2026-10-14", strict_provenance=True, expected_domain="a_share",
    )
    assert rows[0]["supported"] is False
    assert "partial_only" in rows[0]["reason_codes"]


def test_fallback_transport_error_keeps_primary_narrow_fact():
    primary = _normalize(
        "get_market_quotes",
        {"symbols": ["SH000300", "SZ399006"]},
        {
            "source_used": "tencent",
            "partial": True,
            "data": [{"code": "SH000300", "price": 4021.3, "date": "2026-10-14"}],
        },
    )
    fallback_error = _normalize("get_market_quotes", {}, None, error="mcp upstream timeout")
    out = asyncio.run(
        ControlledFallbackRouter().call(
            run_id="keep-primary",
            capability="quote",
            arguments={"symbols": ["SH000300", "SZ399006"]},
            primary_source="ths_skill",
            primary_transport="http_api",
            primary=lambda: _async(primary),
            fallback_source="mcp",
            fallback_transport="mcp",
            fallback=lambda: _async(fallback_error),
        )
    )
    assert out.normalized and out.status == "partial"
    assert next(d for d in out.normalized if d.metric.endswith("price")).as_of_date == "2026-10-14"
    assert out.source_switch is False
    assert out.fallback_source == "mcp"
    assert "fallback_error" in (out.fallback_reason or "")


# ------------------------------------------------------------------ #
# 3 / 4 / 5：controlled fallback 触发与能力判定                          #
# ------------------------------------------------------------------ #


def test_primary_missing_required_fields_triggers_fallback():
    """P0-B：quote 缺日期、sentiment 缺涨跌家数、limit_up_count 缺涨停家数都要回退。"""
    quote = _normalize("get_market_quotes", {"symbols": ["SH000300"]}, QUOTE_NO_DATE_RAW)
    assert primary_trigger_reason(quote, "quote", {"symbols": ["SH000300"]}) == "missing_required_fields:quote"
    assert needs_fallback(quote, "quote", {"symbols": ["SH000300"]}) is True

    sentiment = _normalize(
        "get_ashare_sentiment", {}, SENTIMENT_MISSING_FIELDS_RAW
    )
    assert needs_fallback(sentiment, "sentiment", {}) is True
    assert "上涨家数" in _required_field_groups("sentiment")[0]

    limit_up = _normalize("get_limit_up_count", {}, LIMIT_UP_COUNT_MISSING_RAW)
    assert needs_fallback(limit_up, "limit_up_count", {}) is True


def test_primary_with_required_fields_is_kept_even_when_partial():
    """P0-B：字段齐全的 partial primary 不触发回退（整批 partial 不是回退理由）。"""
    result = _quote_result()
    result.status = "partial"
    result.partial = True
    result.reason_codes = ["missing_as_of_date"]

    assert primary_trigger_reason(result, "quote", {"symbols": ["SH000300"]}) is None
    assert needs_fallback(result, "quote", {"symbols": ["SH000300"]}) is False


def test_gateway_business_error_is_retryable_and_routed():
    """P0-B：``gateway_business_error`` 必须进可回退错误集合。"""
    primary = _normalize(
        "internal_iwencai_industry", {}, None,
        error="gateway_business_error: iwencai_industry rejected the request",
    )
    assert primary.status == "error"
    assert primary.reason_codes == ["gateway_business_error"]
    assert needs_fallback(primary, "limit_up_sectors", {}) is True

    good = _normalize("list_limit_up_sectors", {}, SECTORS_RAW)
    assert fallback_allowed(primary, good, "limit_up_sectors", {}) is True

    routed = asyncio.run(
        ControlledFallbackRouter().call(
            run_id="business-error",
            capability="limit_up_sectors",
            arguments={},
            primary_source="ths_skill",
            primary_transport="http_api",
            primary=lambda: _async(primary),
            fallback_source="mcp",
            fallback_transport="mcp",
            fallback=lambda: _async(good),
        )
    )
    assert routed.status == "success"
    assert routed.source_switch is True
    assert routed.fallback_source == "mcp"
    assert routed.fallback_reason == "gateway_business_error"


def test_auth_and_conflict_never_trigger_fallback():
    """P0-B：auth / conflict 是不变量，partial 缺字段也不许换源。"""
    auth = _normalize("get_ashare_sentiment", {}, None, error="authentication failed: 401")
    assert auth.reason_codes == ["gateway_auth_error"]
    assert primary_trigger_reason(auth, "sentiment", {}) is None
    assert needs_fallback(auth, "sentiment", {}) is False

    conflict = _result([_datum("up_count", 1)], reason_codes=["authority_conflict"])
    assert primary_trigger_reason(conflict, "sentiment", {}) is None
    assert fallback_satisfies_provenance(conflict, _quote_result(), "sentiment", {}) is False


async def _async(value: ToolResult) -> ToolResult:
    return value


def test_router_keeps_zero_datum_invariant_when_fallback_provenance_fails():
    """P0-B：fallback 自身证不出 provenance 时，结果必须保持零 datum 窄事实。"""
    primary = _normalize("get_ashare_sentiment", {}, None, error="gateway_business_error: rejected")
    bad_fallback = _result(
        [_datum("up_count", 3000, date=None, source="ashare-master", authority="ashare-master")],
        status="partial",
        partial=True,
        tool="get_ashare_sentiment",
    )
    bad_fallback.retrieved_at = None
    assert fallback_satisfies_provenance(primary, bad_fallback, "sentiment", {}) is False

    out = asyncio.run(
        ControlledFallbackRouter().call(
            run_id="bad-fallback",
            capability="sentiment",
            arguments={},
            primary_source="ths_skill",
            primary_transport="http_api",
            primary=lambda: _async(primary),
            fallback_source="mcp",
            fallback_transport="mcp",
            fallback=lambda: _async(bad_fallback),
        )
    )
    assert out.status == "error"
    assert out.normalized == []
    assert out.fallback_reason == "fallback_provenance_insufficient"
    assert out.source_switch is True


def test_fallback_candidate_accepts_healthy_primary_missing_provenance():
    """P0-B：primary 健康但缺必需字段时，候选采纳判定不再要求 primary 是 error。"""
    primary = _normalize("get_ashare_sentiment", {}, {"source_used": "ashare-master"})
    assert primary.status == "error"
    assert primary.reason_codes == ["empty_normalized"]
    # 空载荷没有业务 datum → 只能靠 fallback 补
    assert needs_fallback(primary, "sentiment", {}) is True

    good = _normalize(
        "get_ashare_sentiment", {},
        {"source_used": "ashare-master", "up_count": 3000, "down_count": 2000, "date": "2026-10-14"},
    )
    assert good.status == "success"
    # 旧入口刻意保留"primary 必须 error"的语义（给旧调用方与既有测试）
    assert fallback_allowed(primary, good, "sentiment", {}) is True
    # 新入口按"候选能否自证 provenance"判定
    assert fallback_satisfies_provenance(primary, good, "sentiment", {}) is True

    # 真正的"健康但缺必需字段"：有业务 datum、但没有涨跌家数
    healthy_missing = _normalize(
        "get_ashare_sentiment", {},
        {"source_used": "ashare-master", "data": [{"code": "000001.SH", "price": 3210.5}]},
    )
    assert healthy_missing.status == "success"
    assert primary_trigger_reason(healthy_missing, "sentiment", {}) == "missing_required_fields:sentiment"
    assert fallback_satisfies_provenance(healthy_missing, good, "sentiment", {}) is True


# ------------------------------------------------------------------ #
# 6 / 7：market plan 参数保留与变体去重                                  #
# ------------------------------------------------------------------ #


def _market_plan(*steps: ToolCallPlan) -> ResearchPlan:
    return ResearchPlan.model_validate(
        {
            "intent": {
                "domain": "a_share",
                "task": "market_summary",
                "time_scope": "today",
                "question": "今天A股大盘怎么样",
            },
            "steps": [s.model_dump() for s in steps],
        }
    )


def test_default_benchmarks_are_prefixed_and_legacy_alias_points_at_sh000300():
    assert list(A_SHARE_BENCHMARK_SYMBOLS) == ["SH000001", "SH000300", "SZ399006"]
    assert canonicalize_tool_arguments("get_market_quotes", {"symbols": ["000300"]})["symbols"] == ["SH000300"]
    assert canonicalize_tool_arguments("get_market_quotes", {"symbols": ["399006"]})["symbols"] == ["SZ399006"]


def test_critic_multi_index_quote_arguments_are_preserved():
    """P0-C：planner/critic 已给出的多指数 quote 参数不得被代码口径覆盖。"""
    plan = _market_plan(
        ToolCallPlan(
            tool_key="quote",
            arguments={"symbols": ["SH000001", "SZ399006", "000300"]},
            purpose="critic 多指数",
            priority="high",
        )
    )
    prefix_keys, _dropped = apply_market_summary_prefix(
        plan, question="今天A股大盘怎么样", registry=registry_text(domains=["a_share"]), max_steps=8
    )
    quotes = [s for s in plan.steps if s.tool_key == "quote"]
    assert len(quotes) == 1
    # Gateway 边界会对 symbols 排序，这里只要求"三个指数一个不少、原样保留"，
    # 且没有被代码口径的三指数覆盖掉（覆盖会丢掉 SZ399006 之外的差异）。
    assert set(quotes[0].arguments["symbols"]) == {"SH000001", "SH000300", "SZ399006"}
    assert "quote" in prefix_keys
    assert [s.tool_key for s in plan.steps].count("quote") == 1


def test_single_quote_step_is_replaced_by_the_code_owned_benchmarks():
    """P0-C：只给了单标的/个股时仍用代码口径三指数补齐，且不产生重复步骤。"""
    plan = _market_plan(ToolCallPlan(tool_key="quote", arguments={"symbol": "SH600519"}, purpose="p"))
    apply_market_summary_prefix(
        plan, question="今天A股大盘怎么样", registry=registry_text(domains=["a_share"]), max_steps=8
    )
    quotes = [s for s in plan.steps if s.tool_key == "quote"]
    assert len(quotes) == 1
    assert set(quotes[0].arguments["symbols"]) == set(A_SHARE_BENCHMARK_SYMBOLS)


def test_date_and_empty_variants_collapse_into_one_for_unparameterized_tools():
    """P0-C：sentiment/limit_up_count/sectors 的 ``{date}`` 与 ``{}`` 是同一变体。"""
    critique = {
        "verdict": "research_more",
        "issues": [
            {
                "action": "research_more",
                "required_tool_keys": ["sentiment"],
                "required_coverage": {"arguments": {"date": "2026-10-07"}},
            },
            {
                "action": "research_more",
                "required_tool_keys": ["sentiment"],
                "required_coverage": {"arguments": {}},
            },
        ],
    }
    specs = coverage_requirements(critique)
    assert len(specs["sentiment"]["argument_variants"]) == 1

    _selected, rows = append_gap_variants(
        [], ["sentiment"], registry_text(domains=["a_share"]),
        max_steps=8, max_gap_steps=3, question="今天A股怎么样", requirements=specs, results=[],
    )
    assert len(rows) == 1, "同一变体不得被补成两次调用"
    assert rows[0]["arguments"] == {}


def test_limit_up_pool_keeps_distinct_dates_as_distinct_variants():
    """反例保护：``limit_up_pool`` 按日期取数，不同日期必须是两条独立变体。"""
    critique = {
        "verdict": "research_more",
        "issues": [
            {
                "action": "research_more",
                "required_tool_keys": ["limit_up_pool"],
                "required_coverage": {"arguments": {"date": "2026-10-07"}},
            },
            {
                "action": "research_more",
                "required_tool_keys": ["limit_up_pool"],
                "required_coverage": {"arguments": {"date": "2026-10-08"}},
            },
        ],
    }
    specs = coverage_requirements(critique)
    assert len(specs["limit_up_pool"]["argument_variants"]) == 2


# ------------------------------------------------------------------ #
# 8：budget_truncated 与最终 route 一致                                 #
# ------------------------------------------------------------------ #


def test_budget_truncated_records_the_exact_variant_not_in_the_final_plan():
    """P1：被预算挤掉的精确变体必须记为 budget_truncated，且不出现在最终计划/route 里。"""
    critique = {
        "verdict": "research_more",
        "issues": [
            {
                "action": "research_more",
                "required_tool_keys": ["limit_up_pool"],
                "required_coverage": {"arguments": {"date": "2026-10-07"}},
            },
            {
                "action": "research_more",
                "required_tool_keys": ["limit_up_pool"],
                "required_coverage": {"arguments": {"date": "2026-10-08"}},
            },
        ],
    }
    specs = coverage_requirements(critique)
    selected, rows = append_gap_variants(
        [], ["limit_up_pool"], registry_text(domains=["a_share"]),
        max_steps=8, max_gap_steps=1, question="q", requirements=specs, results=[],
    )

    planned = {semantic_signature(s.tool_key, dict(s.arguments or {})) for s in selected}
    kept_rows = {str(row["signature"]) for row in rows}
    dropped_signature = next(
        semantic_signature("limit_up_pool", {"date": date})
        for date in ("2026-10-07", "2026-10-08")
        if semantic_signature("limit_up_pool", {"date": date}) not in kept_rows
    )
    assert kept_rows <= planned
    assert dropped_signature not in planned


# ------------------------------------------------------------------ #
# 9：news search_results 与通用降级门禁                                 #
# ------------------------------------------------------------------ #


def _news_datum(result: Any, field: str = "title") -> Any:
    """从真实归一化结果里取下 ``data[0].<field>`` 这条 datum。"""
    for datum in result.normalized:
        if str(datum.metric).endswith(f".{field}"):
            return datum
    raise AssertionError(f"归一化结果里没有 data[0].{field}")


def _news_evidence(**overrides: Any) -> Any:
    """一篇带日期、带来源的公告类 datum，整批 completeness 记 ``search_results``。

    ``result.partial`` 必须保持 True：真实 iwencai 搜索响应就是"这是一次检索
    结果、不是全量"的信封。恰好用它验证 result_partial 不该拖累完整 datum。
    """
    result = _normalize(
        "internal_iwencai_announcement_search",
        {"query": "宁德时代公告"},
        {
            # 用真实公告源 cninfo（巨潮资讯）：authority 只能来自明确 provider，
            # "同花顺问财"这种泛化 source 不会被冒用成 authority。
            "source_used": "cninfo",
            "partial": True,
            "count": 1,
            "data": [
                {
                    "code": "300750",
                    "name": "宁德时代",
                    "title": "宁德时代发布三季度业绩预告",
                    "summary": "公司预计前三季度净利润同比上升，产能利用率维持高位。",
                    "date": "2026-10-14",
                }
            ],
        },
    )
    result.completeness = "search_results"
    assert result.status == "partial", "fixture 必须复现「整批 partial + 单条完整 datum」"
    datum = _news_datum(result, "title")
    assert datum.as_of_date == "2026-10-14"
    assert datum.partial is False
    evidence = build_evidence([result])
    assert evidence, "新闻结果必须能进 evidence 账本"
    item = next(candidate for candidate in evidence if candidate.metric.endswith(".title"))
    # Keep the status/partial produced by the real chain. Reconstructing an
    # Evidence(status="success") here would hide a normalizer regression.
    return item.model_copy(update={"id": "news-001", "completeness": "search_results", **overrides})


def test_single_news_search_results_can_support_a_narrow_dated_fact():
    """P0-D：单篇新闻/公告允许 completeness=search_results，只要可审阅且有日期。"""
    from app.agent.evidence_gate import match_claims_to_evidence
    from app.models.response import ClaimEvidence, MarketIntelligence

    item = _news_evidence()
    assert item.as_of_date == "2026-10-14"
    assert item.source
    assert item.authority

    report = MarketIntelligence(
        requested_date="2026-10-14",
        as_of_date="2026-10-14",
        claims=[
            ClaimEvidence(
                claim="宁德时代在2026-10-14发布了三季度业绩预告",
                evidence_ids=[item.id],
            )
        ],
    )
    rows = match_claims_to_evidence(
        report,
        [item],
        expected_date="2026-10-14",
        requested_date="2026-10-14",
        strict_provenance=True,
        expected_domain="a_share",
    )
    assert rows[0]["supported"] is True
    assert "partial_only" not in rows[0]["reason_codes"]
    assert "incomplete_completeness" not in rows[0]["reason_codes"]
    assert "unknown_completeness" not in rows[0]["reason_codes"]


def _complete_news_evidence(**overrides: Any) -> Any:
    """同一条新闻，但整批不是 partial：逐条准入的其它条件全部满足。"""
    result = _normalize(
        "internal_iwencai_announcement_search",
        {"query": "宁德时代公告"},
        {
            "source_used": "同花顺问财",
            "count": 1,
            "data": [
                {
                    "title": "宁德时代发布三季度业绩预告",
                    "summary": "公司预计前三季度净利润同比上升，产能利用率维持高位。",
                    "date": "2026-10-14",
                }
            ],
        },
    )
    evidence = build_evidence([result])
    assert evidence
    item = next(
        (row for row in evidence if str(getattr(row, "metric", "")).endswith(".title")),
        evidence[0],
    )
    item.instrument = "300750"
    item.coverage = "instrument"
    for key, value in overrides.items():
        setattr(item, key, value)
    return item


def test_generic_evidence_only_fallback_still_rejects_search_results():
    """P0-D：通用 evidence-only fallback 的完整性要求不得因新闻例外被放宽。"""
    listed = _complete_news_evidence(completeness="search_results")
    assert listed.status == "success"
    assert listed.partial is False
    assert build_evidence_only_fallback(
        [listed], requested_date="2026-10-14", target_instrument="300750"
    ) is None, "search_results 是响应信封，通用降级门禁必须继续拒绝它"

    complete = _complete_news_evidence(completeness="complete")
    assert complete.as_of_date == "2026-10-14"
    assert build_evidence_only_fallback(
        [complete], requested_date="2026-10-14", target_instrument="300750"
    ) is not None


def test_news_exception_requires_reviewable_content_and_a_date():
    """P0-D：没有日期 / 没有可审阅正文的 search_results 不得借例外过关。"""
    from app.agent.evidence_gate import _single_news_evidence_allowed

    item = _news_evidence()
    assert item.as_of_date == "2026-10-14"
    claim = "宁德时代在2026-10-14发布了三季度业绩预告"
    assert _single_news_evidence_allowed(item, is_partial=False, source_tool="internal_iwencai_announcement_search", claim=claim)

    # datum 自身 partial 永远不属于例外
    assert not _single_news_evidence_allowed(
        item, is_partial=True, source_tool="internal_iwencai_announcement_search", claim=claim
    )
    # 没有可审阅正文（只剩一个数字字段）也不是例外
    plain = _news_evidence(metric="price", value=4021.3)
    assert not _single_news_evidence_allowed(
        plain, is_partial=False, source_tool="internal_iwencai_announcement_search", claim=claim
    )
    # 没有来源同样不是例外
    anonymous = _news_evidence(source=None, authority=None)
    assert not _single_news_evidence_allowed(
        anonymous, is_partial=False, source_tool="internal_iwencai_announcement_search", claim=claim
    )
    # completeness 不是检索信封时也不走这条例外
    complete = _news_evidence(completeness="complete")
    assert not _single_news_evidence_allowed(
        complete, is_partial=False, source_tool="internal_iwencai_announcement_search", claim=claim
    )


def test_a_share_market_news_without_a_single_security_code_is_not_a_domain_mismatch():
    """P0-D：无唯一证券代码的 A 股市场新闻不得被判 domain_mismatch；境外仍拒绝。"""
    from app.agent.evidence_gate import evidence_domain_compatible

    class _Item:
        domain = "media"
        instrument = ""

    assert evidence_domain_compatible(_Item(), "今日市场报道：两市成交额放大", "a_share") is True

    # 市场级证据不得替具名境外实体背书
    assert evidence_domain_compatible(_Item(), "腾讯控股在2026-10-14发布公告", "a_share") is False

    class _HK:
        domain = "media"
        instrument = "00700"

    assert evidence_domain_compatible(_HK(), "今日市场报道：两市成交额放大", "a_share") is False

    class _US:
        domain = "media"
        instrument = "AAPL"

    assert evidence_domain_compatible(_US(), "今日市场报道：两市成交额放大", "a_share") is False


# ------------------------------------------------------------------ #
# supervisor：最终 route 只能来自最终 plan                              #
# ------------------------------------------------------------------ #


class _FakeCompletions:
    def __init__(self, payload: str):
        self.payload = payload

    async def create(self, **kwargs):  # noqa: ARG002 - fake OpenAI surface
        message = types.SimpleNamespace(content=self.payload)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


def _supervisor_node(payload: str, *, gap_max_steps: int = 1) -> SupervisorNode:
    node = SupervisorNode.__new__(SupervisorNode)
    node.settings = types.SimpleNamespace(
        max_conversation_turns=10,
        max_research_steps=8,
        openai_model="test-model",
        gap_max_steps=gap_max_steps,
    )
    node.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=_FakeCompletions(payload)))
    node._last_forced_gap = []
    node._gap_state = {}
    return node


@pytest.mark.asyncio
@pytest.mark.parametrize("gap_budget", [1, 2])
async def test_gap_disposition_matches_the_final_route_exactly(gap_budget):
    """P1：kept 变体必在最终 route，budget_truncated 变体必不在。"""
    empty_plan = (
        '{"intent":{"domain":"a_share","task":"market_summary","time_scope":"today","question":"q"},'
        '"steps":[]}'
    )
    node = _supervisor_node(empty_plan, gap_max_steps=gap_budget)
    state = {
        "question": "今天A股大盘怎么样",
        "domain": "a_share",
        "results": [],
        "critique": {
            "verdict": "research_more",
            "reason": "新闻面数据缺失",
            "missing_points": ["缺少消息面"],
            "missing_tool_keys": ["news_search"],
            "issues": [
                {
                    "action": "research_more",
                    "required_tool_keys": ["news_search"],
                    "required_coverage": {"arguments": {"query": "宁德时代 公告"}},
                },
                {
                    "action": "research_more",
                    "required_tool_keys": ["news_search"],
                    "required_coverage": {"arguments": {"query": "固态电池 量产"}},
                },
                {
                    "action": "research_more",
                    "required_tool_keys": ["news_search"],
                    "required_coverage": {"arguments": {"query": "芯片 发布"}},
                },
            ],
        },
    }

    out = await node(state)
    assert out.get("errors", []) == []

    routed_signatures = set()
    for analyst in out["route"]:
        for call in analyst["tool_calls"]:
            routed_signatures.add(semantic_signature(call["tool_key"], dict(call.get("arguments") or {})))

    disposition = out["gap_key_decisions"]
    assert disposition
    for row in disposition:
        if row["decision"] == "kept" and row.get("signature"):
            assert row["signature"] in routed_signatures, f"kept 变体没进 route: {row}"
        if row.get("reason") == "budget_truncated" and row.get("signature"):
            assert row["signature"] not in routed_signatures, f"被截断的变体仍进了 route: {row}"

    variants = out["executable_gap_variants"]
    assert variants, "必须留下精确变体台账"
    for row in variants:
        assert row["signature"] in routed_signatures, row
    truncated = [row for row in disposition if row.get("reason") == "budget_truncated"]
    assert truncated, "同一 key 的两个变体 + max_gap_steps=1 必须留下 budget_truncated 记录"
    assert all(row.get("signature") for row in truncated), "被截断的记录必须带精确变体签名"
    assert len(truncated) == 3 - gap_budget
    kept = [row for row in disposition if row["decision"] == "kept"]
    assert len(kept) == gap_budget and all(row.get("signature") for row in kept)
    assert len([s for s in routed_signatures if s.startswith("news_search:")]) == gap_budget, (
        "被截断的变体不得出现在最终 route 里"
    )


async def _assert_claim_boundaries(tmp_path, item, claim, *, allowed):
    """Real matcher, Finalize and forged persistence label; isolated three-table DB."""
    from app.agent.evidence_gate import match_claims_to_evidence
    from app.agent.persistence import persist_research
    from app.graph.nodes.finalize import FinalizeAuditNode
    from app.memory.storage import MarketMemory
    from app.models.response import MarketIntelligence, ResearchResponse

    report = MarketIntelligence(what_happened=claim, claims=[{
        "claim": claim, "claim_type": "fact", "evidence_ids": [item.id],
    }], evidence=[item.model_dump()])
    state = {"question": claim, "domain": "a_share", "as_of_date": "2026-10-14",
             "requested_date": "2026-10-14", "planned_as_of_date": "2026-10-14",
             "market_closed": False, "evidence": [item], "report": report,
             "results": [], "errors": [], "critique": {"verdict": "pass"}}
    rows = match_claims_to_evidence(report, [item], expected_date="2026-10-14",
                                    strict_provenance=True, expected_domain="a_share")
    assert rows[0]["supported"] is allowed, rows
    out = await FinalizeAuditNode(None)(state)
    assert out.get("errors", []) == []
    assert (out["delivery_status"] == "verified") is allowed, out
    result = ResearchResponse(question=claim, requested_date="2026-10-14", as_of_date="2026-10-14",
                              market_closed=False, report=report, run_id="remediation-boundary",
                              critique={"verdict": "pass"}, final_audit_status="pass", delivery_status="verified")
    memory = MarketMemory(db_path=tmp_path / "boundary.db")
    try:
        assert await persist_research(result, claim, "offline-remediation-boundary", memory=memory) is allowed
        assert result.persisted is allowed
        counts = {table: memory.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("research_records", "conversations", "daily_states")}
        assert set(counts.values()) == {int(allowed)}, counts
        if not allowed:
            for delivery in ("degraded", "blocked", "failed"):
                result.delivery_status = delivery
                assert not await persist_research(result, claim, f"offline-remediation-{delivery}", memory=memory)
                assert result.persisted is False
            assert all(memory.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
                       for table in counts)
    finally:
        memory.close()


@pytest.mark.parametrize("claim", [
    "2026-10-14 全市场完整消息面已确认", "2026-10-14 全部消息已确认",
    "2026-10-14 所有消息已确认", "2026-10-14 完整消息面已确认",
    "2026-10-14 消息排名已确认", "2026-10-14 消息导致市场上涨",
])
@pytest.mark.parametrize("result_partial", [False, True])
async def test_single_news_never_proves_broad_ranking_or_causal_claim(tmp_path, claim, result_partial):
    from app.models.evidence import Evidence

    item = Evidence(id="news-001", domain="media", source_tool="internal_iwencai_news_search",
                    metric="news_1", value={"title": "盘面报道", "summary": "报道有限盘面事件"},
                    source="同花顺问财", authority="同花顺问财", retrieved_at="2026-10-14T01:00:00Z",
                    as_of_date="2026-10-14", completeness="search_results", coverage="search_results",
                    partial=False, result_partial=result_partial)
    await _assert_claim_boundaries(tmp_path, item, claim, allowed=False)


async def test_single_news_narrow_dated_event_passes_all_boundaries(tmp_path):
    item = _news_evidence()
    await _assert_claim_boundaries(tmp_path, item, "宁德时代在2026-10-14发布了三季度业绩预告", allowed=True)


@pytest.mark.parametrize("marker", [
    {}, {"partial": True}, {"status": "partial"}, {"completeness": "partial"},
    {"completeness": "incomplete"}, {"completeness": "truncated"},
])
@pytest.mark.parametrize("nested", [False, True])
async def test_explicit_row_incompleteness_survives_to_persistence(tmp_path, marker, nested):
    row = {"symbol": "SH600519", "date": "2026-10-14", **marker}
    row.update({"quote": {"price": 100.0}} if nested else {"price": 100.0})
    result = _normalize("get_market_quotes", {"symbols": ["SH600519"]}, {
        "source": "tencent", "partial": True, "items": [row],
    })
    price = next(datum for datum in result.normalized if datum.metric.endswith(".price"))
    assert price.partial is bool(marker)
    assert price.status == ("partial" if marker else "success")
    assert price.completeness == ("partial" if marker else "complete")
    item = next(item for item in build_evidence([result]) if item.metric.endswith(".price"))
    assert item.result_partial is True and item.partial is bool(marker)
    await _assert_claim_boundaries(tmp_path, item, "600519在2026-10-14价格为100元", allowed=not marker)


@pytest.mark.parametrize("rows, expected", [
    ([{"板块": "芯片"}], False), ([{"板块": "芯片", "count": None}], False),
    ([{"板块": "芯片", "rank": "强"}], False), ([{"板块": "芯片", "pct": "NaN"}], False),
    ([{"板块": "芯片"}, {"count": 3}], False), ([{"板块": "", "count": 3}], False),
    ([{"板块": "芯片", "count": True}], False),
    ([{"板块": "芯片", "count": 0}], True), ([{"theme": "chips", "rank": 1}], True),
    ([{"板块": "芯片", "pct": "12.5%"}], True),
])
def test_sector_fallback_requires_identity_and_numeric_structure_on_one_row(rows, expected):
    primary = _normalize("list_limit_up_sectors", {}, None, error="timeout")
    fallback = _normalize("list_limit_up_sectors", {}, {
        "source": "ashare-master", "count": 42,
        "items": [{**row, "date": "2026-10-14"} for row in rows],
    })
    assert fallback_satisfies_provenance(primary, fallback, "limit_up_sectors", {}) is expected


@pytest.mark.parametrize("change", [
    {"source": None}, {"authority": None}, {"as_of_date": None, "timestamp": None},
    {"as_of_date": "2026-10-13"},
])
def test_sector_required_fields_do_not_replace_provenance(change):
    primary = _normalize("list_limit_up_sectors", {}, None, error="timeout")
    fallback = _normalize("list_limit_up_sectors", {}, {
        "source": "ashare-master", "items": [{"sector": "chips", "rank": 1, "date": "2026-10-14"}],
    })
    for datum in fallback.normalized:
        for key, value in change.items():
            setattr(datum, key, value)
    if "as_of_date" in change:
        fallback.as_of_date = change["as_of_date"]
    assert not fallback_satisfies_provenance(primary, fallback, "limit_up_sectors", {"date": "2026-10-14"})


async def test_invalid_sector_fallback_retains_primary_narrow_fact_and_diagnosis():
    primary = _normalize("list_limit_up_sectors", {}, {
        "source": "ashare-master", "date": "2026-10-14", "items": [{"板块": "芯片"}],
    })
    fallback = primary.model_copy(deep=True)
    router = ControlledFallbackRouter()

    async def get_primary():
        return primary

    async def get_fallback():
        return fallback

    result = await router.call(run_id="offline-sector-rejection", capability="limit_up_sectors", arguments={},
                               primary_source="ths_skill", primary_transport="http_api", primary=get_primary,
                               fallback_source="mcp", fallback=get_fallback)
    assert result is primary and result.normalized
    assert result.source_switch is False
    assert {"missing_required_fields:limit_up_sectors", "fallback_provenance_insufficient"} <= set(result.reason_codes)
    assert len(router.observations) == 2


def test_partial_container_does_not_taint_independent_complete_row_or_invent_scope():
    result = _normalize("get_market_quotes", {"symbols": ["SH600519"], "date": "2026-10-14"}, {
        "source": "tencent", "partial": True, "data": {"partial": True, "items": [
            {"symbol": "SH600519", "date": "2026-10-14", "price": 100},
            {"price": 99},
        ]},
    })
    prices = [d for d in result.normalized if d.metric.endswith(".price")]
    assert prices[0].partial is False and prices[0].as_of_date == "2026-10-14"
    assert prices[1].partial is True and prices[1].as_of_date is None and prices[1].instrument is None


async def test_three_variants_never_label_executed_baseline_budget_truncated():
    baseline = _normalize("internal_iwencai_news_search", {"query": "query-a", "size": 10}, {
        "source": "同花顺问财", "items": [{"title": "已有基线新闻", "date": "2026-10-14"}],
    })
    baseline.tool_key = "news_search"
    node = _supervisor_node('{"intent":{"domain":"a_share","task":"company_research"},"steps":[]}', gap_max_steps=1)
    state = {"question": "600519新闻", "domain": "a_share", "results": [baseline],
             "critique": {"verdict": "research_more", "missing_tool_keys": ["news_search"], "issues": [
                 {"action": "research_more", "required_tool_keys": ["news_search"],
                  "required_coverage": {"arguments": {"query": query, "size": 10}}}
                 for query in ("query-a", "query-b", "query-c")
             ]}}
    out = await node(state)
    assert out.get("errors", []) == []
    routed = {semantic_signature(call["tool_key"], call["arguments"])
              for assignment in out["route"] for call in assignment["tool_calls"]}
    decisions = out["gap_key_decisions"]
    assert not any(row.get("reason") == "budget_truncated" and row.get("arguments", {}).get("query") == "query-a"
                   for row in decisions)
    for row in decisions:
        if row["decision"] == "kept":
            assert row["signature"] in routed
        elif row["reason"] == "budget_truncated":
            assert row["signature"] not in routed
    assert len([sig for sig in routed if sig.startswith("news_search:")]) == 1


def test_route_and_gap_signatures_share_one_canonicalization():
    """保护：route 与记账用的是同一套 semantic_signature（含 canonicalize）。"""
    raw = {"symbols": ["000300"]}
    canonical = canonicalize_tool_arguments("get_market_quotes", raw)
    assert canonical["symbols"] == ["SH000300"]
    assert semantic_signature("quote", canonical) == semantic_signature(
        "quote", canonicalize_tool_arguments("get_market_quotes", {"symbol": "000300"})
    )
    assert re.match(r"^quote:", semantic_signature("quote", canonical))
