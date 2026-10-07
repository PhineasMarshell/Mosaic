"""Evidence Builder — 从工具调用结果构建用户友好的证据链。

核心职责：
- 过滤掉原始工具参数（exchange, symbol, interval, start, end 等）
- 聚合列表数据为统计摘要（min/max/average/count）
- 只保留有意义的市场指标（价格、成交量、高低价等）
- 生成人类可读的证据描述

性能注意：单次 klines 调用能产生 300+ NormalizedDatum，如果不设上限会
向 evaluator/reasoning LLM 发送 149KB 的 JSON prompt。这里设硬上限防止这种
情况——超出上限的条目被截断而不是全部塞进 prompt。
"""

import re
from typing import Any

from app.gateway.normalizer import _is_eastmoney_f10_tool, infer_domain_from_tool
from app.models.evidence import Evidence
from app.models.market import ToolResult

# ── 上限常量 ────────────────────────────────────
_MAX_EVIDENCE_ITEMS = 80


def _evidence_domain(result: ToolResult) -> str:
    """B7：证据 domain 按数据/工具推断，禁止硬编码。

    K 线摘要与快照摘要兜底分支曾写死 ``domain="crypto"`` —— 任何域的
    K 线/快照证据都被标成 crypto（A 股、美股全中）。这里先取 datum 自带
    的 domain（同非 K 线分支的取法），拿不到再按 operationId 推断
    （``infer_domain_from_tool`` 自带 "unknown" 兜底，不会返回 None/空串）。
    """
    datum_domain = result.normalized[0].domain if result.normalized else None
    return datum_domain or infer_domain_from_tool(result.tool)


# ── 过滤键：原始工具参数（不是证据）─────────────
_FILTERED_PARAM_KEYS = {
    "exchange",
    "type",
    "interval",
    "start",
    "end",
    "symbol",
    "q",
    "keyword",
    "refresh",
    "datasets",
    "include_data",
    "settled_only",
    "limit",
    "offset",
    "direction",
    "ok",
    "source",
    "source_used",
    "items",
    "missing",
    "partial",
    "status",
    "note",
    "error",
    "detail",
}


def _is_tool_param(metric: str) -> bool:
    """判断 metric 是否是工具参数（应被过滤）。"""
    metric_lower = metric.lower()
    if metric_lower in _FILTERED_PARAM_KEYS:
        return True
    if "." in metric_lower:
        parts = metric_lower.split(".")
        last_part = parts[-1]
        if last_part in _FILTERED_PARAM_KEYS:
            return True
    return False


def _extract_candle_summary_from_metrics(
    metrics: list[tuple[str, Any]],
) -> dict[str, Any]:
    """从指标列表中提取 K 线摘要（指标格式：xxx.candles[N].field）。"""
    if not metrics:
        return {"count": 0}

    # 按 candle index 分组
    candle_groups: dict[int, dict[str, Any]] = {}
    candle_pattern = re.compile(r"candles?\[(\d+)\]\.(\w+)$")

    for metric, value in metrics:
        match = candle_pattern.search(metric.lower())
        if match:
            idx = int(match.group(1))
            field = match.group(2)
            if idx not in candle_groups:
                candle_groups[idx] = {}
            candle_groups[idx][field] = value

    if not candle_groups:
        return {"count": 0}

    candles = sorted(candle_groups.values(), key=lambda c: c.get("t", 0))

    # T7：每根 K 线只取**一个**收盘价，按 c/close/last/price 优先级取第一个存在的键；
    # h/l 仅用于区间高低点，不参与 price_last。旧实现把 o/h/l/c 全部 append，
    # price_last=prices[-1] 会取到开盘价（真实最新收盘被覆盖）。
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    volumes = []
    timestamps = []

    for candle in candles:
        close: float | None = None
        for price_key in ("c", "close", "last", "price"):
            if price_key in candle and candle[price_key] is not None:
                try:
                    close = float(candle[price_key])
                    break
                except (ValueError, TypeError):
                    pass
        if close is None:
            continue
        closes.append(close)
        # 高/低点优先取 h/l，缺失时退化为该根收盘。
        highs.append(float(candle["h"]) if candle.get("h") is not None else close)
        lows.append(float(candle["l"]) if candle.get("l") is not None else close)

        if "v" in candle and candle["v"] is not None:
            try:
                volumes.append(float(candle["v"]))
            except (ValueError, TypeError):
                pass

        if "t" in candle and candle["t"] is not None:
            timestamps.append(str(candle["t"]))

    summary: dict[str, Any] = {"count": len(candles)}

    if closes:
        summary["price_min"] = min(lows)
        summary["price_max"] = max(highs)
        # price_last 明确取**最后一根的收盘价**。
        summary["price_last"] = closes[-1]
        summary["price_range"] = f"{min(lows):.2f} - {max(highs):.2f}"

    if volumes:
        summary["volume_total"] = sum(volumes)
        summary["volume_avg"] = sum(volumes) / len(volumes)

    if timestamps:
        summary["timestamp_first"] = timestamps[0]
        summary["timestamp_last"] = timestamps[-1]

    return summary


def _extract_snapshot_summary(response: dict[str, Any], tool: str) -> dict[str, Any]:
    """从快照响应中提取摘要。"""
    summary: dict[str, Any] = {"source": response.get("source", tool)}

    for key in ("price", "lastPrice", "last_price", "markPrice", "mark_price"):
        if key in response and response[key] is not None:
            try:
                summary["price"] = float(response[key])
            except (ValueError, TypeError):
                pass
            break

    for key in ("high", "highPrice", "high_price", "high24h"):
        if key in response and response[key] is not None:
            try:
                summary["high"] = float(response[key])
            except (ValueError, TypeError):
                pass
            break

    for key in ("low", "lowPrice", "low_price"):
        if key in response and response[key] is not None:
            try:
                summary["low"] = float(response[key])
            except (ValueError, TypeError):
                pass
            break

    for key in ("open", "openPrice", "open_price"):
        if key in response and response[key] is not None:
            try:
                summary["open"] = float(response[key])
            except (ValueError, TypeError):
                pass
            break

    for key in ("change", "change_pct", "changePercent"):
        if key in response and response[key] is not None:
            try:
                summary["change_pct"] = float(response[key])
            except (ValueError, TypeError):
                pass
            break

    for key in ("volume", "volume_24h", "quotedVolume", "volume_traded"):
        if key in response and response[key] is not None:
            try:
                summary["volume"] = float(response[key])
            except (ValueError, TypeError):
                pass
            break

    return summary


def _cap_evidence(evidence: list[Evidence], cap: int) -> list[Evidence]:
    """把证据链截到 ``cap`` 条以内（T6，方案 A）。

    - 按 ``source_tool`` 分组；组内末尾代表最新（K 线/列表均时间升序）。
    - 轮转地从每个来源取其最新条目，直到达到 ``cap``，保证每个来源尽量都有代表、
      且优先保留最新数据；最后恢复原始相对顺序。
    - 被截断时把说明写进保留条目的 ``note``，不额外增加条目（保证总量 ≤ cap）。
    """
    if len(evidence) <= cap:
        return evidence

    original_total = len(evidence)

    groups: dict[str, list[tuple[int, Evidence]]] = {}
    order: list[str] = []
    for position, item in enumerate(evidence):
        if item.source_tool not in groups:
            groups[item.source_tool] = []
            order.append(item.source_tool)
        groups[item.source_tool].append((position, item))

    # 每组转成"最新优先"。
    newest_first = {source: list(reversed(pairs)) for source, pairs in groups.items()}

    kept_pairs: list[tuple[int, Evidence]] = []
    while len(kept_pairs) < cap:
        progressed = False
        for source in order:
            if newest_first[source]:
                kept_pairs.append(newest_first[source].pop(0))
                progressed = True
                if len(kept_pairs) >= cap:
                    break
        if not progressed:
            break

    kept_pairs.sort(key=lambda pair: pair[0])
    kept = [item for _, item in kept_pairs]

    dropped = original_total - len(kept)
    truncation_note = f"证据链超出 {cap} 条上限，已按来源保留最新 {len(kept)} 条（截断 {dropped} 条）"
    target = kept[-1]
    target.note = f"{target.note}；{truncation_note}" if target.note else truncation_note
    return kept


def build_evidence(results: list[ToolResult], id_prefix: str = "evidence") -> list[Evidence]:
    """从工具调用结果构建用户友好的证据链。

    Args:
        results: 一个 analyst 产出的工具结果。
        id_prefix: 证据 id 前缀。各 analyst 必须传入自己的 category（如
            "technical"），否则多个 analyst 合并后 id 都从 evidence-001 开始、
            发生跨 analyst 重复（T4）。id 形如 ``f"{id_prefix}-{counter:03d}"``。

    过滤规则：
    - 跳过原始工具参数（exchange, symbol, interval 等）
    - K 线数据聚合为统计摘要
    - 快照数据提取关键指标
    - 错误工具只记录状态，不展示错误详情
    """
    evidence: list[Evidence] = []
    counter = 1

    for result in results:
        if result.status == "error":
            evidence.append(
                Evidence(
                    id=f"{id_prefix}-{counter:03d}",
                    source_tool=result.tool,
                    domain="unknown",
                    metric="tool_status",
                    value=f"Failed: {result.error or 'unknown error'}"[:200],
                    timestamp=None,
                    source=None,
                    status="error",
                    partial=False,
                    note="",
                )
            )
            counter += 1
            continue

        if result.status in ("success", "partial"):
            # 收集所有非过滤指标的 metric-value 对
            valid_metrics: list[tuple[str, Any]] = []
            candle_metrics: list[tuple[str, Any]] = []

            for datum in result.normalized:
                if _is_tool_param(datum.metric):
                    continue

                # 检测是否是 K 线数据（路径格式：candles[N].field 或 candles?[N].field）
                # ⚠️ normalizer 发出的顶层路径没有前导点（如 "candles[0].c"），
                # 以前这里写的是 r"\.candles?" → 永远匹配不上，K 线摘要全部失效。
                if re.search(r"candles?\[\d+\]\.\w+$", datum.metric.lower()):
                    candle_metrics.append((datum.metric, datum.value))
                else:
                    valid_metrics.append((datum.metric, datum.value))

            # 如果有 K 线指标，生成摘要
            if candle_metrics:
                summary = _extract_candle_summary_from_metrics(candle_metrics)
                if summary.get("count", 0) > 0:
                    evidence.append(
                        Evidence(
                            id=f"{id_prefix}-{counter:03d}",
                            source_tool=result.tool,
                            domain=_evidence_domain(result),
                            metric="candle_summary",
                            value=summary,
                            timestamp=None,
                            source=None,
                            status="success",
                            partial=False,
                            note=f"聚合了 {summary['count']} 条 K 线数据",
                        )
                    )
                    counter += 1
                # T7：不再 continue——混合型 payload 里的非 K 线指标（如
                # openInterest）必须照常写入；单个 candle 指标已聚合进 summary，
                # valid_metrics 本就不含 candle 项，不会重复添加。

            # 添加其他有效指标
            for metric, value in valid_metrics:
                # F10 数据：尝试进一步解析不可读值（dict/list → 数值）
                if _is_eastmoney_f10_tool(result.tool) and isinstance(value, (dict, list)):
                    from app.gateway.normalizer import _deep_flatten_value

                    flattened = _deep_flatten_value(value)
                    if isinstance(flattened, (int, float)):
                        value = flattened
                    else:
                        value = str(value)[:200]
                elif isinstance(value, (dict, list)):
                    value = str(value)[:200]

                evidence.append(
                    Evidence(
                        id=f"{id_prefix}-{counter:03d}",
                        source_tool=result.tool,
                        domain=result.normalized[0].domain if result.normalized else "unknown",
                        metric=metric,
                        value=value,
                        timestamp=result.normalized[0].timestamp if result.normalized else None,
                        source=result.normalized[0].source if result.normalized else None,
                        status=result.status,
                        partial=result.partial,
                        note="",
                    )
                )
                counter += 1

            # 如果 normalized 全被过滤了，尝试从 raw 提取快照摘要
            if not result.normalized or all(_is_tool_param(d.metric) for d in result.normalized):
                if result.raw and isinstance(result.raw, dict):
                    snapshot_summary = _extract_snapshot_summary(result.raw, result.tool)
                    snapshot_summary.pop("source", None)
                    for metric, value in snapshot_summary.items():
                        evidence.append(
                            Evidence(
                                id=f"{id_prefix}-{counter:03d}",
                                source_tool=result.tool,
                                domain=_evidence_domain(result),
                                metric=metric,
                                value=value,
                                timestamp=None,
                                source=result.raw.get("source_used") or result.raw.get("source"),
                                status=result.status,
                                partial=result.partial,
                                note="",
                            )
                        )
                        counter += 1

        # T1：normalized 为空的 success/partial 结果，绝不能再伪造一条
        # metric="tool_status"、value="success" 的"证据"——那会让零数据点的结果
        # 通过 Evidence Gate。normalizer 现在已保证空结果必为 status=error（在上方
        # error 分支处理并 continue）；若此处仍遇到直接构造的空结果，按"无证据"跳过。

    # T6：文档承诺的硬上限现在真正生效。
    return _cap_evidence(evidence, _MAX_EVIDENCE_ITEMS)
