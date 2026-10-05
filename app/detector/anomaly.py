"""Anomaly Detector — 市场异常检测系统。

核心职责：
- 定义各指标的"正常范围"阈值
- 对每个 ToolResult / NormalizedDatum 执行规则匹配
- 产出结构化 AnomalyRecord，分级（Low / Medium / High / Critical）
- 与研究主流程集成

异常类型参考 PRD §29-30：
- OI 异常：Open Interest 短时间快速增加
- 涨停异常：A股涨停数量异常扩张或收缩
- Funding 异常：资金费率突然飙升
- Liquidation 异常：大额清算事件
- 题材异常：某主题突然出现大量首板
- 情绪异常：市场情绪快速变化
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.cache import market_cache

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# 数据模型                                                             #
# ------------------------------------------------------------------ #


@dataclass
class AnomalyRecord:
    """单条异常记录。"""

    id: str
    type: str  # oi_spike, liquidation_wave, limit_up_surge 等
    domain: str  # a_share | crypto | ...
    severity: str  # low | medium | high | critical
    description: str  # 人类可读描述
    metric: str  # 触发的指标名称
    value: Any  # 当前值
    normal_range: str  # 正常范围描述
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
    """单个检测规则。

    ``value_semantics``（T22）：
    - ``absolute``：``value`` 本身就是要比较的量（清算金额、家数）；
    - ``pct_change``：``value`` 是绝对量，必须与同一 metric 的**前值**算 % 变化再比阈值；
      拿不到前值就不触发（宁可不报不要误报——真实 OI ≥1e9，直接比百分比阈值每条都误报）；
    - ``ratio_to_percent``：``value`` 是小数形式的比率（fundingRate 0.002 = 0.2%），
      先 ×100 转成百分比再比阈值。
    """

    def __init__(
        self,
        rule_id: str,
        rule_type: str,
        domain: str,
        metric_pattern: str,  # 精确匹配的指标名别名表（| 分隔，大小写不敏感）
        threshold: float,  # 触发阈值（与 value_semantics 对应的单位）
        direction: str,  # "gt" (大于) | "lt" (小于)
        severity: str,
        description_template: str,
        possible_meaning: str,
        normal_range: str,
        value_semantics: str = "absolute",
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
        self.value_semantics = value_semantics


# Crypto 衍生品异常检测规则
# T22：metric_pattern 是**精确别名表**（旧实现的 'oi' 子串匹配会把 noise、
# openInterestRate 之类全命中）；OI 规则语义改为**对前值的 % 变化**；
# fundingRate 语义是"小数比率 → 百分比"（0.002 = 0.2%）。
_CRYPTO_RULES: list[_Rule] = [
    _Rule(
        "oi_spike_high",
        "oi_spike",
        "crypto",
        "openInterest|open_interest|totalOi|openInterestCurrent",
        5.0,  # 日变化 > 5% → High
        "gt",
        "high",
        "OI 短时间内快速增加 {:.1f}%",
        "杠杆多头/空头资金快速增加，市场预期加剧",
        "OI 日变化 < ±5%",
        value_semantics="pct_change",
    ),
    _Rule(
        "oi_spike_critical",
        "oi_spike",
        "crypto",
        "openInterest|open_interest|totalOi|openInterestCurrent",
        10.0,  # 日变化 > 10% → Critical
        "gt",
        "critical",
        "OI 剧烈增长 {:.1f}% — 可能出现大幅波动",
        "大规模新增杠杆头寸，随时可能引发连锁清算",
        "OI 日变化 < ±5%",
        value_semantics="pct_change",
    ),
    _Rule(
        "funding_rate_high",
        "funding_rate_anomaly",
        "crypto",
        "fundingRate|funding_rate",
        0.15,  # > 0.15% → High
        "gt",
        "high",
        "资金费率大幅走高 {:.4f}%",
        "多头支付高额溢价，市场过度拥挤在多头方向",
        "资金费率 < 0.1%",
        value_semantics="ratio_to_percent",
    ),
    _Rule(
        "funding_rate_critical",
        "funding_rate_anomaly",
        "crypto",
        "fundingRate|funding_rate",
        0.5,  # > 0.5% → Critical
        "gt",
        "critical",
        "资金费率极端高位 {:.4f}% — 极度拥挤",
        "市场严重单边化，潜在清算风险极高",
        "资金费率 < 0.1%",
        value_semantics="ratio_to_percent",
    ),
    _Rule(
        "liquidation_spike",
        "liquidation_event",
        "crypto",
        "liquidation|totalLiq|totalLiqValue|liqValue|liquidated",
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
        "liquidation|totalLiq|totalLiqValue|liqValue|liquidated",
        20_000_000,  # > $20M → Critical
        "gt",
        "critical",
        "大规模清算事件发生，${:.0f} 被清算",
        "系统性杠杆爆仓，市场可能出现短期剧烈波动",
        "单日清算 < $1M",
    ),
]

# A 股情绪异常检测规则（家数类是绝对量，阈值本来就是绝对值——语义一致）
_ASHARE_RULES: list[_Rule] = [
    _Rule(
        "limit_up_extreme_expand",
        "limit_up_surge",
        "a_share",
        "涨停家数|涨停|limitUpCount|limitUp|limit_up_count",
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
        "涨停家数|涨停|跌停|limitUpCount|limitUp|limit_down",
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
        "下跌家数|下跌|downCount|declining",
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


def _metric_basename(metric: str) -> str:
    """把 normalizer 产出的 JSON 路径 metric 归一到**末段基名**（T22b）。

    normalizer 对嵌套载荷产出的 metric 是路径（``data.openInterest``、
    ``data[0].openInterest``、``result.list[0].openInterest``），而规则的
    别名表是裸指标名——拿整条路径精确比对会全部漏掉。去掉数组下标、取最后
    一个 ``.`` 之后再比对：基名仍然是**精确**匹配，``noise`` /
    ``openInterestRate`` 不会因此误命中（保住 T22 的"不做子串匹配"裁决）。
    """
    tail = re.sub(r"\[\d+\]", "", metric.strip()).split(".")[-1]
    return tail.strip().lower()


def _matches_metric(rule: _Rule, metric: str) -> bool:
    """检查指标名是否命中规则的**精确别名表**（T22：不再做子串匹配——
    旧的 `'oi' in metric` 会把 noise、openInterestRate 之类全命中；
    T22b：比对前先归一到末段基名，嵌套载荷的点分路径不再漏报）。"""
    if not metric:
        return False
    aliases = {p.strip().lower() for p in rule.metric_pattern.split("|")}
    return _metric_basename(metric) in aliases


def _prev_key(metric: str) -> str:
    return f"__anomaly_prev__:{metric.strip().lower()}"


def _load_prev_value(metric: str) -> float | None:
    """取同一 metric 上一次快照的值（存放在 market_cache，跨调查可用）。"""
    prev = market_cache.get(_prev_key(metric))
    if isinstance(prev, (int, float)):
        return float(prev)
    return None


def _store_prev_value(metric: str, value: float) -> None:
    # 1 小时：足够覆盖"下次调查拿到上次快照"的场景，又不至于拿太旧的数据算变化率
    market_cache.set(_prev_key(metric), float(value), ttl=3600.0)


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
    # T22：pct_change 规则的前值——优先用同一次调用里更早出现的同 metric datum
    # （前后两个快照），否则取 market_cache 里上一次调查留下的值；都没有就不触发。
    prev_in_call: dict[str, float] = {}
    tracked_metrics: set[str] = set()

    for result in tool_results:
        normalized = getattr(result, "normalized", [])
        if not normalized:
            continue

        for datum in normalized:
            metric = getattr(datum, "metric", "") or ""
            value = getattr(datum, "value", None)

            num_value = _extract_numeric(value)
            if num_value is None:
                continue

            # 同一 datum 同一 rule_type 只保留最高严重级（旧实现 OI 一条 datum
            # 同时过 high 和 critical 两条规则 → 报两条重复）
            best_by_type: dict[str, tuple[int, AnomalyRecord, _Rule]] = {}
            severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}

            for rule in rules:
                if not _matches_metric(rule, metric):
                    continue
                if rule.value_semantics == "pct_change":
                    tracked_metrics.add(metric.strip().lower())
                    prev = prev_in_call.get(metric)
                    if prev is None:
                        prev = _load_prev_value(metric)
                    if prev is None or prev == 0:
                        # 拿不到前值就不触发：宁可不报不要误报
                        continue
                    compare_value = (num_value - prev) / abs(prev) * 100.0
                elif rule.value_semantics == "ratio_to_percent":
                    compare_value = num_value * 100.0
                else:
                    compare_value = num_value

                triggered = False
                if rule.direction == "gt" and compare_value > rule.threshold:
                    triggered = True
                elif rule.direction == "lt" and compare_value < rule.threshold:
                    triggered = True

                if not triggered:
                    continue

                ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
                record = AnomalyRecord(
                    id=_next_id(),
                    type=rule.rule_type,
                    domain=getattr(datum, "domain", domain),
                    severity=rule.severity,
                    description=rule.description_template.format(compare_value),
                    metric=metric,
                    value=num_value,
                    normal_range=rule.normal_range,
                    possible_meaning=rule.possible_meaning,
                    timestamp=ts,
                    status="investigating",
                )
                rank = severity_order.get(rule.severity, 99)
                current = best_by_type.get(rule.rule_type)
                if current is None or rank < current[0]:
                    best_by_type[rule.rule_type] = (rank, record, rule)

            for _rank, record, _rule in best_by_type.values():
                results.append(record)
                logger.info(
                    "Anomaly detected: %s [%s] %s=%.2f threshold=%s %s",
                    record.type,
                    record.severity,
                    metric,
                    record.value,
                    _rule.threshold,
                    record.possible_meaning,
                )

            # 更新前值：本调用内后续同 metric 的 datum 与它配对，并存入 market_cache
            if metric.strip().lower() in tracked_metrics:
                prev_in_call[metric] = num_value
                _store_prev_value(metric, num_value)

    # Sort by severity: critical > high > medium > low
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    results.sort(key=lambda r: severity_order.get(r.severity, 99))

    if results:
        logger.info(
            "Detected %d anomalies out of %d tool results",
            len(results),
            len(tool_results),
        )

    return results
