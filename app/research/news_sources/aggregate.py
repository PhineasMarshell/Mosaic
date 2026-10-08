"""新闻聚合纯函数 — 去重 / 排序 / 截断 / 交叉源统计 / symbol 归一。

全部是可直测的纯函数（A9 测试矩阵的核心），不做任何 IO。
"""

import re
from datetime import datetime

from app.research.news_sources.types import NewsItem

#: 标题归一用的「去空白+标点」正则（跨源标题去重的二级键）
_TITLE_NORM_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def symbol_to_akshare6(symbol: str) -> str | None:
    """任意常见写法 → 6 位纯数字 A 股代码；无法识别 → None（不猜）。

    SH600519 / SZ000001 / BJ430047 / 600519 / sh600519 → '600519'。
    """
    text = (symbol or "").strip().upper()
    if not text:
        return None
    if text[:2] in ("SH", "SZ", "BJ"):
        text = text[2:]
    if len(text) == 6 and text.isdigit():
        return text
    return None


def normalize_title(title: str) -> str:
    """标题归一：lower + 去空白与标点（跨源去重的二级键）。"""
    return _TITLE_NORM_RE.sub("", (title or "").lower())


def dedup_news(items: list[NewsItem]) -> tuple[list[NewsItem], int]:
    """跨源去重。

    一级键：url 非空时 url 相等即重复；二级键：标题归一相等即重复。
    返回 (去重后保留的条目, 重复条数)；保留先出现者（稳定）。
    """
    kept: list[NewsItem] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    dup_count = 0
    for item in items:
        url_key = item.url.strip()
        title_key = normalize_title(item.title)
        if url_key and url_key in seen_urls:
            dup_count += 1
            continue
        if title_key and title_key in seen_titles:
            dup_count += 1
            continue
        if url_key:
            seen_urls.add(url_key)
        if title_key:
            seen_titles.add(title_key)
        kept.append(item)
    return kept, dup_count


def rank_news(items: list[NewsItem]) -> list[NewsItem]:
    """按 published_at 降序；None 沉底；稳定排序（同时间保持原顺序）。"""
    return sorted(
        items,
        key=lambda i: i.published_at or datetime.min,
        reverse=True,
    )


def clip(text: str | None, limit: int) -> str:
    """截断文本到 limit 字符，超长加 '…'；None → 空串。"""
    text = text or ""
    if limit <= 0:
        return ""
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def multi_source_count(items: list[NewsItem]) -> int:
    """标题归一后出现于 ≥2 个不同 source 的标题条数（Critic 交叉验证的抓手）。

    调用纪律：必须传**去重前**的合池。dedup_news 会把同标题的跨源条目折叠成
    一条（只留先出现者），在去重后池上统计恒为 0 —— 那样这个数字就没有牙了。
    """
    by_title: dict[str, set[str]] = {}
    for item in items:
        key = normalize_title(item.title)
        if not key:
            continue
        if item.source:
            by_title.setdefault(key, set()).add(item.source)
    return sum(1 for sources in by_title.values() if len(sources) >= 2)
