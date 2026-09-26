"""DDGS 新闻舆情搜索 — 内部直连工具。

不经过 Market Gateway（MCP / HTTP），直接在 Python 进程内调用 ddgs 库搜索
最新财经新闻。Planner 通过 tool_registry 发现此工具，ToolRuntime 走
_execute_internal 路径执行。

使用方式：
    - ToolMeta.http_method = "INTERNAL", operationId = "news_search"
    - Planner 生成步骤时传入 {query, max_results, time_limit}
    - 结果经 normalizer 展开为 NormalizedDatum 条目

注意：ddgs 库是纯同步阻塞的，外部调用需放在 asyncio.to_thread() 中。
"""

import asyncio
import logging
from typing import Any

try:
    from ddgs import DDGS
except ImportError:
    DDGS = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _ddgs_available() -> bool:
    return DDGS is not None


# ------------------------------------------------------------------ #
# 核心搜索逻辑（同步）                                                 #
# ------------------------------------------------------------------ #

def _do_news_search(
    query: str,
    *,
    max_results: int = 5,
    time_limit: str = "d",
) -> dict[str, Any]:
    """DDGS 新闻搜索的同步实现。

    Args:
        query: 搜索关键词（中文或英文均可）
        max_results: 返回条数上限，默认 5
        time_limit: 时间范围 ``d``=当天, ``w``=本周, ``m``=本月

    Returns:
        结构化结果字典，含 meta 和 news 列表。
    """
    if not _ddgs_available():
        raise RuntimeError("ddgs 库未安装，请运行 pip install ddgs")

    # 防御性检查：空查询直接返回结构化错误，不传给 DDGS 导致无意义的 "query is mandatory"
    if not query or not query.strip():
        logger.info("News search skipped: empty query")
        return {
            "meta": {
                "query": "",
                "count": 0,
                "time_limit": time_limit,
                "status": "跳过（未提供搜索关键词）",
            },
            "news": [],
        }

    try:
        logger.info("News search: %s (limit=%s)", query, time_limit)
        kwargs: dict[str, Any] = dict(
            region="wt-wt",
            safesearch="moderate",
            max_results=max_results,
        )
        # DDGS news timelimit: d/w/m → Bing/Yahoo 原生支持
        if time_limit in ("d", "w", "m"):
            kwargs["timelimit"] = time_limit

        with DDGS() as ddgs:
            items = list(ddgs.news(query, **kwargs))[:max_results]

        result = {
            "meta": {
                "query": query,
                "count": len(items),
                "time_limit": time_limit,
                "status": f"成功获取 {len(items)} 条资讯",
            },
            "news": items,
        }
        return result

    except Exception as exc:
        logger.warning("News search failed for %s: %s", query, exc)
        return {
            "meta": {
                "query": query,
                "count": 0,
                "time_limit": time_limit,
                "status": "搜索失败",
                "error": str(exc),
            },
            "news": [],
        }


async def search_news(
    query: str,
    *,
    max_results: int = 5,
    time_limit: str = "d",
) -> dict[str, Any]:
    """异步封装 — 在线程池中运行同步 DDGS 搜索。"""
    return await asyncio.to_thread(
        _do_news_search,
        query,
        max_results=max_results,
        time_limit=time_limit,
    )


# ------------------------------------------------------------------ #
# 结果展开 — DDGS → NormalizedDatum                                  #
# ------------------------------------------------------------------ #

def extract_news_entries(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """将 DDGS news_search 返回结构展开为 NormalizedDatum 字典列表。

    DDGS 原始输出格式::

        {"meta": {...}, "news": [{date, title, body, url, image, source}]}

    展开规则：
    - meta → 单个 metric（查询摘要 + 状态）
    - 每条新闻 item 展开为多个 datum（title/body/url/source/date/image）
    """
    from app.models.market import NormalizedDatum, STATUS_SUCCESS
    entries: list[NormalizedDatum] = []
    if not isinstance(raw, dict):
        return [e.model_dump() for e in entries]

    meta = raw.get("meta", {})
    if meta:
        for k, v in meta.items():
            entries.append(NormalizedDatum(
                domain="media",
                metric=f"meta.{k}",
                value=v,
                tool="news_search",
                status=STATUS_SUCCESS,
            ))

    news_items = raw.get("news")
    if not isinstance(news_items, list):
        return [e.model_dump() for e in entries]

    for i, item in enumerate(news_items):
        if not isinstance(item, dict):
            continue
        date_val = item.get("date") or item.get("pubDate") or item.get("publishedAt")
        for field, datum_key in (
            ("title", "title"),
            ("body", "summary"),
            ("source", "publisher"),
            ("url", "link"),
        ):
            val = item.get(field)
            if val:
                entries.append(NormalizedDatum(
                    domain="media",
                    metric=f"news[{i}].{datum_key}",
                    value=val,
                    timestamp=date_val,
                    source=item.get("source"),
                    tool="news_search",
                    status=STATUS_SUCCESS,
                ))
        if item.get("image"):
            entries.append(NormalizedDatum(
                domain="media",
                metric=f"news[{i}].image",
                value=item["image"],
                tool="news_search",
                status=STATUS_SUCCESS,
            ))

    return [e.model_dump() for e in entries]
