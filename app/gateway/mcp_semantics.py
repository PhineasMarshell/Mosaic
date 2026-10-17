"""MCP 市场数据语义补标注适配器（执行单 mcp-semantic-annotation-execution-plan）。

本模块只处理 ``app/gateway/tool_registry.py`` 中登记了显式 semantic contract 的
MCP 工具（当前为 ``get_limit_up_count`` 与 ``list_limit_up_sectors``），不做通用
JSON 猜测。它的职责是把 MCP 已经返回的业务结果，按工具契约补成带标准 metric、
观察日期、来源/权威标记和逐行日期 lineage 的 NormalizedDatum，并产出一份只供审计
的 annotation sidecar（绝不进入用户报告正文，也不会被扁平化成业务 datum）。

硬约束：

- 输入 ``payload`` 及其嵌套行**绝不被修改**；适配器先深拷贝，再在副本上工作。
- ``mcp`` 只是 transport，永远不能成为 authority。
- 观察日期只能来自业务行/业务字段（observed）、可信信封（envelope）、或契约
  明确允许且确实随 wire 发送的请求日期（request_context）。抓取时间、系统当前
  日期、研究计划日期都不能用来补观察日。
- 行级日期永远优先；批次信封日期只在契约保证同一快照时传播，且不覆盖不同日期。
- 语义标注不能放宽 generic evidence-only fallback，只是把契约已知的事实标准化。
"""

from __future__ import annotations

import copy
from math import isfinite
from typing import Any

from app.gateway.tool_registry import (
    canonical_mcp_operation,
    get_semantic_contract,
)

#: datum 标准化来源（NormalizedDatum.semantic_annotation）。
SEMANTIC_ANNOTATION_SOURCES = frozenset({"contract", "payload", "request_context"})

#: 观察日期来源（NormalizedDatum.date_annotation / sidecar）。
DATE_ANNOTATION_SOURCES = frozenset(
    {"observed", "envelope", "request_context", "contract_snapshot", "unknown"}
)

#: 机器可读的日期来源（sidecar.date_source，测试与日志使用）。
DATE_SOURCES = frozenset({"raw_field", "envelope_field", "request_context", "unknown"})

#: transport/技能包装名，永远不是 provider。
_NON_PROVIDER_LABELS = frozenset({"mcp", "http", "http_api", "ths_skill", "同花顺问财"})

#: 适配器级逐项诊断码。
SEMANTIC_DIAGNOSTIC_CODES = frozenset(
    {
        "mcp_missing_source",
        "mcp_missing_authority",
        "mcp_missing_date",
        "mcp_date_conflict",
        "mcp_invalid_date",
        "mcp_request_date_ignored",
        "mcp_invalid_count",
        "mcp_count_alias_conflict",
        "mcp_missing_business_value",
        "mcp_row_missing_identity",
        "mcp_row_missing_structure",
        "mcp_empty_business_container",
    }
)

_MISSING = object()


# ------------------------------------------------------------------ #
# 基础工具                                                              #
# ------------------------------------------------------------------ #


def _read_path(payload: Any, path: tuple[str, ...]) -> Any:
    cur = payload
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return _MISSING
    return cur


def _coerce_nonneg_int(value: Any) -> int | None:
    """把家数解析为非负整数；无法无歧义解析时返回 None。

    拒绝 bool、负数、小数、NaN/Infinity、null、空字符串与"未知"。``0`` 是
    合法业务事实，不能当空结果。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if not isfinite(value) or value < 0 or float(int(value)) != value:
            return None
        return int(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text or text.casefold() in {"未知", "null", "none", "nan", "inf", "infinity"}:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        if not isfinite(number) or number < 0 or float(int(number)) != number:
            return None
        return int(number)
    return None


def _resolve_provider(payload: Any, declared_provider: str | None) -> tuple[str | None, str | None, list[str]]:
    """返回 (source_label, authority, diagnostics)。

    provider 优先取 raw root 的 source/source_used/provider，再取信封
    metadata/meta/result。authority 只来自 provider 白名单；transport 名
    永不提升为 authority。
    """
    # 延迟导入避免循环依赖。
    from app.gateway.normalizer import provider_authority

    candidates: list[str] = []
    if isinstance(payload, dict):
        for value in (payload.get("source"), payload.get("source_used"), payload.get("provider")):
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
        for envelope in (payload.get("metadata"), payload.get("meta"), payload.get("result"), payload.get("data")):
            if isinstance(envelope, dict):
                for value in (envelope.get("provider"), envelope.get("source_used"), envelope.get("source")):
                    if isinstance(value, str) and value.strip():
                        candidates.append(value.strip())

    diagnostics: list[str] = []
    authority = next((value for value in candidates if provider_authority(value)), None)
    source_label: str | None = authority
    if authority is None:
        declared = declared_provider.strip() if isinstance(declared_provider, str) and declared_provider.strip() else None
        if declared and provider_authority(declared):
            authority = declared
            source_label = declared
    if source_label is None:
        # 保留非白名单、非 transport 的原始标签到 datum.source（审计可读），
        # 但 authority 仍为 None。
        source_label = next(
            (value for value in candidates if value.casefold() not in _NON_PROVIDER_LABELS),
            None,
        )

    if not candidates and not (
        isinstance(declared_provider, str) and declared_provider.strip()
    ):
        diagnostics.append("mcp_missing_source")
    if authority is None:
        diagnostics.append("mcp_missing_authority")
    return source_label, authority, diagnostics


def _normalize_date(value: Any) -> str | None:
    from app.research.trading_calendar import normalize_date

    return normalize_date(value)


def _collect_date(
    payload: Any,
    paths: tuple[tuple[str, ...], ...],
) -> tuple[str | None, str | None, bool, str | None]:
    """在给定路径集合里找观察日期。

    返回 (normalized_date, raw_path, invalid_found, raw_value)。多个不同有效
    日期由调用方按冲突处理。
    """
    found: str | None = None
    found_path: str | None = None
    invalid = False
    for path in paths:
        value = _read_path(payload, path)
        if value is _MISSING or value in (None, ""):
            continue
        normalized = _normalize_date(value)
        if normalized is None:
            invalid = True
            continue
        if found is not None and found != normalized:
            # 同一优先级内出现不同日期 → 冲突，交给调用方。
            return found, found_path, True, str(value)
        if found is None:
            found = normalized
            found_path = ".".join(path)
    return found, found_path, invalid, None


# ------------------------------------------------------------------ #
# get_limit_up_count                                                   #
# ------------------------------------------------------------------ #


def _count_business_candidates(view: Any, contract: dict[str, Any]) -> list[tuple[str, Any]]:
    """返回 (metric_path, raw_value) 候选：data.count / count 等契约允许的路径。"""
    candidates: list[tuple[str, Any]] = []
    for path in contract["count_value_paths"]:
        value = _read_path(view, path)
        if value is not _MISSING:
            candidates.append((".".join(path), value))
    return candidates


def _annotate_limit_up_count(
    *,
    canonical: str,
    contract: dict[str, Any],
    view: Any,
    datums: list[Any],
    source_label: str | None,
    authority: str | None,
    requested_as_of_date: str | None,
    diagnostics: list[str],
) -> tuple[list[Any], dict[str, Any]]:
    business: list[Any] = []
    structural: list[Any] = []

    # 1) 业务家数：找契约允许的 count 路径，校验非负整数，只生成一条 datum。
    candidates = _count_business_candidates(view, contract)
    chosen_value: int | None = None
    chosen_path: str | None = None
    raw_values: list[tuple[str, Any]] = []
    for path, value in candidates:
        raw_values.append((path, value))
        coerced = _coerce_nonneg_int(value)
        if coerced is None:
            # 路径存在但值非法（null/""/小数/负数/未知……）。
            if value not in (None, ""):
                diagnostics.append(f"mcp_invalid_count:{path}")
            continue
        if chosen_value is None:
            chosen_value = coerced
            chosen_path = path
        elif chosen_value != coerced:
            diagnostics.append("mcp_count_alias_conflict")
            chosen_value = None
            chosen_path = None
            break

    structural_paths = {".".join(p) for p in contract["structural_field_paths"]}

    # 2) 逐 datum 分类。
    for datum in datums:
        path = datum.metric
        leaf = path.rsplit(".", 1)[-1].casefold()
        # 业务家数 datum：把 data.count / count 统一成 limit_up_count。
        if chosen_value is not None and path == chosen_path and leaf == "count":
            if datum.structural_metadata:
                # 通用提取器把它误判成结构计数时纠正回来。
                datum.structural_metadata = False
            datum.metric = contract["business_metric"]
            datum.original_field_path = path
            datum.value = chosen_value
            datum.semantic_annotation = "contract"
            business.append(datum)
            continue
        # 结构元数据：root 层的 type/日期/来源/分页/计数等。
        if (not _has_row_index(path)) and (
            path in structural_paths
            or leaf in {
                "type", "code", "code_count", "total", "total_count", "row_count",
                "page", "limit", "item_count", "record_count", "date", "trade_date",
                "trading_date", "as_of_date", "timestamp", "datetime", "时间", "日期",
                "交易日", "source", "source_used", "provider", "msg", "message",
                "status", "name", "page_size", "pagesize",
            }
        ):
            datum.structural_metadata = True
            structural.append(datum)
            continue
        # 契约 count 路径上存在但值非法/冲突的字段，或通用提取器已判为结构的
        # 字段，都不能作为业务事实。
        raw_count_paths = {p for p, _ in raw_values}
        is_count_field = path in raw_count_paths or leaf == "count"
        if is_count_field or datum.structural_metadata:
            datum.structural_metadata = True
            structural.append(datum)
        else:
            # 非契约家数字段（例如 data.涨停家数）仍是业务指标，需要继续进入
            # 异常检测等下游；它不被重命名成 limit_up_count，避免伪造契约家数。
            datum.semantic_annotation = "payload"
            business.append(datum)

    if chosen_value is None:
        # 契约 count 路径存在但非法/冲突，或整条结果没有任何业务 datum 时才报缺失。
        # 仅存在其他命名的业务指标（如 data.涨停家数）不算契约家数缺失。
        if raw_values or not business:
            diagnostics.append("mcp_missing_business_value")

    # 3) 观察日期：observed（业务字段）优先，其次 envelope。
    observed, observed_path, observed_invalid, _ = _collect_date(view, contract["raw_date_paths"])
    envelope, envelope_path, envelope_invalid, _ = _collect_date(view, contract["envelope_date_paths"])
    if observed_invalid or envelope_invalid:
        diagnostics.append("mcp_invalid_date")
    as_of_date = observed
    date_annotation = "observed" if observed else None
    date_path = observed_path
    if as_of_date is None and envelope is not None:
        as_of_date = envelope
        date_annotation = "envelope"
        date_path = envelope_path
    # request_context 仅在契约允许且日期确实随 wire 发送时可用（本工具不允许）。
    if requested_as_of_date:
        if contract.get("date_from_request"):
            requested = _normalize_date(requested_as_of_date)
            if requested:
                if as_of_date is None:
                    as_of_date = requested
                    date_annotation = "request_context"
                    date_path = "arguments.as_of_date"
                elif as_of_date != requested:
                    diagnostics.append("mcp_date_conflict")
        else:
            diagnostics.append("mcp_request_date_ignored")
    if as_of_date is None:
        diagnostics.append("mcp_missing_date")
        date_annotation = "unknown"

    # 4) 给业务 datum 打日期/来源标记。
    for datum in business:
        datum.as_of_date = as_of_date
        if as_of_date:
            datum.timestamp = datum.timestamp or as_of_date
        datum.date_annotation = date_annotation
        datum.annotation_contract_id = contract["contract_id"]
        if authority:
            datum.authority = authority
        if source_label:
            datum.source = source_label

    date_source = {
        "observed": "raw_field",
        "envelope": "envelope_field",
        "request_context": "request_context",
        "unknown": "unknown",
    }[date_annotation or "unknown"]

    normalized = business + structural
    sidecar = _base_sidecar(
        canonical=canonical,
        contract=contract,
        source_label=source_label,
        authority=authority,
        as_of_date=as_of_date,
        date_annotation=date_annotation or "unknown",
        date_source=date_source,
        date_paths=[date_path] if date_path else [],
        diagnostics=diagnostics,
        business=business,
        structural=structural,
    )
    sidecar["count_value"] = chosen_value
    return normalized, sidecar


# ------------------------------------------------------------------ #
# list_limit_up_sectors                                               #
# ------------------------------------------------------------------ #


def _has_row_index(path: str) -> bool:
    return "[" in path and "]" in path


def _row_prefix(path: str) -> str:
    """``data.list[0].plate`` → ``data.list[0]``；无行索引返回 ""。"""
    index = path.rfind("]")
    if index < 0:
        return ""
    return path[: index + 1]


def _iter_sector_rows(view: Any, contract: dict[str, Any]):
    """从 payload 中找出业务行，yield (row_prefix, row_dict)。"""
    keys = contract["row_container_keys"]
    if isinstance(view, dict):
        for key in keys:
            value = view.get(key)
            if isinstance(value, list):
                for i, row in enumerate(value):
                    yield f"{key}[{i}]", row
                return
        data = view.get("data")
        if isinstance(data, dict):
            for key in keys:
                value = data.get(key)
                if isinstance(value, list):
                    for i, row in enumerate(value):
                        yield f"data.{key}[{i}]", row
                    return
        if isinstance(data, list):
            for i, row in enumerate(data):
                yield f"data[{i}]", row
    elif isinstance(view, list):
        for i, row in enumerate(view):
            yield f"[{i}]", row


def _row_identity(row: dict[str, Any], contract: dict[str, Any]) -> str | None:
    for key in contract["row_identity_fields"]:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _row_structure(row: dict[str, Any], contract: dict[str, Any]) -> bool:
    for key in contract["row_structure_fields"]:
        value = row.get(key)
        if value in (None, ""):
            continue
        if _coerce_nonneg_int(value) is not None:
            return True
        # pct/涨幅可能是小数（如 4.5），允许有限数值。
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and isfinite(float(value)):
            return True
        if isinstance(value, str):
            text = value.strip().removesuffix("%").replace(",", "")
            try:
                if isfinite(float(text)):
                    return True
            except ValueError:
                pass
    return False


def _row_date(row: dict[str, Any], contract: dict[str, Any]) -> tuple[str | None, bool]:
    """返回 (normalized_date, invalid)。"""
    for key in contract["row_date_fields"]:
        value = row.get(key)
        if value not in (None, ""):
            normalized = _normalize_date(value)
            if normalized is None:
                return None, True
            return normalized, False
    return None, False


def _annotate_limit_up_sectors(
    *,
    canonical: str,
    contract: dict[str, Any],
    view: Any,
    datums: list[Any],
    source_label: str | None,
    authority: str | None,
    requested_as_of_date: str | None,
    diagnostics: list[str],
) -> tuple[list[Any], dict[str, Any]]:
    # 1) 解析每一行的身份/结构/自有日期。
    rows: dict[str, dict[str, Any]] = {}
    row_order: list[str] = []
    own_dates: list[str] = []
    for prefix, row in _iter_sector_rows(view, contract):
        if not isinstance(row, dict):
            continue
        identity = _row_identity(row, contract)
        structure = _row_structure(row, contract)
        own_date, invalid = _row_date(row, contract)
        if invalid:
            diagnostics.append(f"mcp_invalid_date:{prefix}")
        if identity is None:
            diagnostics.append(f"mcp_row_missing_identity:{prefix}")
        if not structure:
            diagnostics.append(f"mcp_row_missing_structure:{prefix}")
        rows[prefix] = {
            "identity": identity,
            "structure": structure,
            "own_date": own_date,
        }
        row_order.append(prefix)
        if own_date:
            own_dates.append(own_date)

    # 2) 信封批次日期。
    envelope, envelope_path, envelope_invalid, _ = _collect_date(view, contract["envelope_date_paths"])
    if envelope_invalid:
        diagnostics.append("mcp_invalid_date")
    if requested_as_of_date and not contract.get("date_from_request"):
        diagnostics.append("mcp_request_date_ignored")

    # 3) 多个不同的行级日期 → 混合快照冲突（无参当日快照端点只允许同一日）。
    distinct_own = sorted(set(own_dates))
    mixed_snapshot = len(distinct_own) > 1
    envelope_own_mismatch = bool(
        envelope and distinct_own and any(value != envelope for value in distinct_own)
    )
    if mixed_snapshot:
        # 硬冲突：同一快照批出现多个不同行日期，不能当一次一致快照采纳。
        diagnostics.append("mcp_date_conflict")
    elif envelope_own_mismatch:
        # 信封日期与行级日期不同：行级日期（observed）优先级最高、不被覆盖；
        # 仅记录冲突诊断，不因此否决有自有日期的行。
        diagnostics.append("mcp_date_conflict")
    # 只有"多日混合快照"才阻断批次采纳；信封头与行日期不一致但每行日期自洽时，
    # 以行级日期为准（请求日不一致由 fallback_date_mismatch 继续拦截）。
    conflict = mixed_snapshot

    # 批次日期：契约保证同一快照、且无硬冲突时，信封日期可传播给缺自有日期的行。
    batch_date = envelope if (contract.get("batch_date") and not conflict) else None

    # 逐行日期 lineage（受信适配器生成，fallback 门禁据此判断，而不是读 raw）。
    lineage: dict[str, dict[str, Any]] = {}
    for prefix in row_order:
        info = rows[prefix]
        if info["own_date"]:
            lineage[prefix] = {"date": info["own_date"], "date_annotation": "observed", "trusted": True}
        elif batch_date:
            lineage[prefix] = {
                "date": batch_date,
                "date_annotation": "envelope",
                "trusted": True,
                "inherited_from": envelope_path,
            }
        else:
            lineage[prefix] = {"date": None, "date_annotation": "unknown", "trusted": True}

    # 4) 给扁平 datum 打行级标记（日期/partial/contract/date_annotation）。
    business: list[Any] = []
    structural: list[Any] = []
    structural_root_leaves = {
        "type", "code", "code_count", "count", "total", "total_count", "row_count",
        "item_count", "record_count", "page", "limit", "has_more",
    }
    for datum in datums:
        path = datum.metric
        if datum.structural_metadata:
            structural.append(datum)
            continue
        prefix = _row_prefix(path)
        if not prefix or prefix not in rows:
            # 无行索引的 root 字段：type/计数/分页 → 结构元数据。
            leaf = path.rsplit(".", 1)[-1].casefold()
            if leaf in structural_root_leaves:
                datum.structural_metadata = True
                structural.append(datum)
            else:
                # 未落在受信业务行内的字段不当作业务事实。
                datum.structural_metadata = True
                structural.append(datum)
            continue
        info = rows[prefix]
        line = lineage[prefix]
        # 行自身是否完整：缺身份/结构/观察日期，或混合快照冲突。
        row_incomplete = (info["identity"] is None) or (not info["structure"])
        if row_incomplete or (not line["date"]) or conflict:
            # 混合快照冲突是批次级完整性失败，整批行都不能作为一致快照证据。
            datum.partial = True
        else:
            # 完整行即使处在结果级 partial（信封声明 partial / 覆盖截断）中，
            # 其自身仍是完整、可作窄事实的 datum。
            datum.partial = False
        if line["date"]:
            datum.as_of_date = line["date"]
            datum.timestamp = datum.timestamp or line["date"]
        datum.date_annotation = line["date_annotation"]
        datum.semantic_annotation = "contract"
        datum.annotation_contract_id = contract["contract_id"]
        if authority:
            datum.authority = authority
        if source_label:
            datum.source = source_label
        business.append(datum)

    # 5) 结果级日期：所有业务 datum 解析到同一日才声明。
    all_dates = {lineage[p]["date"] for p in row_order if lineage[p]["date"]}
    if len(all_dates) == 1 and not conflict:
        result_date = next(iter(all_dates))
    elif len(all_dates) > 1:
        result_date = None
    else:
        result_date = None

    # 空业务容器：有行容器但零业务行（datas=[]），或所有行都缺业务字段。
    container = next((True for _ in _iter_sector_rows(view, contract)), False)
    if container and not row_order:
        diagnostics.append("mcp_empty_business_container")

    if result_date is None and not conflict:
        diagnostics.append("mcp_missing_date")
    elif result_date is None and conflict:
        # 冲突已经显式报错，不再重复贴一个 missing_date。
        pass

    if result_date is None and all(lineage[p]["date"] is None for p in row_order):
        diagnostics.append("mcp_missing_date")

    if result_date is not None:
        if all(lineage[p]["date_annotation"] == "observed" for p in row_order if p in lineage):
            result_date_annotation = "observed"
            date_source = "raw_field"
        elif any(lineage[p]["date_annotation"] == "observed" for p in row_order):
            result_date_annotation = "observed"
            date_source = "raw_field"
        else:
            result_date_annotation = "envelope"
            date_source = "envelope_field"
    else:
        result_date_annotation = "unknown"
        date_source = "unknown"

    # 去重诊断（保序）。
    diagnostics = list(dict.fromkeys(diagnostics))

    normalized = business + structural
    sidecar = _base_sidecar(
        canonical=canonical,
        contract=contract,
        source_label=source_label,
        authority=authority,
        as_of_date=result_date,
        date_annotation=result_date_annotation,
        date_source=date_source,
        date_paths=[envelope_path] if envelope_path else [],
        diagnostics=diagnostics,
        business=business,
        structural=structural,
    )
    sidecar["row_count"] = len(row_order)
    sidecar["row_date_lineage"] = lineage
    sidecar["batch_date"] = batch_date
    return normalized, sidecar


def _base_sidecar(
    *,
    canonical: str,
    contract: dict[str, Any],
    source_label: str | None,
    authority: str | None,
    as_of_date: str | None,
    date_annotation: str,
    date_source: str,
    date_paths: list[str],
    diagnostics: list[str],
    business: list[Any],
    structural: list[Any],
) -> dict[str, Any]:
    semantic_counts: dict[str, int] = {}
    date_annotations: dict[str, int] = {}
    for datum in business:
        sa = getattr(datum, "semantic_annotation", None)
        if sa:
            semantic_counts[sa] = semantic_counts.get(sa, 0) + 1
        da = getattr(datum, "date_annotation", None)
        if da:
            date_annotations[da] = date_annotations.get(da, 0) + 1
    return {
        "tool_name": canonical,
        "contract_id": contract["contract_id"],
        "payload_kind": contract["payload_kind"],
        "capability": contract["capability"],
        "semantic_annotation": "contract",
        "provider": authority or source_label,
        "source": source_label,
        "authority": authority,
        "authority_set": sorted({authority}) if authority else [],
        "as_of_date": as_of_date,
        "date_annotation": date_annotation,
        "date_source": date_source,
        "date_paths": date_paths,
        "business_datum_count": len(business),
        "structural_metadata_count": len(structural),
        "semantic_annotation_counts": semantic_counts,
        "date_annotations": date_annotations,
        "diagnostics": list(dict.fromkeys(diagnostics)),
    }


# ------------------------------------------------------------------ #
# 公共入口                                                             #
# ------------------------------------------------------------------ #


def annotate_mcp_payload(
    tool_name: str,
    arguments: dict[str, Any],
    payload: Any,
    *,
    declared_provider: str | None = None,
    requested_as_of_date: str | None = None,
) -> tuple[list[Any], dict[str, Any]] | None:
    """按工具契约补标注 MCP payload，返回 (normalized_datums, annotation)。

    - 深拷贝输入，原始 payload 与嵌套行不被修改。
    - 无契约（非目标工具）返回 None，调用方应回退到通用归一化。
    - 仅处理 dict/list 业务载荷；错误信封/空载荷由调用方先拦截。
    """
    canonical = canonical_mcp_operation(tool_name)
    contract = get_semantic_contract(canonical)
    if contract is None:
        return None
    if not isinstance(payload, (dict, list)):
        return None

    # 深拷贝：所有后续 flatten / 标注都在副本上，绝不触碰调用方的原始对象。
    view = copy.deepcopy(payload)
    arguments = dict(arguments or {})

    # 延迟导入避免循环依赖。
    from app.gateway.normalizer import (
        _date_index,
        _extract_metrics,
        _find_truncations,
        _truncation_paths,
        find_partial,
        find_timestamp,
        infer_domain_from_tool,
    )

    domain = infer_domain_from_tool(canonical)
    partial = find_partial(view)
    truncations = _find_truncations(view, canonical) if isinstance(view, dict) else []
    if truncations:
        partial = True
    import app.gateway.normalizer as _normalizer_module

    _normalizer_module._TRUNCATED_CONTAINER_PATHS = _truncation_paths(truncations)
    timestamp = find_timestamp(view) if isinstance(view, dict) else None

    source_label, authority, provider_diags = _resolve_provider(view, declared_provider)

    datums = _extract_metrics(
        view,
        _tool=canonical,
        _domain=domain,
        _status="partial" if partial else "success",
        _partial=partial,
        _timestamp=timestamp,
        _source=authority,
        _date_index=_date_index(view),
    )

    if contract["payload_kind"] == "limit_up_count":
        normalized, sidecar = _annotate_limit_up_count(
            canonical=canonical,
            contract=contract,
            view=view,
            datums=datums,
            source_label=source_label,
            authority=authority,
            requested_as_of_date=requested_as_of_date,
            diagnostics=list(provider_diags),
        )
    else:
        normalized, sidecar = _annotate_limit_up_sectors(
            canonical=canonical,
            contract=contract,
            view=view,
            datums=datums,
            source_label=source_label,
            authority=authority,
            requested_as_of_date=requested_as_of_date,
            diagnostics=list(provider_diags),
        )

    return normalized, sidecar


def semantic_annotation_diagnostics(
    tool_name: str,
    payload: Any,
    arguments: dict[str, Any],
    *,
    declared_provider: str | None = None,
    requested_as_of_date: str | None = None,
) -> dict[str, Any]:
    """只跑语义标注并返回诊断 sidecar（便于在不接 ToolResult 时直接审计）。"""
    result = annotate_mcp_payload(
        tool_name,
        arguments,
        payload,
        declared_provider=declared_provider,
        requested_as_of_date=requested_as_of_date,
    )
    if result is None:
        return {
            "tool_name": canonical_mcp_operation(tool_name),
            "contract_id": None,
            "diagnostics": ["mcp_no_contract"],
        }
    _datums, sidecar = result
    return sidecar
