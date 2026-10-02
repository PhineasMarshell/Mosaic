"""新闻事件分析员 — 执行 Supervisor 分配的 news 类工具（DDGS 检索等）。"""

from app.graph.nodes.analysts.base import MarketAnalystNode


class NewsAnalystNode(MarketAnalystNode):
    category = "news"
