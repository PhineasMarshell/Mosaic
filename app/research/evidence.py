"""Evidence Builder — 从工具调用结果构建用户友好的证据链。

核心职责：
- 过滤掉原始工具参数（exchange, symbol, interval, start, end 等）
- 聚合列表数据为统计摘要（min/max/average/count）
- 只保留有意义的市场指标（价格、成交量、高低价等）
- 生成人类可读的证据描述
"""

import re
from typing import Any

from app.gateway.normalizer import _is_eastmoney_f10_tool
from app.models.evidence import Evidence
from app.models.market import NormalizedDatum, ToolResult


# 要过滤的工具参数字段（这些是 API 调用参数，不是证据）
_FILTERED_PARAM_KEYS = {
    # 通用参数
    "exchange", "type", "interval", "start", "end", "symbol",
    "q", "keyword", "refresh", "datasets",
    "include_data", "settled_only", "limit", "offset", "direction",
    # 响应元数据
    "ok", "source", "source_used", "count", "items", "missing",
    "partial", "status", "note", "error", "detail",
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

    prices = []
    volumes = []
    timestamps = []

    for candle in candles:
        for price_key in ("c", "close", "price", "last", "h", "l", "o"):
            if price_key in candle and candle[price_key] is not None:
                try:
                    prices.append(float(candle[price_key]))
                except (ValueError, TypeError):
                    pass

        if "v" in candle and candle["v"] is not None:
            try:
                volumes.append(float(candle["v"]))
            except (ValueError, TypeError):
                pass

        if "t" in candle and candle["t"] is not None:
            timestamps.append(str(candle["t"]))

    summary: dict[str, Any] = {"count": len(candles)}

    if prices:
        summary["price_min"] = min(prices)
        summary["price_max"] = max(prices)
        summary["price_last"] = prices[-1]
        summary["price_range"] = f"{min(prices):.2f} - {max(prices):.2f}"

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


def build_evidence(results: list[ToolResult]) -> list[Evidence]:
    """从工具调用结果构建用户友好的证据链。

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
            evidence.append(Evidence(
                id=f"evidence-{counter:03d}",
                source_tool=result.tool,
                domain="unknown",
                metric="tool_status",
                value=f"Failed: {result.error or 'unknown error'}"[:200],
                timestamp=None,
                source=None,
                status="error",
                partial=False,
                note="",
            ))
            counter += 1
            continue

        if result.status in ("success", "partial"):
            # 收集所有非过滤指标的 metric-value 对
            valid_metrics: list[tuple[str, Any]] = []
            candle_metrics: list[tuple[str, Any]] = []

            for datum in result.normalized:
                if _is_tool_param(datum.metric):
                    continue

                # 检测是否是 K 线数据（路径包含 candles[N].field）
                if re.search(r"\.candles?\[\d+\]\.\w+$", datum.metric.lower()):
                    candle_metrics.append((datum.metric, datum.value))
                else:
                    valid_metrics.append((datum.metric, datum.value))

            # 如果有 K 线指标，生成摘要
            if candle_metrics:
                summary = _extract_candle_summary_from_metrics(candle_metrics)
                if summary.get("count", 0) > 0:
                    evidence.append(Evidence(
                        id=f"evidence-{counter:03d}",
                        source_tool=result.tool,
                        domain="crypto",
                        metric="candle_summary",
                        value=summary,
                        timestamp=None,
                        source=None,
                        status="success",
                        partial=False,
                        note=f"聚合了 {summary['count']} 条 K 线数据",
                    ))
                    counter += 1
                continue  # 不再添加单个 candle 指标

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

                evidence.append(Evidence(
                    id=f"evidence-{counter:03d}",
                    source_tool=result.tool,
                    domain=result.normalized[0].domain if result.normalized else "unknown",
                    metric=metric,
                    value=value,
                    timestamp=result.normalized[0].timestamp if result.normalized else None,
                    source=result.normalized[0].source if result.normalized else None,
                    status=result.status,
                    partial=result.partial,
                    note="",
                ))
                counter += 1

            # 如果 normalized 全被过滤了，尝试从 raw 提取快照摘要
            if not result.normalized or all(
                _is_tool_param(d.metric) for d in result.normalized
            ):
                if result.raw and isinstance(result.raw, dict):
                    snapshot_summary = _extract_snapshot_summary(result.raw, result.tool)
                    snapshot_summary.pop("source", None)
                    for metric, value in snapshot_summary.items():
                        evidence.append(Evidence(
                            id=f"evidence-{counter:03d}",
                            source_tool=result.tool,
                            domain="crypto",
                            metric=metric,
                            value=value,
                            timestamp=None,
                            source=result.raw.get("source_used") or result.raw.get("source"),
                            status=result.status,
                            partial=result.partial,
                            note="",
                        ))
                        counter += 1

        if not result.normalized and result.status in ("success", "partial"):
            evidence.append(Evidence(
                id=f"evidence-{counter:03d}",
                source_tool=result.tool,
                domain="unknown",
                metric="tool_status",
                value=result.status,
                timestamp=None,
                source=None,
                status=result.status,
                partial=result.partial,
                note="",
            ))
            counter += 1

    return evidence
