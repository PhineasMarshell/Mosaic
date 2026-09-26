"""基本面分析员 — F10：finance/business/concept/shareholders/survey。"""

from __future__ import annotations

from app.graph.nodes.analysts.base import MarketAnalystNode


class FundamentalAnalystNode(MarketAnalystNode):
    """基本面分析师：公司主营业务/财务信息/概念标签/股东信息/资料调查。

    category="fundamental" 的工具通过 by_category 自动发现。
    """

    category = "fundamental"
