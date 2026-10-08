"""DDGS 信源适配 — 复用 news_search._do_news_search，不改限流防御。

DDGS 限流是常态（A0 实测：首测成、随后 5 连败），本模块只做薄封装：
结构化结果 → NewsItem 列表；失败（meta.error 存在）抛 DdgsSourceError，
由聚合层决定重试一次（只许一次）与「源失败」记账。

date 字段实测是 ISO8601 绝对时间（'2026-10-07T19:47:00+00:00'），带 tz →
转 naive 本地；容错 'YYYY-MM-DD' 纯日期与 None。
"""

import asyncio
from datetime import datetime

from app.research.news_search import _ddgs_available, _do_news_search
from app.research.news_sources.types import DdgsSourceError, NewsItem, _to_naive


def _parse_date(value) -> datetime | None:
    """DDGS date → naive datetime；'YYYY-MM-DD' / None / 坏值容错。"""
    if not value:
        return None
    try:
        return _to_naive(datetime.fromisoformat(str(value)))
    except ValueError:
        try:
            return datetime.strptime(str(value), "%Y-%m-%d")
        except ValueError:
            return None


async def fetch_ddgs(
    query: str,
    *,
    max_results: int = 8,
    time_limit: str = "d",
    timeout: float = 20,
) -> list[NewsItem]:
    """DDGS 新闻搜索 → NewsItem 列表（薄封装，不重复实现限流防御）。

    Raises:
        DdgsSourceError: ddgs 库未安装，或 _do_news_search 返回结构化错误
            （meta.error 存在，即限流/网络异常）。零结果但无 error → 返回空列表。
    """
    if not _ddgs_available():
        raise DdgsSourceError("ddgs 库未安装，请运行 pip install ddgs")
    if not query or not query.strip():
        raise DdgsSourceError("DDGS 搜索词为空")
    # _do_news_search 是**同步阻塞**实现（内部 to_thread 再包一层），
    # 直接 await 会抛 "object dict can't be awaited"，必须先 to_thread。
    raw = await asyncio.wait_for(
        asyncio.to_thread(
            _do_news_search,
            query,
            max_results=max_results,
            time_limit=time_limit,
        ),
        timeout=timeout,
    )
    meta = raw.get("meta") or {}
    if meta.get("error"):
        raise DdgsSourceError(str(meta["error"]))
    items: list[NewsItem] = []
    for item in raw.get("news") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        items.append(
            NewsItem(
                title=title,
                url=str(item.get("url") or "").strip(),
                published_at=_parse_date(item.get("date")),
                source=str(item.get("source") or "").strip(),
                text=str(item.get("body") or "").strip(),
            )
        )
    return items
