"""技术面分析员 — 行情/K线/盘口/题材/情绪/异常归因。"""

from __future__ import annotations

import logging

from app.graph.nodes.analysts.base import MarketAnalystNode

logger = logging.getLogger(__name__)


class TechnicalAnalystNode(MarketAnalystNode):
    """技术面分析师：K 线、盘口、涨停池/题材、市场情绪、异动归因。

    category="technical" 的工具通过 by_category 自动发现，
    跨域工具（klines/snapshot/abnormal_reasons）的默认参数按域注入。
    """

    category = "technical"
