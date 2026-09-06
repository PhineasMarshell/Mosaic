"""Anomaly Detector — 市场异常检测系统。

核心职责：
- 定义各指标的"正常范围"阈值
- 对每个 ToolResult / NormalizedDatum 执行规则匹配
- 产出结构化 AnomalyRecord，分级（Low / Medium / High / Critical）
- 与 MarketDetective 主流程集成

异常类型参考 PRD §29-30：
- OI 异常：Open Interest 短时间快速增加
- 涨停异常：A股涨停数量异常扩张或收缩
- Funding 异常：资金费率突然飙升
- Liquidation 异常：大额清算事件
- 题材异常：某主题突然出现大量首板
- 情绪异常：市场情绪快速变化
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# 数据模型                                                             #
# ------------------------------------------------------------------ #


@dataclass
class AnomalyRecord:
    """单条异常记录。"""
    id: str
    type: str              # oi_spike, liquidation_wave, limit_up_surge 等
    domain: str            # a_share | crypto | ...
    severity: str          # low | medium | high | critical
    description: str       # 人类可读描述
    metric: str            # 触发的指标名称
    value: Any             # 当前值
    normal_range: str      # 正常范围描述
    possible_meaning: str  # 可能含义
    status: str = "investigating"  # investigating | confirmed | false_positive
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "domain": self.domain,
            "severity": self.severity,
            "description": self.description,
            "metric": self.metric,
            "value": self.value,
            "normal_range": self.normal_range,
            "possible_meaning": self.possible_meaning,
            "status": self.status,
            "timestamp": self.timestamp,
        }


# ------------------------------------------------------------------ #
# 规则定义                                                             #
# ------------------------------------------------------------------ #


class _Rule:
    """单个检测规则。"""
    def __init__(
        self,
        rule_id: str,
        rule_type: str,
        domain: str,
        metric_pattern: str,   # 用于匹配的指标名关键词
        threshold: float,       # 触发阈值
        direction: str,         # "gt" (大于) | "lt" (小于)
        severity: str,
        description_template: str,
        possible_meaning: str,
        normal_range: str,
    ):
        self.rule_id = rule_id
        self.rule_type = rule_type
        self.domain = domain
        self.metric_pattern = metric_pattern
        self.threshold = threshold
        self.direction = direction
        self.severity = severity
        self.description_template = description_template
        self.possible_meaning = possible_meaning
        self.normal_range = normal_range


# Crypto 衍生品异常检测规则
_CRYPTO_RULES: list[_Rule] = [
    _Rule(
        "oi_spike_high",
        "oi_spike",
        "crypto",
        "openInterest|open_interest|oi",
        5.0,  # > 5% 日变化 → High
        "gt",
        "high",
        "OI 短时间内快速增加 {:.1f}%",
        "杠杆多头/空头资金快速增加，市场预期加剧",
        "OI 日变化 < ±5%",
    ),
    _Rule(
        "oi_spike_critical",
        "oi_spike",
        "crypto",
        "openInterest|open_interest|oi",
        10.0,  # > 10% → Critical
        "gt",
        "critical",
        "OI 剧烈增长 {:.1f}% — 可能出现大幅波动",
        "大规模新增杠杆头寸，随时可能引发连锁清算",
        "OI 日变化 < ±5%",
    ),
    _Rule(
        "funding_rate_high",
        "funding_rate_anomaly",
        "crypto",
        "fundingRate|funding_rate|funding",
        0.15,  # > 0.15% → High
        "gt",
        "high",
        "资金费率大幅走高 {:.4f}%",
        "多头支付高额溢价，市场过度拥挤在多头方向",
        "资金费率 |daily annualized| < 0.1%",
    ),
    _Rule(
        "funding_rate_critical",
        "funding_rate_anomaly",
        "crypto",
        "fundingRate|funding_rate|funding",
        0.5,  # > 0.5% → Critical
        "gt",
        "critical",
        "资金费率极端高位 {:.4f}% — 极度拥挤",
        "市场严重单边化，潜在清算风险极高",
        "资金费率 |daily annualized| < 0.1%",
    ),
    _Rule(
        "liquidation_spike",
        "liquidation_event",
        "crypto",
        "liquidation|totalLiq|liqValue|liquidated",
        5_000_000,  # > $5M → High
        "gt",
        "high",
        "当日清算金额达 ${:.0f}",
        "杠杆多头被强制平仓，可能触发进一步清算",
        "单日清算 < $1M",
    ),
    _Rule(
        "liquidation_massive",
        "liquidation_event",
        "crypto",
        "liquidation|totalLiq|liqValue|liquidated",
        20_000_000,  # > $20M → Critical
        "gt",
        "critical",
        "大规模清算事件发生，${:.0f} 被清算",
        "系统性杠杆爆仓，市场可能出现短期剧烈波动",
        "单日清算 < $1M",
    ),
]

# A 股情绪异常检测规则
_ASHARE_RULES: list[_Rule] = [
    _Rule(
        "limit_up_extreme_expand",
        "limit_up_surge",
        "a_share",
        "涨停|limitUp|limit_up|上涨家数",
        80,  # > 80 家涨停 → High
        "gt",
        "high",
        "涨停家数异常扩张至 {} 家",
        "市场风险偏好显著提升，活跃资金高度集中",
        "涨停家数 30-60 家",
    ),
    _Rule(
        "limit_up_extreme_squeeze",
        "limit_up_collapse",
        "a_share",
        "涨停|跌停|limitUp|limit_down|下跌家数",
        20,  # < 20 涨停 → High (inverted)
        "lt",
        "high",
        "涨停家数急剧收缩至 {} 家以下",
        "市场风险偏好显著下降，短线资金撤退",
        "涨停家数 > 30 家",
    ),
    _Rule(
        "sentiment_drop",
        "sentiment_change",
        "a_share",
        "sentiment|riskOn|riskOff|risk_pref",
        0.3,  # drop > 30% → Medium
        "lt",
        "medium",
        "市场情绪指数骤降 {:.1f}% (或跌至 {:.2f})",
        "市场整体风险偏好快速降温",
        "情绪指数日变化 < ±15%",
    ),
    _Rule(
        "market_breadth_wide_decline",
        "sentiment_change",
        "a_share",
        "下跌家数|downCount|declining|下跌",
        3000,  # > 3000 只下跌 → High
        "gt",
        "high",
        "{} 只股票下跌，市场宽度极差",
        "全面抛售，缺乏承接力量",
        "下跌家数 < 1500 只",
    ),
]


ALL_RULES: list[_Rule] = _CRYPTO_RULES + _ASHARE_RULES

_counter = 0


def _next_id() -> str:
    global _counter
    _counter += 1
    return f"anomaly-{_counter:03d}"


def _matches_metric(rule: _Rule, metric: str) -> bool:
    """检查指标名是否匹配规则模式。"""
    if not metric:
        return False
    pattern_lower = rule.metric_pattern.lower()
    metric_lower = metric.lower()
    return any(p in metric_lower for p in pattern_lower.split("|"))


def _extract_numeric(value: Any) -> float | None:
    """从值中提取数值。"""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            # Handle format like "1,234,567" or "$1.2M"
            cleaned = value.replace(",", "").replace("$", "").replace("%", "")
            if cleaned.endswith("M"):
                return float(cleaned.rstrip("M")) * 1_000_000
            if cleaned.endswith("B"):
                return float(cleaned.rstrip("B")) * 1_000_000_000
            return float(cleaned)
        except (ValueError, TypeError):
            return None
    return None


def detect_anomalies(
    tool_results: list[Any],
    domain: str = "unknown",
) -> list[AnomalyRecord]:
    """检测一组工具结果中的市场异常。

    Args:
        tool_results: ToolResult 对象列表（每个有 .normalized 属性含 NormalizedDatum）
        domain: 目标市场域

    Returns:
        AnomalyRecord 列表（按严重程度降序）
    """
    global _counter
    results: list[AnomalyRecord] = []

    rules = [r for r in ALL_RULES if r.domain == domain or r.domain == "cross"]

    for result in tool_results:
        normalized = getattr(result, "normalized", [])
        if not normalized:
            continue

        for datum in normalized:
            metric = getattr(datum, "metric", "") or ""
            value = getattr(datum, "value", None)
            source_tool = getattr(result, "tool", "unknown")

            num_value = _extract_numeric(value)
            if num_value is None:
                continue

            for rule in rules:
                if not _matches_metric(rule, metric):
                    continue

                triggered = False
                if rule.direction == "gt" and num_value > rule.threshold:
                    triggered = True
                elif rule.direction == "lt" and num_value < rule.threshold:
                    triggered = True

                if triggered:
                    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
                    record = AnomalyRecord(
                        id=_next_id(),
                        type=rule.rule_type,
                        domain=getattr(datum, "domain", domain),
                        severity=rule.severity,
                        description=rule.description_template.format(num_value),
                        metric=metric,
                        value=num_value,
                        normal_range=rule.normal_range,
                        possible_meaning=rule.possible_meaning,
                        timestamp=ts,
                        status="investigating",
                    )
                    results.append(record)
                    logger.info(
                        "Anomaly detected: %s [%s] %s=%.2f threshold=%s %s",
                        rule.rule_id, rule.severity, metric, num_value,
                        rule.threshold, rule.possible_meaning,
                    )

    # Sort by severity: critical > high > medium > low
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    results.sort(key=lambda r: severity_order.get(r.severity, 99))

    if results:
        logger.info(
            "Detected %d anomalies out of %d tool results",
            len(results), len(tool_results),
        )

    return results
