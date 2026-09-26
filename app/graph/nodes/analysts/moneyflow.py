"""资金面分析员 — 两融、龙虎榜、筹码详情、HK 北向资金。"""

from __future__ import annotations

from app.graph.nodes.analysts.base import MarketAnalystNode


class MoneyflowAnalystNode(MarketAnalystNode):
    """资金面分析师：龙虎榜、北向资金、恒生指数、筹码/股东/两融/解禁。

    category="moneyflow" 的工具通过 by_category 自动发现。
    """

    category = "moneyflow"
