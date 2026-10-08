"""新闻事件分析员 — 执行 Supervisor 分配的 news 类工具（多源新闻聚合）。

A6：在通用骨架上覆写 ``_make_digest``，从结果 datum 里做**规则版事件归因**
（无 LLM）：统计 news_top_* / telegraph_* 条数、可用/失败源、双源确认数与
时间窗，产出形如 ``个股新闻8条(东财+Google,2条双源确认,最新14:00)`` 的摘要，
≤200 字硬约束（base._make_digest 同款截断）。只覆写摘要，不碰 _execute_tools。
"""

from app.graph.nodes.analysts.base import MarketAnalystNode
from app.models.market import ToolResult

#: 聚合工具 sources_ok 里的源标识 → 摘要用中文名
_SOURCE_LABELS = {"eastmoney": "东财", "google": "Google", "ddgs": "DDGS"}


def _source_labels(names: list) -> str:
    return "+".join(_SOURCE_LABELS.get(n, n) for n in names) if names else ""


class NewsAnalystNode(MarketAnalystNode):
    category = "news"

    def _make_digest(self, tools_used: list[str], results: list[ToolResult]) -> str:
        """新闻证据归因摘要（≤200 字）。

        从 datum 统计而非复述工具名：让 Reasoning 在不翻 datum 的情况下也能
        看到证据量、来源与时效。没有新闻类 datum 时回退基类通用摘要。
        """
        parts: list[str] = []
        has_news = False
        for result in results:
            if result.status == "error":
                continue
            items = 0
            meta: dict = {}
            multi_source: int | None = None
            for datum in result.normalized:
                metric = datum.metric
                if metric == "news_meta" or metric == "telegraph_meta":
                    if isinstance(datum.value, dict):
                        meta = datum.value
                elif metric.startswith("news_top_") or (metric.startswith("telegraph_") and metric != "telegraph_meta"):
                    items += 1
                elif metric == "news_stats" and isinstance(datum.value, dict):
                    multi_source = datum.value.get("multi_source_titles")
            if not meta and items == 0:
                continue
            has_news = True
            sources_ok = meta.get("sources_ok") or []
            sources_failed = meta.get("sources_failed") or []
            if result.tool == "telegraph":
                # 注意：_news_window 返回 (最新, 最早)，所以最新时刻在 window_start。
                # 这里曾误用 window_end（最早那条）渲染「截止」，导致摘要时间比实际最新消息还早。
                latest = str(meta.get("window_start") or "")
                piece = f"电报{items}条" + (f"(截止{latest[-5:]})" if latest else "")
            else:
                window_start = str(meta.get("window_start") or "")
                piece = f"{result.tool} {items}条({_source_labels(sources_ok)}"
                if multi_source:
                    piece += f",{multi_source}条双源确认"
                if window_start:
                    piece += f",最新{window_start[-5:]}"
                piece += ")"
            if sources_failed:
                piece += f"[部分源失败:{_source_labels(sources_failed)}]"
            parts.append(piece)

        if not has_news:
            return super()._make_digest(tools_used, results)
        text = ";".join(parts)
        if len(text) > 200:
            text = text[:200]
        return text
