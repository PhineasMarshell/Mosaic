"""Evidence quality gate and reusable claim/evidence matching helpers."""

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from app.gateway.tool_registry import resolve_tool, resolve_tool_by_name
from app.models.market import ToolResult
from app.research.entity_check import extract_entity_mentions
from app.research.trading_calendar import normalize_date

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
    "trade_date",
    "trading_date",
    "日期",
    "交易日期",
    "交易日",
    "时间",
    "更新时间",
    "time",
    "timestamp",
    "datetime",
    "ts",
    "as_of_date",
    "retrieved_at",
}
_NON_BLOCKING_REASONS = {"unknown_date", "unknown_source", "unknown_tool_key", "request_scoped_instrument"}


def _field(obj: Any, key: str, default=None):
    value = obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)
    return default if value is None else value


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
    # 结构元数据（envelope count/total/paging、MCP 类型/日期注记）只描述批次，
    # 不是可主张的业务事实：永远不生成 Evidence，也不通过 strict provenance。
    is_structural = bool(_field(datum, "structural_metadata", False))
    return bool(
        metric
        and not is_request_metadata
        and not is_structural
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


def evidence_date_scope(tool_key: str | None, operation_id: str | None) -> str:
    """Separate trading observations from dated events and reference data."""
    meta = None
    for resolver, value in ((resolve_tool, tool_key), (resolve_tool_by_name, operation_id)):
        if not value:
            continue
        try:
            meta = resolver(str(value))
            break
        except KeyError:
            continue
    if meta is not None:
        if meta.key == "iwencai_finance":
            return "reporting_period"
        if meta.key == "iwencai_basicinfo":
            return "static"
        if meta.key == "iwencai_macro":
            return "economic_period"
        if meta.key == "iwencai_management":
            return "snapshot"
        if meta.key == "iwencai_industry":
            return "mixed_reference"
        if meta.key == "iwencai_report_search":
            return "event"
        if meta.category == "news" or meta.key == "timeline":
            return "event"
        if meta.category in {"technical", "moneyflow"} and meta.key not in {"search", "hk_search"}:
            return "market"
        return "other"
    name = f"{tool_key or ''} {operation_id or ''}".lower()
    if any(token in name for token in ("news", "telegraph", "announcement", "disclosure", "discussion")):
        return "event"
    return "other"


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


def run_evidence_gate(
    results: list[ToolResult], requested_date: str | None = None, *, event_date: str | None = None,
) -> EvidenceGateResult:
    gate = EvidenceGateResult()
    market_expected_date = normalize_date(requested_date)
    event_expected_date = normalize_date(event_date) or market_expected_date
    for result in results:
        raw_tool_key = _field(result, "tool_key")
        tool = str(raw_tool_key or _field(result, "tool") or "unknown")
        operation_id = _field(result, "operation_id") or _field(result, "tool")
        date_scope = evidence_date_scope(raw_tool_key, operation_id)
        expected_date = event_expected_date if date_scope == "event" else market_expected_date
        if date_scope in {"reporting_period", "economic_period", "static"}:
            # A report period/static attribute is not today's trading observation.
            expected_date = None
        status = _field(result, "status")
        raw_normalized = list(_field(result, "normalized", []) or [])
        # 结构元数据不是业务 datum，也不算"无效证据"——它不进证据账本。
        business_normalized = [
            d for d in raw_normalized if not bool(_field(d, "structural_metadata", False))
        ]
        datums = _result_datums(result)
        invalid_datum_count = 0 if status == "error" else len(business_normalized) - len(datums)
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
            # Typed normalized fields can have mixed scopes in one response.
            # Reset per datum; a report field must not change the next quote's
            # date requirement. No Skill-specific admission branches here.
            date_scope = evidence_date_scope(raw_tool_key, operation_id)
            expected_date = event_expected_date if date_scope == "event" else market_expected_date
            if date_scope in {"reporting_period", "economic_period", "static", "snapshot"}:
                expected_date = None
            value = _field(datum, "value")
            datum_scope = value.get("date_scope") if isinstance(value, dict) else None
            if datum_scope in {"market", "event", "reporting_period", "snapshot", "static"}:
                date_scope = datum_scope
                expected_date = market_expected_date if date_scope == "market" else event_expected_date if date_scope == "event" else None
            # as_of_date is the observation date.  A timestamp may be a
            # retrieval/event timestamp, and retrieved_at is never eligible.
            timestamp = (
                normalize_date(_field(datum, "as_of_date"))
                or normalize_date(_field(datum, "timestamp"))
                or normalize_date(_field(result, "as_of_date"))
            )
            reasons: list[str] = []
            if not timestamp:
                # Unknown is retained as metadata; it is not silently filled
                # with retrieval time and does not erase an otherwise real datum.
                reasons.append("unknown_date")
            if expected_date and timestamp and timestamp != expected_date:
                reasons.append("stale_date")
                gate.stale_evidence.append({"tool_key": tool, "datum_index": index, "as_of_date": timestamp, "expected": expected_date})
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
            # A partial result limits set-wide claims, but does not make each
            # complete, dated row partial. Keep both scopes visible to Critic.
            partial = bool(_field(datum, "partial") or _field(datum, "status") == "partial")
            result_partial = bool(_field(result, "partial") or status == "partial")
            # 语义契约工具（涨停家数/板块）：无观察日或 partial 的业务 datum 无法证明
            # 交易日，必须硬拒绝，不能像通用市场 datum 那样把 unknown_date 当作
            # 非阻塞、也不能让 partial 计入有效证据。
            semantic_contract = bool(getattr(result, "date_annotation", None))
            if semantic_contract and date_scope == "market":
                if not timestamp:
                    reasons.append("missing_as_of_date")
                if partial:
                    reasons.append("partial_datum")
            hard_reasons = [reason for reason in reasons if reason not in _NON_BLOCKING_REASONS]
            source = _field(datum, "source") or _field(datum, "authority") or _field(result, "authority") or "unknown"
            if not (_field(datum, "source") or _field(datum, "authority") or _field(result, "authority")):
                reasons.append("unknown_source")
            metric = str(_field(datum, "metric", "") or "")
            row = {"tool_key": tool, "operation_id": operation_id, "datum_index": index, "metric": metric, "datum_value": _field(datum, "value"), "evidence_id": f"{tool}-{index}", "source": source, "requested_instruments": expected_instruments, "returned_instrument": returned_instrument or "unknown", "instrument": instrument or "unknown", "as_of_date": timestamp or "unknown", "date_scope": date_scope, "retrieved_at": _field(datum, "retrieved_at") or _field(result, "retrieved_at") or "unknown", "completeness": "partial" if partial else (_field(datum, "completeness") or _field(result, "completeness") or "unknown"), "coverage": coverage, "partial": partial, "result_partial": result_partial, "valid": not hard_reasons, "reason_codes": reasons}
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
    return bool(re.search(r"价格|股价|点位|指数值|上涨|下跌|涨停|跌停|收涨|收跌|行情|报价|突破", claim))


def evidence_domain_compatible(item: Any, claim: str, expected_domain: str) -> bool:
    """Separate source categories from an explicitly different security market."""
    item_domain = str(_field(item, "domain", "") or "").strip().lower()
    if not expected_domain or expected_domain == "cross" or not item_domain or item_domain in {"unknown", expected_domain}:
        return True
    if expected_domain != "a_share":
        return False
    if item_domain in {"hk_stock", "us_stock", "crypto"}:
        return False
    if item_domain not in {"media", "reference"}:
        return True
    instrument = str(_field(item, "instrument", "") or "").upper().replace(" ", "")
    if item_domain == "reference":
        # Reference also labels index and industry rows. It is a source kind,
        # not a foreign market; reject only an explicitly foreign instrument.
        return not (instrument.startswith("HK") or instrument.endswith(".HK"))
    if not re.search(r"新闻|消息|公告|报道|研报|事件", claim) and instrument:
        # 有具体 instrument 的 media 证据必须配一条新闻类 claim；市场级 media
        # 证据（instrument 为空，例如"两市成交额报道"）本身不指向任何证券，
        # 下面按 claim/实体是否点名证券来判断是否跨市场错配。
        return False
    # 明确的境外标的永远拒绝（HK/US），与 claim 里是否写了代码无关。
    if instrument.startswith(("HK", "US")) or instrument.endswith((".HK", ".US")):
        return False
    # 裸数字且不是 A 股的 6 位代码（例如港股惯用的 5 位 "00700"）也是境外标的。
    if re.fullmatch(r"\d+", instrument) and not re.fullmatch(r"\d{6}", instrument):
        return False
    if re.fullmatch(r"[A-Z][A-Z.]{0,5}", instrument):
        # 纯字母代码（AAPL / MSFT / BRK.B）不是 A 股标的，市场级媒体证据不能
        # 替它背书。
        return False
    if not re.fullmatch(r"(?:(?:SH|SZ|BJ)?\d{6}|\d{6}\.(?:SH|SZ|BJ))", instrument):
        # 市场级媒体/参考证据（例如"两市成交额报道"）不指向任何单一证券。
        # 只有 claim 也没点名任何证券（既无代码，也无具名实体）时才不算跨市场错配，
        # 否则"用市场级新闻替境外标的背书"会绕开这道闸门。
        return not _claim_codes(claim) and not extract_entity_mentions(claim)
    return True


#: 单篇新闻/公告类 evidence 里可人工复审的正文线索（标题 / 摘要 / 链接）。
_NEWS_REVIEW_FIELDS = ("title", "headline", "subject", "summary", "digest", "abstract", "url", "link", "source_url")
#: 中文指标名同样代表可审阅正文（同花顺问财返回的是中文列名）。
_NEWS_REVIEW_FIELD_NAMES = frozenset(
    set(_NEWS_REVIEW_FIELDS)
    | {
        "content", "text", "body", "description",
        "标题", "题目", "主题", "摘要", "概要", "简介", "内容", "正文", "链接", "网址", "来源",
    }
)
_NEWS_INDEXED_PATH_RE = re.compile(r"(?:^|\.)(?:items|data|rows|results|list)(?:\[\d+\])?\.?$")


def _is_single_news_item(item: Any, source_tool: str) -> bool:
    """判断这条 evidence 是否是"新闻/公告检索结果里的单篇"。"""
    metric = str(_field(item, "metric", "") or "").strip().lower()
    if metric:
        if "search_results" in metric or _NEWS_INDEXED_PATH_RE.search(metric):
            return True
    haystack = f"{metric} {source_tool} {_field(item, 'category', '') or ''} {_field(item, 'domain', '') or ''}".lower()
    return any(token in haystack for token in ("news", "telegraph", "announcement", "disclosure", "媒体", "公告"))


def _has_reviewable_news_content(item: Any, result: Any = None) -> bool:
    """单篇新闻必须带可审阅内容：标题 / 摘要 / url 之一。"""
    metric = str(_field(item, "metric", "") or "").lower()
    segments = [segment for segment in re.split(r"[.\[\]]+", metric) if segment]
    leaf = segments[-1] if segments else ""
    if leaf in _NEWS_REVIEW_FIELD_NAMES:
        return True
    value = _field(item, "value")
    if value is None:
        value = _field(item, "datum_value")
    if isinstance(value, dict):
        return any(str(key).lower() in _NEWS_REVIEW_FIELD_NAMES for key in value)
    if isinstance(value, str) and len(value.strip()) >= 8:
        return True
    return False


_SINGLE_NEWS_UNSUPPORTED_SCOPE = re.compile(
    r"全市场|全部|所有|全貌|完整|排名|最强|领先|第一|主线|前[一二三四五六七八九十0-9]+|"
    r"因果|导致|带动|引发|造成|因为|由于|\b(?:all|entire|complete|ranking|causes?)\b",
    re.IGNORECASE,
)


def _single_news_evidence_allowed(
    item: Any, *, is_partial: bool, source_tool: str, claim: str, claim_type: str = ""
) -> bool:
    """Allow search-result completeness only for a dated, limited event claim."""
    if is_partial:
        return False
    if not _claim_needs_date(claim) or _SINGLE_NEWS_UNSUPPORTED_SCOPE.search(claim):
        return False
    if claim_type in {"comparison", "causation"}:
        return False
    completeness = str(_field(item, "completeness", "") or "").strip().lower()
    if completeness not in {"search_results", "page"}:
        return False
    if not normalize_date(_field(item, "as_of_date")):
        return False
    if not all(str(_field(item, key, "") or "").strip() for key in ("source", "authority", "retrieved_at")):
        return False
    return _has_reviewable_news_content(item) and _is_single_news_item(item, source_tool)


def _claim_codes(claim: str) -> set[str]:
    return set(re.findall(r"(?<!\d)(?:SH|SZ|BJ)?\d{6}(?!\d)", claim.upper()))


def _claim_needs_date(claim: str) -> bool:
    return bool(re.search(r"今日|今天|当日|当天|截至|日期|交易日|\d{1,2}月\d{1,2}日", claim) or normalize_date(claim))


def _claim_explicit_dates(text: str, reference_date: str | None) -> set[str]:
    dates = {
        normalized
        for value in re.findall(r"20\d{2}年\d{1,2}月\d{1,2}日?|(?<!\d)20\d{2}[-/]?\d{1,2}[-/]?\d{1,2}(?!\d)", text)
        if (normalized := normalize_date(value))
    }
    if reference_date:
        reference = date.fromisoformat(reference_date)
        for match in re.finditer(r"(?<!年)(?<!\d)(\d{1,2})月(\d{1,2})日", text):
            try:
                candidate = date(reference.year, int(match.group(1)), int(match.group(2)))
                if candidate > reference:
                    candidate = candidate.replace(year=candidate.year - 1)
                dates.add(candidate.isoformat())
            except ValueError:
                continue
    return dates


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
    "恒生指数",
    "纳斯达克指数",
    "道琼斯指数",
    "标普500",
}


def _market_metric_value(item: Any) -> tuple[str, Any]:
    """Read market fields inside typed normalized row context."""
    metric = str(_field(item, "metric", "") or "").lower()
    value = _field(item, "value")
    if isinstance(value, dict) and "value" in value:
        if value.get("date_scope") == "market":
            semantic = metric + " " + str(value.get("indicator", ""))
            if re.search(r"涨跌幅|涨幅|跌幅", semantic):
                metric += ".change_pct"
            elif re.search(r"最新价|收盘价|开盘价|价格|点位|指数值", semantic):
                metric += ".price"
            value = value.get("value")
    return metric, value


def _typed_claim_reasons(claim: str, item: Any) -> list[str]:
    """An attributed opinion cannot become an observed or realized fact."""
    value = _field(item, "value")
    if not isinstance(value, dict) or value.get("data_kind") != "institutional_opinion":
        return []
    reasons = []
    institution = value.get("institution_name")
    if not institution:
        reasons.append("missing_institution_provenance")
    elif str(institution) not in claim:
        reasons.append("unattributed_institutional_opinion")
    if not value.get("publication_date"):
        reasons.append("missing_publication_date")
    if not (_field(item, "source") or _field(item, "authority")):
        reasons.append("unknown_source")
    if not _field(item, "instrument"):
        reasons.append("unknown_returned_instrument")
    if re.search(r"已实现|实际(?:利润|业绩|收入|股价)|实现.{0,8}(?:利润|收入)|保证|必然", claim):
        reasons.append("institutional_opinion_not_realized")
    kind = value.get("opinion_kind")
    if kind == "forecast":
        target = str(value.get("target_period") or "")
        if not target:
            reasons.append("missing_forecast_period")
        else:
            years = set(re.findall(r"(20\d{2})年", claim))
            if years and not any(year in target for year in years):
                reasons.append("forecast_period_mismatch")
    qualifiers = {
        "forecast": r"预测|预期",
        "target_price": r"目标价",
        "rating": r"评级|评分|星级",
        "recommendation": r"金股|推荐|机构观点",
        "research_report": r"研报|研究观点|机构观点|标题",
    }
    if kind in qualifiers and not re.search(qualifiers[kind], claim):
        reasons.append("institutional_opinion_not_observation")
    return reasons


def _market_predicate_reason(claim: str, item: Any) -> str | None:
    metric, value = _market_metric_value(item)
    if re.search(r"股价|股票价格|个股|股票", claim) and _field(item, "coverage") in {"industry", "industry_index", "index"}:
        return "unmatched_instrument"
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
    metric, value = _market_metric_value(item)
    percent = re.search(r"(?:上涨|下跌|收涨|收跌|涨幅|跌幅|涨跌幅)\s*([+-]?\d+(?:\.\d+)?)\s*[%％]", claim)
    price = re.search(r"(?:价格|股价|报价|收盘价|点位|指数值)(?:为|是|达|到)?\s*([+-]?\d+(?:\.\d+)?)\s*(?:元|点)?", claim)
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


def _code_owned_limitation(
    text: str,
    *,
    requested_date: str | None,
    expected_date: str | None,
    market_closed: bool | None,
) -> bool:
    """Allow only complete calendar/data-gap templates, never positive prose.

    A structure label or a neighbouring caveat is not proof. Calendar clauses
    must agree with code-owned request metadata; every other clause must be
    an explicit statement of unavailable/unverified data.
    """
    clauses = [part.strip() for part in re.split(r"[，,；;。\n]+", text) if part.strip()]
    if not clauses:
        return False
    date_pattern = r"20\d{2}-\d{2}-\d{2}"
    known_dates = {value for value in (requested_date, expected_date) if value}
    for clause in clauses:
        dates = re.findall(date_pattern, clause)
        if dates and (not known_dates or any(value not in known_dates for value in dates)):
            return False
        calendar = re.fullmatch(rf"(?:请求日\s*)?(?:({date_pattern})\s*)?(?:A股|市场)?休市", clause)
        if calendar:
            if market_closed is not True:
                return False
            if calendar.group(1) and calendar.group(1) != requested_date:
                return False
            continue
        if re.fullmatch(rf"(?:可核验)?数据截至\s*{date_pattern}", clause):
            if not expected_date or dates != [expected_date]:
                return False
            continue
        gap = re.fullmatch(
            rf"(?:(?:请求日|最近交易日)\s*(?:{date_pattern})?\s*)?"
            r"(?:(?:A股)?(?:行情|数据|行情数据|行情证据)\s*)?"
            r"(?:暂无|尚无)(?:可核验|可用交易日)(?:行情数据|行情证据|数据|证据|行情)?",
            clause,
        )
        unverified = re.fullmatch(
            rf"(?:(?:请求日|最近交易日)\s*(?:{date_pattern})?\s*)?"
            r"(?:(?:行情|数据|行情数据)\s*)?(?:尚)?(?:未验证|未核实|无法确认)",
            clause,
        )
        if not gap and not unverified:
            return False
    return True


def match_claims_to_evidence(
    report: Any,
    evidence: list[Any],
    expected_date: str | None = None,
    *,
    requested_date: str | None = None,
    market_closed: bool | None = None,
    expected_domain: str | None = None,
    strict_provenance: bool = False,
    expected_instruments: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Check every claim's cited IDs and observable scope after Reasoning.

    ``strict_provenance`` is used by the terminal audit/replay boundary.  The
    legacy matcher remains permissive for old analytical callers, while the
    strict path requires an explicit source, retrieval timestamp, dated
    observation and usable coverage before an evidence item can support a
    delivered claim. Only exact code-owned calendar/data-gap templates are
    exempt from citations; a structure label or possibility word is not enough.
    """
    by_id = {str(_field(item, "id")): item for item in evidence or [] if _field(item, "id")}
    planned_date = normalize_date(expected_date)
    request_date = normalize_date(requested_date)
    rows: list[dict[str, Any]] = []
    for claim in _field(report, "claims", []) or []:
        text = str(_field(claim, "claim", "") or "")
        claim_type = str(_field(claim, "claim_type", "") or "").lower()
        ids = [str(x) for x in (_field(claim, "evidence_ids", []) or [])]
        hard_reasons: list[str] = []
        candidate_reasons: list[str] = []
        predicate_failures: list[str] = []
        matched: list[str] = []
        candidates: list[Any] = []
        if strict_provenance and not ids and _code_owned_limitation(
            text, requested_date=request_date, expected_date=planned_date, market_closed=market_closed,
        ):
            rows.append({"claim": text, "evidence_ids": [], "matched_evidence_ids": [],
                         "supported": True, "reason_codes": []})
            continue
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
            if strict_provenance and (status.lower() not in {"success", "partial"} or not is_usable_datum(item)):
                hard_reasons.append("invalid_evidence")
                continue
            if expected_domain and not evidence_domain_compatible(item, text, expected_domain):
                hard_reasons.append("domain_mismatch")
                continue
            local_reasons.extend(_typed_claim_reasons(text, item))
            if _claim_needs_price(text) and any(token in source_tool for token in ("news", "telegraph", "announcement", "iwencai_report_search")) and not re.search(r"price|close|last|change|涨跌|涨停", metric):
                local_reasons.append("news_mention_not_price")
            is_partial = bool(
                _field(item, "partial")
                or _field(item, "status") == "partial"
                or _field(item, "completeness") == "partial"
            )
            if is_partial and not re.search(r"部分|样本|观察|截至|限定|可能|无法确认", text):
                local_reasons.append("partial_only")
            if _field(item, "result_partial", False) and re.search(
                r"最强|全市场|所有|全部|全貌|完整(?:行情|覆盖|数据)|(?:行情|数据|覆盖).{0,8}(?:完整|全貌)|前[一二三四五六七八九十0-9]+|排名|领先|第一|市场情绪|资金流向|资金流入|资金流出",
                text,
            ):
                local_reasons.append("partial_only")
            # A retrieved timestamp describes when we fetched data, never the
            # observation date.  Terminal verification therefore uses the
            # explicit as_of_date and does not fall back to timestamp.
            date = normalize_date(_field(item, "as_of_date"))
            if not strict_provenance:
                date = date or normalize_date(_field(item, "timestamp"))
            if _claim_needs_date(text) and not date:
                local_reasons.append("unknown_date")
            if strict_provenance:
                if not source_tool or source_tool in {"unknown", "reasoning_engine"}:
                    local_reasons.append("missing_source_tool")
                if not date:
                    local_reasons.append("missing_as_of_date")
                elif planned_date and date != planned_date:
                    local_reasons.append("stale_date")
                month = re.search(r"(20\d{2})\s*年\s*(1[0-2]|0?[1-9])\s*月", text)
                if month and (not date or not date.startswith(f"{month.group(1)}-{int(month.group(2)):02d}")):
                    local_reasons.append("stale_month")
                if expected_instruments:
                    instrument = str(_field(item, "instrument", "") or "")
                    if not instrument:
                        local_reasons.append("missing_instrument")
                    elif not any(_same_instrument(instrument, value) for value in expected_instruments):
                        local_reasons.append("unmatched_instrument")
                source = str(_field(item, "source", "") or "").strip().lower()
                authority = str(_field(item, "authority", "") or "").strip().lower()
                placeholders = {"", "unknown", "none", "null", "fake", "forged", "synthetic", "test"}
                forged_marker = re.compile(r"(?:^|[-_ ])(?:fake|forged|synthetic|mock|dummy|test)(?:$|[-_ ])")
                # Terminal verification needs both provenance dimensions.  A
                # valid-looking authority cannot repair a forged/missing
                # source, and vice versa.
                if source in placeholders or forged_marker.search(source):
                    local_reasons.extend(("missing_source", "unknown_source"))
                if authority in placeholders or forged_marker.search(authority):
                    local_reasons.append("missing_authority")
                if not str(_field(item, "retrieved_at", "") or "").strip():
                    local_reasons.append("missing_retrieved_at")
                completeness = str(_field(item, "completeness", "") or "").strip().lower()
                coverage = str(_field(item, "coverage", "") or "").strip().lower()
                single_news = _single_news_evidence_allowed(
                    item, is_partial=is_partial, source_tool=source_tool, claim=text, claim_type=claim_type
                )
                # datum 自身的 partial 永远拒绝；result_partial 只在上面的
                # "全市场/完整/排名/最强"穷举型断言里拦截，不再拖累带日期的窄事实。
                if is_partial:
                    local_reasons.append("partial_only")
                if completeness not in {"complete", "full"} and not single_news:
                    local_reasons.append(
                        "unknown_completeness" if completeness in {"", "unknown"} else "incomplete_completeness"
                    )
                if coverage in {"", "unknown", "partial", "sample", "insufficient", "unverified_instrument"}:
                    if coverage in {"", "unknown"}:
                        local_reasons.extend(("unknown_coverage", "insufficient_coverage"))
                    else:
                        local_reasons.append("insufficient_coverage")
            claim_dates = _claim_explicit_dates(text, request_date or planned_date)
            evidence_date = date or ""
            current_wording = bool(re.search(r"今日|今天|当日|当天", text))
            market_claim = _claim_needs_price(text)
            if current_wording and market_claim and (
                market_closed is True or (request_date and planned_date and request_date != planned_date)
            ):
                local_reasons.append("closed_day_current_market_claim")
            if claim_dates and evidence_date and evidence_date not in claim_dates:
                local_reasons.append("stale_date")
            elif (
                planned_date
                and _claim_needs_date(text)
                and evidence_date
                and (not claim_dates or current_wording)
                and evidence_date != (request_date if current_wording and not market_claim and request_date else planned_date)
            ):
                local_reasons.append("stale_date")
            source = _field(item, "source") or _field(item, "authority")
            if (market_claim or re.search(r"新闻|消息|公告|报道", text)) and (not source or str(source).lower() == "unknown"):
                local_reasons.append("unknown_source")
            if market_claim and _field(item, "coverage") == "unverified_instrument":
                local_reasons.append("unknown_returned_instrument")
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
                metric_key = _market_metric_value(item)[0]
                family = "direction" if re.search(r"(?:^|[.\[\]])(?:change|change_pct|pct_change|return|涨跌|涨幅|跌幅)(?:$|[._\[\]])", metric_key) else "price" if re.search(r"(?:^|[.\[\]])(?:price|last|close)(?:$|[._\[\]])", metric_key) else "other"
                groups.setdefault((date_key, instrument_key, family), []).append(item)
            direction_claim = bool(re.search(r"上涨|下跌|收涨|收跌|涨幅|跌幅|涨跌幅", text))
            percent_claim = bool(re.search(r"(?:上涨|下跌|收涨|收跌|涨幅|跌幅|涨跌幅)\s*[+-]?\d", text))
            price_claim = bool(re.search(r"(?:价格|股价|报价|收盘价|点位|指数值)\s*(?:为|是|达|到)?\s*[+-]?\d", text))
            direction_group_seen = False
            for (_group_date, _group_instrument, family), group_items in groups.items():
                # A price/previous-close datum cannot itself prove a direction
                # predicate.  The old all-groups check therefore rejected a
                # valid claim whenever its cited evidence also contained price
                # fields.  Evaluate each predicate only in its metric family;
                # require a direction family separately when the claim needs it.
                if family == "direction":
                    direction_group_seen = True
                predicate_reasons = [_market_predicate_reason(text, item) for item in group_items]
                numeric_reasons = [_numeric_claim_reason(text, item) for item in group_items]
                if family == "direction" or not direction_claim:
                    if all(reason is not None for reason in predicate_reasons):
                        predicate_failures.append(next((reason for reason in predicate_reasons if reason == "contradictory_direction"), predicate_reasons[0]))
                if (family == "direction" and percent_claim) or (family == "price" and price_claim) or (family == "other" and not (percent_claim or price_claim)):
                    if all(reason is not None for reason in numeric_reasons):
                        predicate_failures.append(next((reason for reason in numeric_reasons if reason == "numeric_mismatch"), numeric_reasons[0]))
                # Multiple cited observations of the same subject/date/family
                # must not silently resolve in favor of whichever fits.
                if any(reason is None for reason in predicate_reasons) and "contradictory_direction" in predicate_reasons:
                    predicate_failures.append("conflicting_evidence")
                if any(reason is None for reason in numeric_reasons) and "numeric_mismatch" in numeric_reasons:
                    predicate_failures.append("conflicting_evidence")
            if direction_claim and not direction_group_seen:
                predicate_failures.append("missing_direction_metric")
        # A single instrument price observation does not establish a causal,
        # comparative, ranked, or market-wide inference.  Keep this claim
        # scoped so a direct fact (close/volume/change) remains admissible.
        if candidates and claim_type in {"inference", "comparison", "causation"}:
            broad_inference = re.search(
                r"导致|带动|引发|造成|因为|由于|相比|相较|高于|低于|超过|排名|领先|第一|最强|主线|全市场|所有|全部|市场情绪|资金流向",
                text,
            )
            if broad_inference and len(candidates) < 2:
                predicate_failures.append("inference_scope_exceeded")
        if not ids:
            hard_reasons.append("missing_reference")
        # Analytical callers may keep a contextual citation alongside a valid
        # quote. Strict delivery preserves every cited datum's scope/provenance
        # failures; a good citation cannot hide a bad one.
        reasons = hard_reasons + predicate_failures + (candidate_reasons if strict_provenance or not candidates else [])
        rows.append({"claim": text, "evidence_ids": ids, "matched_evidence_ids": matched, "supported": bool(candidates) and not reasons, "reason_codes": list(dict.fromkeys(reasons))})
    return rows
