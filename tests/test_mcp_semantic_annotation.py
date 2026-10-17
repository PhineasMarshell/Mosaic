"""MCP 语义补标注离线回归（执行单 mcp-semantic-annotation-execution-plan §7）。

覆盖 get_limit_up_count / list_limit_up_sectors 的语义适配器、normalizer、
fallback provenance、evidence ledger、strict claim audit 与跨日缓存。

全部用本地 dict，不连接真实网络 / THS / MCP / live acceptance，不写 memory.db。
"""

from __future__ import annotations

import asyncio
import copy

import pytest

from app.agent.evidence_gate import match_claims_to_evidence, run_evidence_gate
from app.cache import _make_cache_key, market_cache
from app.gateway.controlled_fallback import (
    ControlledFallbackRouter,
    fallback_provenance_failures,
    fallback_satisfies_provenance,
)
from app.gateway.normalizer import normalize_tool_result
from app.gateway.tool_registry import _MCP_SEMANTIC_CONTRACTS
from app.research.evidence import build_evidence

COUNT = "get_limit_up_count"
SECTORS = "list_limit_up_sectors"
D = "2026-10-15"
D2 = "2026-10-14"
PROVIDER = "ashare-master"


# ------------------------------------------------------------------ #
# 辅助函数                                                            #
# ------------------------------------------------------------------ #


def _norm(tool: str, raw, *, arguments=None, **kwargs):
    return normalize_tool_result(tool, arguments or {}, raw, **kwargs)


def _business(result):
    return [d for d in result.normalized if not d.structural_metadata]


def _structural(result):
    return [d for d in result.normalized if d.structural_metadata]


def _error_result(tool: str = SECTORS):
    return _norm(tool, None, error="timeout")


async def _await(value):
    return value


def _run_router(primary, fallback, capability: str, arguments=None):
    return asyncio.run(
        ControlledFallbackRouter().call(
            run_id="semantic-fixture",
            capability=capability,
            arguments=arguments or {},
            primary_source="ths_skill",
            primary_transport="http_api",
            primary=lambda: _await(primary),
            fallback_source="mcp",
            fallback_transport="mcp",
            fallback=lambda: _await(fallback),
        )
    )


# ── §7.1 顶层 count 归一为一条 limit_up_count 业务 datum ─────────────────────


def test_7_1_top_level_count_normalizes_to_single_limit_up_count():
    result = _norm(COUNT, {"source_used": PROVIDER, "count": 59, "date": D})
    business = _business(result)
    assert len(business) == 1
    datum = business[0]
    assert datum.metric == "limit_up_count"
    assert datum.value == 59
    assert datum.as_of_date == D
    assert datum.source == datum.authority == PROVIDER
    assert datum.semantic_annotation == "contract"
    assert datum.date_annotation == "observed"
    assert datum.annotation_contract_id == "mcp.get_limit_up_count.v1"
    assert datum.original_field_path == "count"
    assert result.status == "success" and result.evidence_created is True
    assert result.business_datum_count == 1
    # data.count 嵌套信封同样归一。
    nested = _norm(COUNT, {"data": {"count": "59"}, "source": PROVIDER, "date": D})
    assert _business(nested)[0].metric == "limit_up_count"
    assert _business(nested)[0].original_field_path == "data.count"


def test_7_1_equal_count_aliases_still_produce_one_business_datum():
    result = _norm(
        COUNT,
        {"source_used": PROVIDER, "count": 59, "data": {"count": 59}, "date": D},
    )
    business = _business(result)
    assert len(business) == 1
    assert business[0].metric == "limit_up_count"
    assert business[0].original_field_path == "data.count"
    assert result.business_datum_count == 1
    assert result.evidence_created is True


# ── §7.2 契约允许请求日期时补 request_context；§7.17 实际不允许时不得补 ──────


def test_7_2_request_context_branch_when_contract_allows_it():
    contract = _MCP_SEMANTIC_CONTRACTS[COUNT]
    original = contract["date_from_request"]
    try:
        # 模拟一个真正支持日期参数（且日期随 wire 发送）的契约。
        contract["date_from_request"] = True
        result = _norm(
            COUNT,
            {"source_used": PROVIDER, "count": 59},
            arguments={"date": D},
            requested_as_of_date=D,
        )
        datum = _business(result)[0]
        assert datum.date_annotation == "request_context"
        assert datum.as_of_date == D
        assert result.evidence_created is True
    finally:
        contract["date_from_request"] = original


def test_7_17_planned_date_is_not_used_when_no_wire_date_param():
    # 真实契约 date_from_request=False：仅有 planned_as_of_date 不得补观察日。
    result = _norm(
        COUNT,
        {"source_used": PROVIDER, "count": 59},
        arguments={"date": D},
        requested_as_of_date=D,
    )
    assert result.status == "partial" and result.evidence_created is False
    assert _business(result)[0].date_annotation == "unknown"
    assert _business(result)[0].as_of_date is None
    assert "mcp_request_date_ignored" in result.semantic_diagnostics
    assert "mcp_missing_date" in result.semantic_diagnostics


# ── §7.3 无请求日、无 payload 日期时返回 fallback_missing_date ─────────────


def test_7_3_missing_date_returns_fallback_missing_date():
    primary = _error_result(COUNT)
    fallback = _norm(COUNT, {"source_used": PROVIDER, "count": 59})
    codes = fallback_provenance_failures(primary, fallback, "limit_up_count", {})
    assert "fallback_missing_date" in codes
    assert fallback_satisfies_provenance(primary, fallback, "limit_up_count", {}) is False
    assert fallback.evidence_created is False


# ── §7.4 其他工具的顶层 count 保持结构；未知工具孤立 count 不映射 ───────────


def test_7_4_other_tools_do_not_map_top_level_count_to_limit_up():
    # 未知工具：孤立 count 不被解释成涨停家数。
    unknown = _norm("some_unrelated_tool", {"count": 5, "source": "x"})
    assert all(d.metric != "limit_up_count" for d in unknown.normalized)
    # 板块工具的顶层 count/code_count 是结构元数据，不是业务行。
    sectors = _norm(
        SECTORS,
        {"source": PROVIDER, "count": 2030, "date": D2,
         "rows": [{"板块": "机器人", "涨停家数": 8}]},
    )
    top_counts = [d for d in _structural(sectors) if d.metric in {"count", "code_count"}]
    assert top_counts and all(d.structural_metadata for d in top_counts)
    assert all(d.metric != "limit_up_count" for d in _structural(sectors))


# ── §7.5 null / 空串 / 非数字 count 不生成 evidence ─────────────────────────


@pytest.mark.parametrize("bad", [None, "", "abc", "未知"])
def test_7_5_invalid_count_values_generate_no_evidence(bad):
    result = _norm(COUNT, {"source_used": PROVIDER, "count": bad, "date": D})
    assert _business(result) == []
    assert result.evidence_created is False
    assert "mcp_invalid_count" in " ".join(result.semantic_diagnostics) or \
        "mcp_missing_business_value" in result.semantic_diagnostics


# ── §7.6 envelope 日期传播到每一行 ─────────────────────────────────────────


def test_7_6_envelope_date_propagates_to_every_row():
    result = _norm(
        SECTORS,
        {"source": PROVIDER, "date": D2,
         "rows": [
             {"板块": "机器人", "涨停家数": 12},
             {"板块": "固态电池", "涨停家数": 8},
         ]},
    )
    business = _business(result)
    assert business and all(d.as_of_date == D2 for d in business)
    assert all(d.date_annotation == "envelope" for d in business)
    assert all(d.source == d.authority == PROVIDER for d in business)
    lineage = result.date_annotation["row_date_lineage"]
    assert all(info["trusted"] and info["date_annotation"] == "envelope" for info in lineage.values())


# ── §7.7 行与信封都没有日期时拒绝 ──────────────────────────────────────────


def test_7_7_no_row_or_envelope_date_is_rejected():
    primary = _error_result(SECTORS)
    fallback = _norm(
        SECTORS,
        {"source": PROVIDER, "rows": [{"板块": "机器人", "涨停家数": 8}]},
    )
    codes = fallback_provenance_failures(primary, fallback, "limit_up_sectors", {})
    assert "fallback_missing_date" in codes
    assert fallback_satisfies_provenance(primary, fallback, "limit_up_sectors", {}) is False


# ── §7.8 只有板块名没有数值结构时缺必需字段 ────────────────────────────────


def test_7_8_identity_only_row_missing_structure():
    primary = _error_result(SECTORS)
    fallback = _norm(
        SECTORS,
        {"source": PROVIDER, "date": D2, "rows": [{"板块": "机器人"}]},
    )
    codes = fallback_provenance_failures(primary, fallback, "limit_up_sectors", {})
    assert "fallback_missing_sector_structure" in codes
    assert "fallback_missing_required_fields" in codes
    # 行被标记 partial。
    assert all(d.partial for d in _business(fallback))


# ── §7.9 一行 partial 不把其他完整行变成 partial ───────────────────────────


def test_7_9_one_partial_row_does_not_partialize_complete_rows():
    result = _norm(
        SECTORS,
        {"source": PROVIDER, "date": D2,
         "rows": [
             {"板块": "机器人", "涨停家数": 12},
             {"板块": "固态电池"},  # 缺结构
         ]},
    )
    complete = [d for d in _business(result) if "rows[0]" in d.metric]
    incomplete = [d for d in _business(result) if "rows[1]" in d.metric]
    assert complete and all(not d.partial for d in complete)
    assert incomplete and all(d.partial for d in incomplete)
    # 但整批仍因任一 partial 被拒绝（§6.8 / §7.22）。
    primary = _error_result(SECTORS)
    codes = fallback_provenance_failures(primary, result, "limit_up_sectors", {})
    assert "fallback_partial_datum" in codes


# ── §7.10 datas=[] 零业务；count=0 有效 ────────────────────────────────────


def test_7_10_empty_datas_zero_business_and_count_zero_is_valid():
    empty = _norm(SECTORS, {"code_count": 0, "datas": []})
    assert empty.status == "error" and empty.reason_codes == ["empty_normalized"]
    assert empty.business_datum_count == 0 and empty.evidence_created is False
    assert empty.normalized == []

    zero = _norm(COUNT, {"source_used": PROVIDER, "count": 0, "date": D})
    datum = _business(zero)[0]
    assert datum.value == 0 and datum.metric == "limit_up_count"
    assert zero.status == "success" and zero.evidence_created is True


# ── §7.11 root / metadata / result 三种 provider 都能产生 authority ─────────


@pytest.mark.parametrize("raw", [
    {"source": PROVIDER, "date": D2, "rows": [{"板块": "A", "涨停家数": 1}]},
    {"source_used": PROVIDER, "date": D2, "rows": [{"板块": "A", "涨停家数": 1}]},
    {"metadata": {"provider": PROVIDER}, "date": D2, "rows": [{"板块": "A", "涨停家数": 1}]},
    {"result": {"source_used": PROVIDER}, "date": D2, "rows": [{"板块": "A", "涨停家数": 1}]},
])
def test_7_11_provider_envelope_locations_produce_authority(raw):
    result = _norm(SECTORS, raw)
    assert result.authority == PROVIDER
    assert all(d.authority == PROVIDER for d in _business(result))


# ── §7.12 只有 transport 名时缺 authority；合法 declared provider 可补齐 ─────


def test_7_12_transport_only_missing_authority_but_declared_provider_fills():
    primary = _error_result(SECTORS)
    transport_only = _norm(
        SECTORS,
        {"transport": "mcp", "date": D2, "rows": [{"板块": "A", "涨停家数": 1}]},
    )
    assert transport_only.authority is None
    codes = fallback_provenance_failures(primary, transport_only, "limit_up_sectors", {})
    assert "fallback_missing_authority" in codes and "fallback_missing_source" in codes

    # 已核实的 declared provider 契约可补齐 authority。
    declared = _norm(
        SECTORS,
        {"date": D2, "rows": [{"板块": "A", "涨停家数": 1}]},
        declared_provider=PROVIDER,
    )
    assert declared.authority == PROVIDER
    assert fallback_satisfies_provenance(primary, declared, "limit_up_sectors", {}) is True


# ── §7.13 retrieved_at 存在但无观察日仍拒绝 ────────────────────────────────


def test_7_13_retrieved_at_never_stands_in_for_observation_date():
    result = _norm(COUNT, {"source_used": PROVIDER, "count": 59})
    assert result.retrieved_at  # 抓取时间确实存在
    assert result.as_of_date is None and _business(result)[0].as_of_date is None
    primary = _error_result(COUNT)
    assert "fallback_missing_date" in fallback_provenance_failures(
        primary, result, "limit_up_count", {}
    )


# ── §7.14 合格 fallback 被采纳：source_switch + datum_count>0 ──────────────


def test_7_14_good_fallback_adopted_with_source_switch():
    primary = _error_result(SECTORS)
    fallback = _norm(
        SECTORS,
        {"source": PROVIDER, "date": D2, "rows": [{"板块": "机器人", "涨停家数": 8}]},
    )
    result = _run_router(primary, fallback, "limit_up_sectors")
    assert result.source_switch is True
    assert len(_business(result)) > 0
    assert "fallback_provenance_insufficient" not in result.reason_codes


# ── §7.15 不合格 fallback 保留汇总码 + 逐项码 ──────────────────────────────


def test_7_15_bad_fallback_keeps_summary_and_itemized_codes():
    primary = _error_result(SECTORS)
    fallback = _norm(SECTORS, {"source": PROVIDER, "rows": [{"板块": "机器人"}]})
    result = _run_router(primary, fallback, "limit_up_sectors")
    # 不合格候选的业务数据不被采纳：保留零 datum 不变式。
    assert result.status == "error" and result.normalized == []
    assert "fallback_provenance_insufficient" in result.reason_codes
    assert "fallback_missing_date" in result.reason_codes
    assert "fallback_missing_sector_structure" in result.reason_codes
    # 失败的 fallback 候选本身也挂逐项码。
    assert "fallback_missing_date" in fallback.fallback_provenance_failures


# ── §7.16 strict audit 接受完整窄事实、拒绝 partial/无日期 ─────────────────


def test_7_16_strict_audit_accepts_narrow_fact_rejects_partial_undated():
    good = _norm(COUNT, {"source_used": PROVIDER, "count": 59, "date": D})
    evidence = build_evidence([good])
    assert len(evidence) == 1
    rows = match_claims_to_evidence(
        {"claims": [{"claim": "2026-10-15两市达到涨幅上限的个股数量为59只",
                     "evidence_ids": [evidence[0].id]}]},
        evidence, expected_date=D, requested_date=D, strict_provenance=True,
        expected_domain="a_share",
    )
    assert rows[0]["supported"] is True

    # 无日期/partial 不进 ledger。
    undated = _norm(COUNT, {"source_used": PROVIDER, "count": 59})
    assert build_evidence([undated]) == []
    lean = _norm(SECTORS, {"source": PROVIDER, "data": {"list": [{"plate": "小米汽车"}]}})
    assert build_evidence([lean]) == []
    gate = run_evidence_gate([lean], requested_date=D2)
    assert gate.has_evidence is False


# ── §7.18 行日≠请求日 / 非法日期 / 混合日期 / 历史撞快照 ───────────────────


def test_7_18_date_conflicts_and_invalid_dates_are_not_passed():
    # 行日期与请求日不同 → fallback_date_mismatch。
    primary = _error_result(SECTORS)
    stale = _norm(
        SECTORS,
        {"source": PROVIDER, "rows": [{"板块": "A", "涨停家数": 1, "date": "2026-10-10"}]},
    )
    codes = fallback_provenance_failures(primary, stale, "limit_up_sectors", {"date": D2})
    assert "fallback_date_mismatch" in codes

    # 非法日期：不静默换成请求日期。
    invalid = _norm(COUNT, {"source_used": PROVIDER, "count": 59, "date": "not-a-date"})
    assert "mcp_invalid_date" in invalid.semantic_diagnostics
    assert invalid.evidence_created is False

    # 混合行日期：硬冲突，整批拒绝。
    mixed = _norm(
        SECTORS,
        {"source": PROVIDER, "rows": [
            {"板块": "A", "涨停家数": 1, "date": "2026-10-14"},
            {"板块": "B", "涨停家数": 2, "date": "2026-10-13"},
        ]},
    )
    assert "mcp_date_conflict" in mixed.semantic_diagnostics
    assert mixed.status == "partial" and mixed.evidence_created is False
    assert fallback_satisfies_provenance(primary, mixed, "limit_up_sectors", {}) is False

    # 历史请求撞"最新快照"：真实契约不用请求日补日期。
    history = _norm(
        COUNT,
        {"source_used": PROVIDER, "count": 59},
        arguments={"date": "2026-01-01"},
        requested_as_of_date="2026-01-01",
    )
    assert history.evidence_created is False
    assert "mcp_request_date_ignored" in history.semantic_diagnostics


# ── §7.19 输入不可变 + 幂等 + 标注元数据不变成证据 ─────────────────────────


def test_7_19_input_immutability_idempotency_and_no_annotation_evidence():
    raw = {"source": PROVIDER, "date": D2, "rows": [{"板块": "机器人", "涨停家数": 8}]}
    snapshot = copy.deepcopy(raw)
    first = _norm(SECTORS, raw)
    second = _norm(SECTORS, raw)
    assert raw == snapshot  # 原始 dict 与嵌套行未被修改
    assert [d.metric for d in first.normalized] == [d.metric for d in second.normalized]
    # 语义标注/sidecar 没有被扁平化成业务 datum。
    assert all(d.metric not in {"date_annotation", "semantic_annotation", "contract_id"}
               for d in first.normalized)
    # 结构/计数诊断不进 ledger。
    structural_only = _norm(SECTORS, {"source": PROVIDER, "code_count": 421, "date": D2})
    assert build_evidence([structural_only]) == []


# ── §7.20 数值 0 / 负 / 小数 / bool / NaN / 别名冲突 ───────────────────────


@pytest.mark.parametrize("value,valid", [
    (0, True),
    ("0", True),
    (59, True),
    ("59", True),
    (-1, False),
    (1.5, False),
    (True, False),
    ("NaN", False),
    (float("nan"), False),
])
def test_7_20_count_value_validation(value, valid):
    result = _norm(COUNT, {"source_used": PROVIDER, "count": value, "date": D})
    if valid:
        assert result.evidence_created is True
        assert _business(result)[0].value == int(value)
    else:
        assert result.evidence_created is False
        assert _business(result) == []


def test_7_20_count_alias_conflict_is_an_error():
    result = _norm(
        COUNT,
        {"source_used": PROVIDER, "count": 59, "data": {"count": 60}, "date": D},
    )
    assert "mcp_count_alias_conflict" in result.semantic_diagnostics
    assert _business(result) == []


# ── §7.21 envelope 日期结果经过 normalizer→fallback→ledger→strict 全链路 ─────


def test_7_21_envelope_sectors_pass_full_chain():
    primary = _error_result(SECTORS)
    fallback = _norm(
        SECTORS,
        {"source": PROVIDER, "date": D2,
         "rows": [{"板块": "汽车零部件", "涨停家数": 12}]},
    )
    routed = _run_router(primary, fallback, "limit_up_sectors")
    assert routed.source_switch is True
    evidence = build_evidence([routed])
    count = next(e for e in evidence if "涨停家数" in e.metric)
    assert count.as_of_date == D2 and count.authority == PROVIDER
    rows = match_claims_to_evidence(
        {"claims": [{"claim": "2026-10-14汽车零部件板块触及涨幅上限的个股数量为12只",
                     "evidence_ids": [count.id]}]},
        [count], expected_date=D2, requested_date=D2, strict_provenance=True,
        expected_domain="a_share",
    )
    assert rows[0]["supported"] is True


# ── §7.22 完整/partial 混合不整批放行，逐项诊断保留 ─────────────────────────


def test_7_22_mixed_complete_partial_batch_not_adopted():
    primary = _error_result(SECTORS)
    fallback = _norm(
        SECTORS,
        {"source": PROVIDER, "date": D2,
         "rows": [
             {"板块": "机器人", "涨停家数": 12},
             {"板块": "固态电池"},
         ]},
    )
    result = _run_router(primary, fallback, "limit_up_sectors")
    # 即使有一条完整行，另一条 partial 也不整批放行：零 datum 不变式。
    assert result.status == "error" and result.normalized == []
    assert "fallback_provenance_insufficient" in result.reason_codes
    assert "fallback_partial_datum" in result.reason_codes
    # 逐项诊断挂在失败候选上。
    assert fallback.fallback_provenance_failures
    assert "fallback_partial_datum" in fallback.fallback_provenance_failures


# ── §7.23 跨日缓存命中保留原观察日，不重新贴日期 ───────────────────────────


def test_7_23_cross_day_cache_hit_preserves_observation_date():
    first = _norm(
        SECTORS,
        {"source": PROVIDER, "date": D2, "rows": [{"板块": "机器人", "涨停家数": 8}]},
    )
    key = _make_cache_key(SECTORS, {})
    market_cache.set(key, first.model_copy(deep=True), ttl=60)
    try:
        cached = market_cache.get(key)
        assert cached is not None
        assert cached.as_of_date == D2
        assert all(d.as_of_date == D2 for d in _business(cached))
        # 即使研究目标日变成第二天，缓存命中的仍是原观察日（不重贴）。
        assert cached.date_annotation["as_of_date"] == D2
        lineage = cached.date_annotation["row_date_lineage"]
        assert all(info["date"] == D2 for info in lineage.values())
    finally:
        market_cache.invalidate(key)
