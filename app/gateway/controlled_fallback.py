"""受控数据源路由与 fallback。

Fallback 在 primary 可重试错误、缺必需字段或缺来源时考虑，并要求备用结果具有可
审计 provenance 和必需字段。它不会覆盖权威冲突，也不会把 Skill 成功包装成 MCP
修复；调用方始终能看到 primary/fallback/source_switch/run_id。
"""

from __future__ import annotations

import re
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from math import isfinite
from typing import Any

from app.gateway.normalizer import _MARKET_CAPABILITIES as _MARKET_CAPABILITIES
from app.gateway.normalizer import is_market_capability, is_structural_metadata_datum
from app.models.market import STATUS_ERROR, ToolResult
from app.research.trading_calendar import normalize_date

_RETRYABLE_ERRORS = {
    "gateway_timeout",
    "gateway_upstream_error",
    "gateway_upstream_incomplete",
    "gateway_transport_error",
    "gateway_tls_error",
    "gateway_tool_unavailable",
    "deadline_exhausted",
    "empty_normalized",
    "empty_gateway_response",
    "unsupported",
    "skill_unsupported",
    "gateway_http_error",
    "gateway_invalid_response",
    # 上游 200 信封里的业务错误（例如 iwencai "gateway_business_error"）与
    # 超时/传输失败一样，都说明这次 primary 调用没有拿到数据，当然可以回退。
    "gateway_business_error",
}

#: 纯聚合型市场级能力：上游返回"全市场 N 项聚合值"，datum 按定义没有
#: instrument。quote / kline 是按标的取数，**不在**这里。
_AGGREGATE_MARKET_CAPABILITIES = frozenset(
    {
        "market_breadth",
        "sentiment",
        "limit_up",
        "limit_up_count",
        "limit_up_pool",
        "limit_up_sectors",
    }
)


def _code(value: Any) -> str:
    text = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    match = re.search(r"\d{6}", text)
    return match.group() if match else text


def _requested_instruments(arguments: dict[str, Any]) -> set[str]:
    raw = arguments.get("symbol") or arguments.get("symbols") or []
    values = raw if isinstance(raw, (list, tuple, set)) else [raw]
    return {_code(value) for value in values if _code(value)}


def _requested_date(arguments: dict[str, Any]) -> str | None:
    raw = arguments.get("as_of_date") or arguments.get("date") or arguments.get("end") or arguments.get("end_date")
    text = str(raw or "")[:10].replace("/", "-")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


@dataclass
class RouteObservation:
    run_id: str
    source: str
    transport: str
    capability: str
    result: ToolResult
    latency_ms: float
    error_type: str | None = None
    tls_or_upstream: str | None = None
    fallback_provenance_failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        result = self.result
        business = _business_datums(result)
        return {
            "run_id": self.run_id,
            "source": self.source,
            "transport": self.transport,
            "capability": self.capability,
            "status": result.status,
            "datum_count": len(result.normalized),
            "evidence_created": bool(result.normalized) and result.status != "error",
            "latency_ms": self.latency_ms,
            "error_type": self.error_type,
            "tls_or_upstream": self.tls_or_upstream,
            "reason_codes": result.reason_codes,
            "endpoint": result.endpoint,
            "skill_id": result.skill_id,
            "authority": sorted({str(d.authority) for d in business if d.authority}),
            "dates": sorted({str(d.as_of_date) for d in business if d.as_of_date}),
            "business_datum_count": len(business),
            "structural_metadata_count": len(result.normalized) - len(business),
            "missing_required_fields": _missing_required_field_groups(result, self.capability),
            "fallback_provenance_failures": list(self.fallback_provenance_failures),
        }


@dataclass
class CapabilityMetrics:
    capability: str
    source: str
    observations: list[RouteObservation] = field(default_factory=list)

    def add(self, observation: RouteObservation) -> None:
        self.observations.append(observation)

    def summary(self) -> dict[str, Any]:
        rows = self.observations
        latencies = sorted(row.latency_ms for row in rows)
        successes = sum(row.result.status == "success" and bool(row.result.normalized) for row in rows)
        errors = sum(row.result.status == "error" for row in rows)
        partial = sum(row.result.status == "partial" or row.result.partial for row in rows)
        dated = sum(bool(row.result.as_of_date or any(d.as_of_date for d in row.result.normalized)) for row in rows)
        instruments = sum(any(d.instrument for d in row.result.normalized) for row in rows)
        evidence = sum(bool(row.result.normalized) and row.result.status != "error" for row in rows)
        return {
            "capability": self.capability,
            "source": self.source,
            "calls": len(rows),
            "success_rate": successes / len(rows) if rows else 0.0,
            "error_rate": errors / len(rows) if rows else 0.0,
            "timeout_error_rate": sum("gateway_timeout" in row.result.reason_codes for row in rows) / len(rows) if rows else 0.0,
            "p50_latency_ms": _percentile(latencies, 0.50),
            "p95_latency_ms": _percentile(latencies, 0.95),
            "date_completeness": dated / len(rows) if rows else 0.0,
            "instrument_match_rate": instruments / len(rows) if rows else 0.0,
            "partial_rate": partial / len(rows) if rows else 0.0,
            "evidence_availability": evidence / len(rows) if rows else 0.0,
        }


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 1)
    return round(statistics.quantiles(values, n=100, method="inclusive")[max(0, int(fraction * 100) - 1)], 1)


def _datum_date(datum: Any, result: ToolResult, *, use_result_date: bool = False) -> str | None:
    """这条 datum 描述哪一天。

    ``use_result_date`` 只给**纯聚合型市场级**能力用：sentiment / limit_up_*
    这类响应把"描述哪一天"声明在结果级（``result.as_of_date``），datum 自己也
    没有标的，所以结果级日期确实适用于每一条。quote / kline 是逐标的取数，
    缺自己日期的 datum 不能被结果级日期"补"上。
    """
    own = normalize_date(datum.as_of_date) or normalize_date(datum.timestamp)
    if own:
        return own
    return normalize_date(result.as_of_date) if use_result_date else None


def _has_provenance(result: ToolResult, capability: str, arguments: dict[str, Any]) -> bool:
    if result.status == "error" or not result.normalized:
        return False
    expected_codes = _requested_instruments(arguments)
    market_level = is_market_capability(capability)
    expected_date = _requested_date(arguments) if market_level else None
    # 纯聚合型市场级 tool（sentiment / limit_up_* / market_breadth）
    # 的 datum 天然没有 instrument：上游返回的就是"全市场几项聚合值"，它不是
    # 不可归因。这类结果的"描述哪一天"声明在结果级（result.as_of_date），所以
    # 允许用结果级日期补齐 datum 自己的日期。quote / kline 是按标的逐条取数，
    # 每条 datum 必须自己有日期，绝不拿结果级日期顶替。
    # Sector/pool rows are individually attributable facts; a result-level
    # date cannot silently fill a missing row date. Pure aggregate counters may
    # use the response date because each datum has the same market scope.
    aggregate_market = capability in {"market_breadth", "sentiment", "limit_up", "limit_up_count"}
    datums = _business_datums(result)
    if not datums:
        return False
    if capability in {"limit_up_sectors", "limit_up_pool"} and not _rows_have_own_dates(result):
        return False
    # 行批快照能力：任一业务行自身 partial（缺身份/结构/日期）即整批拒绝；逐行
    # partial 分离只影响 datum 标记，不代表整批可自动放行（部分行采纳不在本单）。
    # 注意：结果级 result.partial（覆盖截断）仍允许每条自身完整的行作为窄事实。
    if capability in {"limit_up_sectors", "limit_up_pool"} and any(
        bool(getattr(datum, "partial", False)) for datum in datums
    ):
        return False
    for datum in datums:
        # 顶层信封元数据（count/total/row_count/page…）描述的是"这一批响应有几条"，
        # 不是业务事实，也没有自己的日期与 provider —— 不参与逐条 provenance 校验。
        # Strict provenance has two independent dimensions. ``source`` says
        # what the payload declared; ``authority`` says which recognized
        # provider can be held accountable. Either one alone is insufficient.
        if not datum.source or not datum.authority or not (datum.retrieved_at or result.retrieved_at):
            return False
        observed_date = _datum_date(datum, result, use_result_date=aggregate_market)
        if market_level and not observed_date:
            return False
        if capability in {"quote", "kline"} and not datum.instrument:
            # 按标的取数的端点：datum 必须自己说明标的，否则这条无法归因。
            return False
        if expected_date and observed_date != expected_date:
            return False
        if expected_codes and (not datum.instrument or _code(datum.instrument) not in expected_codes):
            return False
        coverage = str(getattr(datum, "coverage", "") or "")
        scope = str(getattr(datum, "instrument_scope", "") or "")
        # 只有"既没点名标的、又没有覆盖面声明、也没有自己的标的、说明白的是市场
        # 口径"的 datum 才算不可归因。聚合型市场端点（sentiment / limit_up_*…）
        # 天然就是全市场口径，缺 coverage 元数据不该单独否决整批证据；quote /
        # kline 这类按标的取的端点仍要求每条 datum 自己站得住。
        unattributable = not expected_codes and coverage == "unknown" and scope == "unknown"
        if unattributable and not datum.instrument and not aggregate_market and capability != "limit_up_sectors":
            return False
    return True


# ------------------------------------------------------------------ #
# primary 必需字段触发：partial/success 也可能"缺了必须有的东西"          #
# ------------------------------------------------------------------ #

#: 每个市场级能力必须拿到的最小字段集。用语义分组而不是固定字符串：上游换
#: 指标名（涨跌家数 / 上涨家数 / 上涨数）不应该让必需字段判定失效。
_UP_COUNT_FIELDS = frozenset(
    {"up_count", "up", "advance", "advancers", "rising", "rise_count", "up_num", "上涨家数", "上涨数", "上涨", "涨家数"}
)
_DOWN_COUNT_FIELDS = frozenset(
    {"down_count", "down", "decline", "decliners", "falling", "fall_count", "down_num", "下跌家数", "下跌数", "下跌", "跌家数"}
)
_LIMIT_UP_COUNT_FIELDS = frozenset(
    {"count", "limit_up_count", "limit_up", "limitup_count", "limit_up_num", "涨停家数", "涨停数", "涨停", "涨停股数"}
)
_LIMIT_UP_SECTOR_FIELDS = frozenset(
    {
        "limit_up_sector",
        "limit_up_sectors",
        "limitup_sector",
        "limit_up_industry",
        "limit_up_theme",
        "limit_up_concept",
        "涨停板块",
        "涨停题材",
        "涨停行业",
        "涨停概念",
        "涨停板块数",
        # 上游按"行"返回板块时，行的判别字段往往是家数/板块名本身，而不是
        # "板块"这个词（`list_limit_up_sectors` 的 rows[0].涨停家数）。把这一层
        # 也纳入，避免同一能力的两种返回形态被判成缺字段。
        "涨停家数",
        "涨停数",
        "板块",
        "题材",
        "行业",
        "概念",
    }
)
_LIMIT_UP_ROW_FIELDS = frozenset(
    {
        "limit_up_pool",
        "limit_up_stock",
        "limit_up_stocks",
        "limit_up_pool_count",
        "涨停池",
        "涨停股票",
        "涨停个股",
    }
)
_NEWS_EVENT_FIELDS = frozenset({"news", "news_count", "telegraph", "announcement", "公告", "快讯", "新闻", "title", "headline"})
_SECTOR_IDENTITY_FIELDS = frozenset({
    "plate", "plate_name", "sector", "sector_name", "theme", "theme_name",
    "industry", "industry_name", "concept", "concept_name", "name",
    "板块", "题材", "行业", "概念", "行业板块", "概念板块", "题材板块", "名称",
    "limit_up_sector", "limit_up_sectors", "limit_up_theme", "limit_up_industry",
    "涨停板块", "涨停题材", "涨停行业", "涨停概念",
})
_SECTOR_STRUCTURE_FIELDS = _LIMIT_UP_COUNT_FIELDS | frozenset({
    "rank", "ranking", "pct", "pct_change", "percentage", "change_pct", "ratio",
    "排名", "涨幅", "涨跌幅", "占比", "家数",
})
_FALLBACK_CAPABILITY_ALIASES = {"limit_up": "limit_up_pool"}
_QUOTE_VALUE_FIELDS = frozenset({
    "price", "last", "close", "latest", "latest_price", "current_price",
    "last_price", "lastprice", "change", "change_pct", "pct_change", "changepercent",
    "latest_change_pct", "pct_chg", "最新涨跌幅", "涨跌幅", "涨跌额", "最新价",
    "最新价格", "收盘价", "现价", "点位", "c",
})


def _metric_leaf(metric: str) -> str:
    # THS columns can embed the observation date and adjustment label, such as
    # ``收盘价[20261014]:前复权``.  Those decorations do not change the metric.
    leaf = metric.rsplit(".", 1)[-1].casefold()
    return re.sub(r"\[[^\]]*\]|[:：].*$", "", leaf).strip()


def _is_numeric_value(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return False
    try:
        return isfinite(float(str(value).strip().replace(",", "").removesuffix("%")))
    except ValueError:
        return False


def _required_field_groups(capability: str) -> tuple[frozenset[str], ...]:
    """Required metric-name groups for a capability (empty tuple = no field rule)."""
    resolved = _FALLBACK_CAPABILITY_ALIASES.get(capability, capability)
    if resolved in {"quote", "kline", "abnormal_reasons", "sector_theme"}:
        return ()
    if resolved == "market_breadth" or resolved == "sentiment":
        return (_UP_COUNT_FIELDS, _DOWN_COUNT_FIELDS)
    if resolved == "limit_up_count":
        return (_LIMIT_UP_COUNT_FIELDS,)
    if resolved == "limit_up_sectors":
        return (_LIMIT_UP_SECTOR_FIELDS,)
    if resolved == "limit_up_pool":
        return (_LIMIT_UP_ROW_FIELDS,)
    if resolved == "telegraph":
        return (_NEWS_EVENT_FIELDS,)
    return ()


def _metric_names(result: ToolResult) -> set[str]:
    names: set[str] = set()
    for datum in result.normalized:
        if is_structural_metadata_datum(datum) or datum.value in (None, "", [], {}):
            continue
        metric = str(getattr(datum, "metric", "") or "")
        if not metric:
            continue
        lowered = metric.casefold()
        names.add(lowered)
        names.add(_metric_leaf(lowered))
    return names


def _missing_required_field_groups(result: ToolResult, capability: str) -> list[str]:
    groups = _required_field_groups(capability)
    if not groups:
        return []
    names = _metric_names(result)
    return [sorted(group)[0] for group in groups if not (names & group)]


def _business_datums(result: ToolResult) -> list[Any]:
    return [datum for datum in result.normalized if not is_structural_metadata_datum(datum)]


def _raw_rows_own_dates(result: ToolResult) -> bool:
    """原始兜底：逐条原始行必须自己带日期（不信任 payload 自标注）。

    适用于语义适配器未产出受信 lineage 的情况；它不允许 envelope 日期替代
    行级日期，也不因为找不到可识别列表就自动通过。
    """
    raw = result.raw if isinstance(result.raw, dict) else {}
    rows = next(
        (raw.get(key) for key in ("rows", "items", "datas", "data") if isinstance(raw.get(key), list)),
        None,
    )
    if not rows:
        # 找不到原始列表时不能自动通过；仍须 normalized 业务 datum 各自站得住。
        business = _business_datums(result)
        return bool(business) and all(_datum_date(datum, result) is not None for datum in business)
    date_keys = {
        "date", "trade_date", "trading_date", "as_of_date", "日期", "交易日期", "交易日", "timestamp", "datetime",
    }
    return all(
        isinstance(row, dict) and any(row.get(key) not in (None, "") for key in date_keys)
        for row in rows
    )


def _rows_have_own_dates(result: ToolResult) -> bool:
    """Sector/pool 行是否每条都能归因到一个观察日期。

    优先采信语义适配器生成的**受信逐行 lineage**（raw 不改写、payload 自标注
    无效）：行级日期（observed）或契约保证同一快照传播的信封日期（envelope）
    都算。没有受信 lineage 时回退到 ``_raw_rows_own_dates`` 的严格逐行检查。
    """
    sidecar = result.date_annotation if isinstance(getattr(result, "date_annotation", None), dict) else None
    if sidecar is not None and sidecar.get("payload_kind") == "limit_up_sectors":
        lineage = sidecar.get("row_date_lineage")
        if isinstance(lineage, dict) and lineage:
            # 每一条业务行都必须有受信日期；任一行为 unknown 即整批不通过。
            return all(
                isinstance(info, dict) and info.get("trusted") and info.get("date")
                for info in lineage.values()
            )
        # 适配器跑过但没有识别到任何业务行：不自动通过，交由 normalized 验证。
        business = _business_datums(result)
        if not business:
            return False
    return _raw_rows_own_dates(result)


def _has_required_fields(result: ToolResult, capability: str, arguments: dict[str, Any]) -> bool:
    """False when a non-error result is missing capability-critical fields.

    这里**不**校验 coverage/provenance：那是 fallback 候选能否被采纳的门槛
    （:func:`fallback_allowed`）。primary 能不能自立看的是"必需字段在不在"。
    """
    if result.status == "error" or not result.normalized:
        return False
    datums = _business_datums(result)
    if not datums:
        return False
    resolved = _FALLBACK_CAPABILITY_ALIASES.get(capability, capability)
    if is_market_capability(capability):
        aggregate = capability in _AGGREGATE_MARKET_CAPABILITIES
        if any(_datum_date(datum, result, use_result_date=aggregate) is None for datum in datums):
            return False
    if resolved in {"quote", "kline"}:
        expected_codes = _requested_instruments(arguments)
        candidates: dict[str, bool] = {code: False for code in expected_codes}
        for datum in datums:
            metric = str(getattr(datum, "metric", "") or "")
            if _metric_leaf(metric) not in _QUOTE_VALUE_FIELDS:
                continue
            if not _is_numeric_value(datum.value) or datum.status == STATUS_ERROR:
                continue
            if _datum_date(datum, result) is None:
                continue
            code = _code(getattr(datum, "instrument", None))
            if expected_codes:
                if code in candidates:
                    candidates[code] = True
            elif code:
                candidates[code] = True
        return all(candidates.values()) if candidates else False
    if resolved in {"sentiment", "market_breadth", "limit_up_count"}:
        numeric_fields = {
            _metric_leaf(datum.metric) for datum in datums
            if _is_numeric_value(datum.value) and datum.status != STATUS_ERROR
        }
        return all(numeric_fields & group for group in _required_field_groups(resolved))
    if resolved == "limit_up_sectors":
        rows: dict[str, set[str]] = {}
        for datum in datums:
            if datum.status != "success" or datum.partial or datum.completeness in {"partial", "incomplete", "truncated"}:
                continue
            # Same returned row, never a global union of unrelated fields.
            row, _, leaf = datum.metric.rpartition(".")
            leaf = _metric_leaf(leaf)
            flags = rows.setdefault(row, set())
            if leaf in _SECTOR_IDENTITY_FIELDS and isinstance(datum.value, str) and datum.value.strip():
                flags.add("identity")
            if leaf in _SECTOR_STRUCTURE_FIELDS and _is_numeric_value(datum.value):
                flags.add("structure")
        return any(flags == {"identity", "structure"} for flags in rows.values())
    if _missing_required_field_groups(result, resolved):
        return False
    return True


def fallback_provenance_failures(
    primary: ToolResult,
    fallback: ToolResult,
    capability: str,
    arguments: dict[str, Any],
) -> list[str]:
    """Return stable, per-gate diagnostics for a rejected fallback.

    The summary code ``fallback_provenance_insufficient`` remains on the result
    for compatibility.  These codes make the rejection actionable without
    retaining the upstream payload in logs.
    """
    failures: list[str] = []
    business = _business_datums(fallback)
    if not business:
        raw = fallback.raw if isinstance(fallback.raw, dict) else {}
        has_structural = bool(fallback.normalized) or any(
            key in raw for key in ("count", "code_count", "total", "row_count", "item_count")
        )
        if has_structural:
            failures.append("fallback_structural_metadata_only")
        else:
            failures.append("fallback_missing_required_fields")
        return failures
    if any(not str(getattr(datum, "source", "") or "").strip() for datum in business):
        failures.append("fallback_missing_source")
    if any(not str(getattr(datum, "authority", "") or "").strip() for datum in business):
        failures.append("fallback_missing_authority")
    if any(not (getattr(datum, "retrieved_at", None) or fallback.retrieved_at) for datum in business):
        failures.append("fallback_missing_retrieved_at")

    market_level = is_market_capability(capability)
    aggregate = capability in {"market_breadth", "sentiment", "limit_up", "limit_up_count"}
    observed_dates = {
        _datum_date(datum, fallback, use_result_date=aggregate)
        for datum in business
    }
    observed_dates.discard(None)
    if market_level and any(_datum_date(datum, fallback, use_result_date=aggregate) is None for datum in business):
        failures.append("fallback_missing_date")
    if capability in {"limit_up_sectors", "limit_up_pool"} and not _rows_have_own_dates(fallback):
        failures.append("fallback_missing_date")
    expected_date = _requested_date(arguments) if market_level else None
    if expected_date and observed_dates and any(value != expected_date for value in observed_dates):
        failures.append("fallback_date_mismatch")

    requested_codes = _requested_instruments(arguments)
    if requested_codes:
        returned_codes = {_code(getattr(datum, "instrument", None)) for datum in business}
        returned_codes.discard("")
        if not returned_codes or not requested_codes.issubset(returned_codes):
            failures.append("fallback_instrument_mismatch")
    if any(bool(getattr(datum, "partial", False)) for datum in business):
        failures.append("fallback_partial_datum")
    # 拆分 limit_up_sectors 的缺身份/缺数值：旧实现只给一个汇总码，无法定位是
    # 缺板块身份、缺数值结构还是缺日期。这里给出真实缺口，保留汇总码兼容。
    if capability == "limit_up_sectors":
        gaps = _sector_field_gaps(fallback)
        if "identity" in gaps:
            failures.append("fallback_missing_sector_identity")
        if "structure" in gaps:
            failures.append("fallback_missing_sector_structure")
    if not _has_required_fields(fallback, capability, arguments):
        failures.append("fallback_missing_required_fields")
    return list(dict.fromkeys(failures))


def _sector_field_gaps(result: ToolResult) -> set[str]:
    """对 limit_up_sectors 报告整批缺身份/缺数值（至少一条完整行才算具备）。

    逐行分组，与 :func:`_has_required_fields` 同一口径；只报告"整批没有任何一条
    行满足"的那一类，避免把个别不完整行误报为整批缺失。
    """
    datums = _business_datums(result)
    has_identity = False
    has_structure = False
    rows: dict[str, set[str]] = {}
    for datum in datums:
        # 按字段实际存在性统计维度缺口：partial 行（例如缺日期）仍能提供身份/结构
        # 信息，不能因行 partial 就把它实际存在的 plate 误判成整批缺身份。
        # error datum 才完全不可信。
        if datum.status == "error":
            continue
        row, _, leaf = datum.metric.rpartition(".")
        leaf = _metric_leaf(leaf)
        flags = rows.setdefault(row, set())
        if leaf in _SECTOR_IDENTITY_FIELDS and isinstance(datum.value, str) and datum.value.strip():
            flags.add("identity")
        if leaf in _SECTOR_STRUCTURE_FIELDS and _is_numeric_value(datum.value):
            flags.add("structure")
    for flags in rows.values():
        if "identity" in flags:
            has_identity = True
        if "structure" in flags:
            has_structure = True
    gaps: set[str] = set()
    if not has_identity:
        gaps.add("identity")
    if not has_structure:
        gaps.add("structure")
    return gaps


def _has_primary_provenance(result: ToolResult, capability: str, arguments: dict[str, Any]) -> bool:
    """The primary's business datum must also carry a source it can be attributed to."""
    datums = _business_datums(result)
    if not datums:
        return False
    for datum in datums:
        if not datum.source or not datum.authority:
            return False
    return True


def primary_trigger_reason(primary: ToolResult, capability: str, arguments: dict[str, Any]) -> str | None:
    """Return a stable trigger code when the primary cannot stand on its own.

    - auth/conflict 永不回退（由调用方与 :func:`fallback_allowed` 双重保障）；
    - error primary：可回退 reason code 才触发，其余（如 auth）返回 None；
    - 健康/partial primary：缺能力必需字段（sentiment 缺涨跌家数、
      limit_up_count 缺涨停家数、quote 缺日期或目标标的）时触发；字段齐全时
      partial 也**不**触发——"整批 partial 但每条事实完整"不是回退理由。
    """
    codes = set(primary.reason_codes)
    if codes & {"conflicting_evidence", "authority_conflict", "gateway_auth_error"}:
        return None
    if primary.status == STATUS_ERROR:
        if not codes.intersection(_RETRYABLE_ERRORS):
            return None
        return primary.reason_codes[0] if primary.reason_codes else "primary_error"
    if not _has_required_fields(primary, capability, arguments):
        return f"missing_required_fields:{_FALLBACK_CAPABILITY_ALIASES.get(capability, capability)}"
    if not _has_primary_provenance(primary, capability, arguments):
        return "missing_primary_provenance"
    return None


def needs_fallback(primary: ToolResult, capability: str, arguments: dict[str, Any]) -> bool:
    """True only when a fallback should actually be probed for this primary."""
    return primary_trigger_reason(primary, capability, arguments) is not None


def fallback_satisfies_provenance(
    primary: ToolResult, fallback: ToolResult, capability: str, arguments: dict[str, Any]
) -> bool:
    """Is this fallback result acceptable as a replacement for the primary?

    Same contract as :func:`fallback_allowed` minus the "primary must be a
    retryable transport error" precondition: a partial/success primary that is
    missing capability-critical fields may also be replaced, but never by a
    fallback that cannot prove its own provenance, and never on an
    auth/conflict diagnosis.
    """
    codes = set(primary.reason_codes)
    if "conflicting_evidence" in codes or "authority_conflict" in codes:
        return False
    if "gateway_auth_error" in codes:
        return False
    return _has_required_fields(fallback, capability, arguments) and _has_provenance(
        fallback, capability, arguments
    )


def fallback_allowed(primary: ToolResult, fallback: ToolResult, capability: str, arguments: dict[str, Any]) -> bool:
    """Return true only for an explicit primary transport failure + provenance."""
    if primary.status != "error":
        return False
    if "conflicting_evidence" in primary.reason_codes or "authority_conflict" in primary.reason_codes:
        return False
    if not set(primary.reason_codes).intersection(_RETRYABLE_ERRORS):
        return False
    return fallback_satisfies_provenance(primary, fallback, capability, arguments)


class ControlledFallbackRouter:
    """Route one capability while retaining both sides of the decision."""

    def __init__(self):
        self.observations: list[RouteObservation] = []

    async def call(
        self,
        *,
        run_id: str,
        capability: str,
        arguments: dict[str, Any],
        primary_source: str,
        primary_transport: str,
        primary: Callable[[], Awaitable[ToolResult]],
        fallback_source: str | None = None,
        fallback_transport: str = "http_api",
        fallback: Callable[[], Awaitable[ToolResult]] | None = None,
    ) -> ToolResult:
        started = time.monotonic()
        primary_result = await primary()
        self.observations.append(RouteObservation(
            run_id, primary_source, primary_transport, capability, primary_result,
            round((time.monotonic() - started) * 1000, 1),
            error_type=primary_result.reason_codes[0] if primary_result.reason_codes else None,
            tls_or_upstream="tls" if any("tls" in code for code in primary_result.reason_codes) else None,
        ))
        if not fallback or not fallback_source:
            return _annotate(primary_result, run_id=run_id, primary_source=primary_source)

        # Authentication failures are configuration/identity problems.  A
        # blind MCP retry would hide the actionable THS diagnosis and could
        # produce an unrelated datum, so this policy deliberately keeps the
        # result zero-datum and records why fallback was skipped.
        if "gateway_auth_error" in primary_result.reason_codes:
            primary_result.reason_codes = list(dict.fromkeys([
                *primary_result.reason_codes,
                "mcp_fallback_not_attempted_auth",
            ]))
            return _annotate(
                primary_result,
                run_id=run_id,
                primary_source=primary_source,
                fallback_reason="authentication_failure_no_fallback",
                source_switch=False,
            )

        # Partial alone is not a trigger; only a retryable error, a required
        # field gap or missing source provenance warrants a fallback probe.
        trigger = primary_trigger_reason(primary_result, capability, arguments)
        if trigger is None:
            return _annotate(primary_result, run_id=run_id, primary_source=primary_source)
        try:
            fallback_result = await fallback()
        except Exception as exc:  # fallback errors stay zero-datum and auditable
            fallback_result = ToolResult(
                tool=capability,
                arguments=arguments,
                status="error",
                normalized=[],
                error=f"fallback transport failed: {type(exc).__name__}",
                reason_codes=["fallback_transport_error"],
                transport_exception=True,
            )
        fallback_observation = RouteObservation(
            run_id, fallback_source, fallback_transport, capability, fallback_result,
            round((time.monotonic() - started) * 1000, 1),
            error_type=fallback_result.reason_codes[0] if fallback_result.reason_codes else None,
            tls_or_upstream="upstream" if any("upstream" in code for code in fallback_result.reason_codes) else None,
        )
        self.observations.append(fallback_observation)
        if not fallback_satisfies_provenance(primary_result, fallback_result, capability, arguments):
            provenance_failures = fallback_provenance_failures(
                primary_result, fallback_result, capability, arguments
            )
            fallback_observation.fallback_provenance_failures = provenance_failures
            # 逐门失败码也挂到结果对象上（供 run_log/审计读取），汇总码仍进
            # reason_codes 保持兼容。
            fallback_result.fallback_provenance_failures = list(provenance_failures)
            fallback_result.reason_codes = list(dict.fromkeys([
                *fallback_result.reason_codes,
                *provenance_failures,
            ]))

            if fallback_result.status == "error":
                # A partial/success primary can still contain a narrow,
                # auditable fact (for example one dated quote).  If probing
                # the fallback itself fails, retain that fact and expose the
                # fallback failure in diagnostics instead of replacing it with
                # a zero-datum transport error.  An error primary has no such
                # fact and keeps the historical zero-datum behavior below.
                if primary_result.status != STATUS_ERROR and primary_result.normalized:
                    primary_result.reason_codes = list(dict.fromkeys([
                        *primary_result.reason_codes,
                        *fallback_result.reason_codes,
                    ]))
                    return _annotate(
                        primary_result,
                        run_id=run_id,
                        primary_source=primary_source,
                        fallback_source=fallback_source,
                        fallback_reason=(
                            f"{trigger},fallback_error:"
                            f"{fallback_result.reason_codes[0] if fallback_result.reason_codes else 'fallback_error'}"
                        ),
                        source_switch=False,
                    )
                # Preserve the fallback transport diagnosis (TLS/upstream/
                # deadline) while keeping the zero-datum invariant.  The
                # primary failure is still visible in fallback_reason.
                fallback_result.attempts = [
                    *fallback_result.attempts,
                    {"stage": "fallback", "endpoint": fallback_result.endpoint,
                     "attempt": 1, "elapsed_ms": fallback_result.duration_ms,
                     "reason_codes": fallback_result.reason_codes},
                ]
                return _annotate(
                    fallback_result,
                    run_id=run_id,
                    primary_source=primary_source,
                    fallback_source=fallback_source,
                    fallback_reason=",".join(primary_result.reason_codes) or trigger,
                    source_switch=True,
                )
            # Preserve the primary's fact or failure; failed provenance never
            # upgrades it. Record the trigger as well as the rejection.
            primary_result.reason_codes = list(dict.fromkeys([
                *primary_result.reason_codes, trigger, "fallback_provenance_insufficient",
                *provenance_failures,
            ]))
            return _annotate(primary_result, run_id=run_id, primary_source=primary_source,
                             fallback_source=fallback_source, fallback_reason="fallback_provenance_insufficient",
                             source_switch=primary_result.status == STATUS_ERROR)
        return _annotate(
            fallback_result,
            run_id=run_id,
            primary_source=primary_source,
            fallback_source=fallback_source,
            fallback_reason=",".join(primary_result.reason_codes) or trigger,
            source_switch=True,
        )


def _annotate(result: ToolResult, **fields: Any) -> ToolResult:
    for key, value in fields.items():
        setattr(result, key, value)
    return result
