"""Evidence quality gate and reusable claim/evidence matching helpers."""

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from app.models.market import ToolResult
from app.research.entity_check import extract_entity_mentions

_REQUEST_METRICS = {
    "symbol",
    "symbols",
    "exchange",
    "interval",
    "start",
    "end",
    "query",
    "q",
    "keyword",
    "limit",
    "offset",
    "direction",
    "tool_status",
    "request",
    "source",
    "source_used",
    "note",
    "partial",
    "error",
    "detail",
    "ok",
    "date",
    "time",
    "timestamp",
    "datetime",
    "ts",
    "as_of_date",
    "retrieved_at",
}
_NON_BLOCKING_REASONS = {"unknown_date", "unknown_source", "unknown_tool_key", "request_scoped_instrument"}


def _field(obj: Any, key: str, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def is_usable_datum(datum: Any) -> bool:
    metric = str(_field(datum, "metric", "") or "").strip().lower()
    value = _field(datum, "value", None)
    # Gateway normalizers may expose request metadata as ``quote.symbol`` or
    # ``payload.exchange``.  Treat every path component as metadata, rather
    # than only filtering the exact bare metric name.
    metric_parts = [part for part in re.split(r"[.\[\]]+", metric) if part]
    is_request_metadata = any(part in _REQUEST_METRICS for part in metric_parts)
    if metric == "status" or (metric.endswith(".status") and str(value).lower() in {"success", "partial", "error"}):
        is_request_metadata = True
    return bool(
        metric
        and not is_request_metadata
        and _field(datum, "status", "success") != "error"
        and value is not None
        and (not isinstance(value, (str, list, tuple, dict, set)) or bool(value))
    )


def _result_datums(result: ToolResult) -> list[Any]:
    if _field(result, "status") == "error":
        return []
    return [datum for datum in (_field(result, "normalized", []) or []) if is_usable_datum(datum)]


def _instrument_values(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return [str(item).strip().upper() for item in values if str(item).strip()]


def _same_instrument(left: str, right: str) -> bool:
    left_clean = re.sub(r"[^A-Z0-9]", "", left.upper())
    right_clean = re.sub(r"[^A-Z0-9]", "", right.upper())
    if left_clean == right_clean:
        return True
    left_code = re.search(r"\d{6}", left_clean)
    right_code = re.search(r"\d{6}", right_clean)
    return bool(left_code and right_code and left_code.group() == right_code.group())


@dataclass
class EvidenceGateResult:
    has_evidence: bool = False
    successful_tools: list[str] = field(default_factory=list)
    partial_tools: list[str] = field(default_factory=list)
    error_tools: list[dict[str, str]] = field(default_factory=list)
    successful_tool_count: int = 0
    valid_evidence_count: int = 0
    invalid_evidence_count: int = 0
    stale_evidence: list[dict[str, Any]] = field(default_factory=list)
    unmatched_instruments: list[dict[str, Any]] = field(default_factory=list)
    partial_only: list[dict[str, Any]] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    evidence_quality: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


def run_evidence_gate(results: list[ToolResult], requested_date: str | None = None) -> EvidenceGateResult:
    gate = EvidenceGateResult()
    for result in results:
        raw_tool_key = _field(result, "tool_key")
        tool = str(raw_tool_key or _field(result, "tool") or "unknown")
        status = _field(result, "status")
        raw_normalized = list(_field(result, "normalized", []) or [])
        datums = _result_datums(result)
        invalid_datum_count = 0 if status == "error" else len(raw_normalized) - len(datums)
        if invalid_datum_count:
            gate.invalid_evidence_count += invalid_datum_count
            gate.reason_codes.append("invalid_datum")
        if not raw_tool_key:
            gate.reason_codes.append("unknown_tool_key")
        if status == "success":
            gate.successful_tool_count += 1
            gate.successful_tools.append(tool)
        elif status == "partial":
            gate.partial_tools.append(tool)
        elif status == "error":
            gate.error_tools.append({"tool": tool, "error": str(_field(result, "error") or "unknown error")})
            gate.invalid_evidence_count += 1
            gate.reason_codes.append("status_error")
            gate.evidence_quality.append({"tool_key": tool, "operation_id": _field(result, "operation_id") or _field(result, "tool"), "valid": False, "reason_codes": ["status_error"], "evidence_ids": []})
        if status in {"success", "partial"} and not datums:
            if not raw_normalized:
                gate.invalid_evidence_count += 1
                gate.reason_codes.append("empty_normalized")
            reasons = ["empty_normalized"] if not raw_normalized else ["invalid_datum"]
            gate.evidence_quality.append({"tool_key": tool, "operation_id": _field(result, "operation_id") or _field(result, "tool"), "valid": False, "reason_codes": reasons, "evidence_ids": []})
            continue
        for index, datum in enumerate(datums):
            timestamp = _field(datum, "timestamp") or _field(datum, "as_of_date") or _field(result, "as_of_date")
            reasons: list[str] = []
            if not timestamp:
                # Unknown is retained as metadata; it is not silently filled
                # with retrieval time and does not erase an otherwise real datum.
                reasons.append("unknown_date")
            if requested_date and timestamp and str(timestamp)[:10] != requested_date:
                reasons.append("stale_date")
                gate.stale_evidence.append({"tool_key": tool, "datum_index": index, "as_of_date": str(timestamp)[:10], "expected": requested_date})
            arguments = _field(result, "arguments", {}) or {}
            returned_instrument = _field(datum, "instrument")
            expected_instruments = _instrument_values(arguments.get("symbol") or arguments.get("symbols"))
            actual_instruments = _instrument_values(returned_instrument)
            if expected_instruments and (not actual_instruments or not any(_same_instrument(expected, actual) for expected in expected_instruments for actual in actual_instruments)):
                if actual_instruments:
                    reasons.append("unmatched_instrument")
                    gate.unmatched_instruments.append({"tool_key": tool, "datum_index": index, "expected": expected_instruments, "actual": actual_instruments})
                elif len(expected_instruments) > 1:
                    reasons.append("unknown_returned_instrument")
                    gate.unmatched_instruments.append({"tool_key": tool, "datum_index": index, "expected": expected_instruments, "actual": ["unknown"]})
                else:
                    reasons.append("request_scoped_instrument")
            instrument = returned_instrument or (expected_instruments[0] if len(expected_instruments) == 1 else None)
            declared_scope = str(_field(datum, "instrument_scope", "") or "").strip().lower()
            coverage = declared_scope if declared_scope and declared_scope != "unknown" else ("instrument" if instrument else "market")
            partial = bool(_field(result, "partial") or _field(datum, "partial") or status == "partial" or _field(datum, "status") == "partial")
            hard_reasons = [reason for reason in reasons if reason not in _NON_BLOCKING_REASONS]
            source = _field(datum, "source") or _field(datum, "authority") or _field(result, "authority") or "unknown"
            if not (_field(datum, "source") or _field(datum, "authority") or _field(result, "authority")):
                reasons.append("unknown_source")
            metric = str(_field(datum, "metric", "") or "")
            row = {"tool_key": tool, "operation_id": _field(result, "operation_id") or _field(result, "tool"), "datum_index": index, "metric": metric, "datum_value": _field(datum, "value"), "evidence_id": f"{tool}-{index}", "source": source, "requested_instruments": expected_instruments, "returned_instrument": returned_instrument or "unknown", "instrument": instrument or "unknown", "as_of_date": str(timestamp)[:10] if timestamp else "unknown", "retrieved_at": _field(datum, "retrieved_at") or _field(result, "retrieved_at") or "unknown", "completeness": "partial" if partial else (_field(datum, "completeness") or _field(result, "completeness") or "unknown"), "coverage": coverage, "valid": not hard_reasons, "reason_codes": reasons}
            gate.evidence_quality.append(row)
            hard_reasons = [reason for reason in reasons if reason not in _NON_BLOCKING_REASONS]
            gate.reason_codes.extend(reasons)
            if hard_reasons:
                gate.invalid_evidence_count += 1
            else:
                gate.valid_evidence_count += 1
            if partial:
                gate.partial_only.append({"tool_key": tool, "evidence_id": row["evidence_id"], "scope": coverage, "as_of_date": row["as_of_date"]})
    gate.reason_codes = list(dict.fromkeys(gate.reason_codes))
    gate.has_evidence = gate.valid_evidence_count > 0
    if not gate.has_evidence:
        gate.reason_codes.append("no_valid_datum")
        failed = ", ".join(item["tool"] for item in gate.error_tools)
        gate.reason = "没有可用于论断的有效 datum" + (f"；失败工具: {failed}" if failed else "")
    else:
        gate.reason = f"成功工具 {gate.successful_tool_count} 个；有效 datum {gate.valid_evidence_count} 条"
    if gate.error_tools:
        gate.reason += f"；失败工具 {len(gate.error_tools)} 个"
    return gate


def _claim_needs_price(claim: str) -> bool:
    return bool(re.search(r"价格|股价|上涨|下跌|涨停|跌停|收涨|收跌|行情|报价|突破", claim))


def _claim_codes(claim: str) -> set[str]:
    return set(re.findall(r"(?<!\d)(?:SH|SZ|BJ)?\d{6}(?!\d)", claim.upper()))


def _claim_needs_date(claim: str) -> bool:
    return bool(re.search(r"今日|今天|当日|当天|截至|日期|交易日|\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}", claim))


_GENERIC_CLAIM_WORDS = {
    "今日",
    "今天",
    "当日",
    "当天",
    "上涨",
    "下跌",
    "涨停",
    "跌停",
    "收涨",
    "收跌",
    "公司",
    "股票",
    "个股",
    "该股",
    "市场",
    "行情",
    "指数",
}
_THEME_OR_INDEX_TERMS = {
    "商业航天",
    "人工智能",
    "低空经济",
    "算力",
    "机器人",
    "半导体",
    "芯片",
    "新能源",
    "军工",
    "沪深300",
    "上证指数",
    "深证成指",
    "创业板指",
    "科创50",
    "中证500",
    "中证1000",
}


def _market_predicate_reason(claim: str, item: Any) -> str | None:
    metric = str(_field(item, "metric", "") or "").lower()
    value = _field(item, "value")
    direction_metric = bool(re.search(r"(?:^|[.\[\]])(?:change|change_pct|pct_change|return|涨跌幅|涨幅|跌幅|涨跌额)(?:$|[._\[\]])", metric))
    price_metric = bool(re.search(r"(?:^|[.\[\]])(?:price|last|close|报价|收盘价)(?:$|[._\[\]])", metric))
    if "涨停" in claim:
        is_status = bool(re.search(r"(?:^|[.])(?:is_)?limit_up(?:_status)?$|涨停状态$", metric))
        is_status = is_status or (metric.endswith(".status") and str(value) == "涨停")
        return None if is_status and value in (True, 1, "1", "true", "涨停", "封板", "limit_up") else "missing_limit_up_status"
    if "跌停" in claim:
        is_status = bool(re.search(r"(?:^|[.])(?:is_)?limit_down(?:_status)?$|跌停状态$", metric))
        is_status = is_status or (metric.endswith(".status") and str(value) == "跌停")
        return None if is_status and value in (True, 1, "1", "true", "跌停", "封跌停", "limit_down") else "missing_limit_down_status"
    if re.search(r"上涨|收涨", claim):
        if not direction_metric:
            return "missing_direction_metric"
        try:
            return None if float(value) > 0 else "contradictory_direction"
        except (TypeError, ValueError):
            return "missing_direction_metric"
    if re.search(r"下跌|收跌", claim):
        if not direction_metric:
            return "missing_direction_metric"
        try:
            return None if float(value) < 0 else "contradictory_direction"
        except (TypeError, ValueError):
            return "missing_direction_metric"
    if _claim_needs_price(claim) and not (price_metric or direction_metric or re.search(r"(?:^|[.\[\]])(?:limit_up|limit_down|status|涨停状态|跌停状态)(?:$|[._\[\]])", metric)):
        return "missing_direction_metric"
    return None


def _numeric_claim_reason(claim: str, item: Any) -> str | None:
    metric = str(_field(item, "metric", "") or "").lower()
    value = _field(item, "value")
    percent = re.search(r"(?:上涨|下跌|收涨|收跌|涨幅|跌幅|涨跌幅)\s*([+-]?\d+(?:\.\d+)?)\s*[%％]", claim)
    price = re.search(r"(?:价格|股价|报价|收盘价)(?:为|是|达|到)?\s*([+-]?\d+(?:\.\d+)?)\s*元?", claim)
    if percent:
        if not re.search(r"pct|percent|涨跌幅|涨幅|跌幅", metric):
            return "missing_percent_metric"
        expected = float(percent.group(1))
    elif price:
        if not re.search(r"price|last|close|报价|收盘价", metric):
            return "missing_price_metric"
        expected = float(price.group(1))
    else:
        return None
    try:
        actual = float(value)
    except (TypeError, ValueError):
        return "unverifiable_numeric_value"
    tolerance = max(0.01, abs(expected) * 0.001)
    return None if abs(abs(actual) - abs(expected)) <= tolerance else "numeric_mismatch"


def match_claims_to_evidence(report: Any, evidence: list[Any], expected_date: str | None = None) -> list[dict[str, Any]]:
    """Check every claim's cited IDs and observable scope after Reasoning."""
    by_id = {str(_field(item, "id")): item for item in evidence or [] if _field(item, "id")}
    rows: list[dict[str, Any]] = []
    for claim in _field(report, "claims", []) or []:
        text = str(_field(claim, "claim", "") or "")
        ids = [str(x) for x in (_field(claim, "evidence_ids", []) or [])]
        hard_reasons: list[str] = []
        candidate_reasons: list[str] = []
        predicate_failures: list[str] = []
        matched: list[str] = []
        candidates: list[Any] = []
        for eid in ids:
            item = by_id.get(eid)
            if item is None:
                hard_reasons.append("invalid_reference")
                continue
            matched.append(eid)
            local_reasons: list[str] = []
            status = str(_field(item, "status", "success"))
            metric = str(_field(item, "metric", "") or "").lower()
            source_tool = str(_field(item, "source_tool", "") or "").lower()
            if status == "error" or metric == "tool_status":
                hard_reasons.append("invalid_evidence")
                continue
            if _claim_needs_price(text) and ("news" in source_tool or "telegraph" in source_tool) and not re.search(r"price|close|last|change|涨跌|涨停", metric):
                local_reasons.append("news_mention_not_price")
            is_partial = bool(
                _field(item, "partial")
                or _field(item, "status") == "partial"
                or _field(item, "completeness") == "partial"
            )
            if is_partial and not re.search(r"部分|样本|观察|截至|限定|可能|无法确认", text):
                local_reasons.append("partial_only")
            date = _field(item, "as_of_date") or _field(item, "timestamp")
            if _claim_needs_date(text) and (not date or str(date).lower() == "unknown"):
                local_reasons.append("unknown_date")
            claim_dates = {
                value.replace("/", "-")
                for value in re.findall(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}", text)
            }
            evidence_date = str(date)[:10].replace("/", "-") if date else ""
            if claim_dates and evidence_date and evidence_date not in claim_dates:
                local_reasons.append("stale_date")
            elif expected_date and _claim_needs_date(text) and evidence_date and evidence_date != expected_date[:10].replace("/", "-"):
                local_reasons.append("stale_date")
            source = _field(item, "source") or _field(item, "authority")
            if re.search(r"新闻|消息|公告|报道", text) and (not source or str(source).lower() == "unknown"):
                local_reasons.append("unknown_source")
            codes = {re.sub(r"[^0-9]", "", code) for code in _claim_codes(text)}
            instrument = str(_field(item, "instrument", "") or "").upper()
            instrument_codes = set(re.findall(r"\d{6}", instrument))
            if codes and (not instrument or not instrument_codes.intersection(codes)):
                local_reasons.append("unmatched_instrument" if instrument else "market_level_for_single_instrument")
            instrument_name = str(_field(item, "instrument_name", "") or "")
            claim_names = [name for name in extract_entity_mentions(text) if name not in _THEME_OR_INDEX_TERMS]
            verified_codes = _field(report, "entity_code_matches", {}) or {}
            if instrument_name and claim_names and instrument_name not in text:
                local_reasons.append("unmatched_instrument")
            elif claim_names and not codes and instrument and not instrument_name:
                # A code-only datum cannot prove which Chinese name it refers
                # to unless P0-A has supplied a verified master-data mapping.
                matching_codes = [str(verified_codes.get(name) or "") for name in claim_names]
                if not matching_codes or any(not code for code in matching_codes):
                    local_reasons.append("instrument_name_unknown")
                elif not any(_same_instrument(code, instrument) for code in matching_codes):
                    local_reasons.append("unmatched_instrument")
            if claim_names and _claim_needs_price(text) and str(_field(item, "coverage", "") or "").lower() == "market" and not codes:
                local_reasons.append("market_level_for_single_instrument")
            named_subjects = [term for term in _THEME_OR_INDEX_TERMS if term in text]
            if named_subjects and not any(
                subject in instrument_name or subject in metric or subject in str(_field(item, "value", "") or "")
                for subject in named_subjects
            ):
                local_reasons.append("unmatched_subject")
            if local_reasons:
                candidate_reasons.extend(local_reasons)
            else:
                candidates.append(item)
        if candidates and _claim_needs_price(text):
            groups: dict[tuple[str, str, str], list[Any]] = {}
            for item in candidates:
                date_key = str(_field(item, "as_of_date") or _field(item, "timestamp") or "")[:10].replace("/", "-")
                instrument_key = str(_field(item, "instrument", "") or "").upper()
                metric_key = str(_field(item, "metric", "") or "").lower()
                family = "direction" if re.search(r"(?:^|[.\[\]])(?:change|change_pct|pct_change|return|涨跌|涨幅|跌幅)(?:$|[._\[\]])", metric_key) else "price" if re.search(r"(?:^|[.\[\]])(?:price|last|close)(?:$|[._\[\]])", metric_key) else "other"
                groups.setdefault((date_key, instrument_key, family), []).append(item)
            for group_items in groups.values():
                predicate_reasons = [_market_predicate_reason(text, item) for item in group_items]
                if all(reason is not None for reason in predicate_reasons):
                    predicate_failures.append(next((reason for reason in predicate_reasons if reason == "contradictory_direction"), predicate_reasons[0]))
                numeric_reasons = [_numeric_claim_reason(text, item) for item in group_items]
                if all(reason is not None for reason in numeric_reasons):
                    predicate_failures.append(next((reason for reason in numeric_reasons if reason == "numeric_mismatch"), numeric_reasons[0]))
                # Multiple cited observations of the same subject/date/family
                # must not silently resolve in favor of whichever fits.
                if any(reason is None for reason in predicate_reasons) and "contradictory_direction" in predicate_reasons:
                    predicate_failures.append("conflicting_evidence")
                if any(reason is None for reason in numeric_reasons) and "numeric_mismatch" in numeric_reasons:
                    predicate_failures.append("conflicting_evidence")
        if not ids:
            hard_reasons.append("missing_reference")
        # A news citation may provide context alongside a complete quote.
        # Scope/date/partial shortcomings only block when no cited datum can
        # actually support the claim; forged or failed references always block.
        reasons = hard_reasons + predicate_failures + (candidate_reasons if not candidates else [])
        rows.append({"claim": text, "evidence_ids": ids, "matched_evidence_ids": matched, "supported": bool(candidates) and not reasons, "reason_codes": list(dict.fromkeys(reasons))})
    return rows
