"""新闻面多源信源适配层 — 公开 API re-export。

模块划分：
- types:        NewsItem + 各源失败异常 + 时间归一
- google_rss:   Google News RSS（代理硬依赖，直连实测 ConnectTimeout）
- akshare_sources: 东财个股新闻 / 财联社电报 / 全A code↔name 表
- ddgs_source:  DDGS 薄封装（限流常态，失败抛 DdgsSourceError）
- aggregate:    去重/排序/截断/交叉源统计（纯函数）
"""

from app.research.news_sources.aggregate import (
    clip,
    dedup_news,
    multi_source_count,
    normalize_title,
    rank_news,
    symbol_to_akshare6,
)
from app.research.news_sources.akshare_sources import (
    TELEGRAPH_SOURCE,
    fetch_code_name_map,
    fetch_stock_news,
    fetch_telegraph,
)
from app.research.news_sources.ddgs_source import fetch_ddgs
from app.research.news_sources.google_rss import build_url, parse_rss
from app.research.news_sources.google_rss import fetch as fetch_google_rss
from app.research.news_sources.types import (
    AkshareUnavailableError,
    DdgsSourceError,
    GoogleRssError,
    NewsItem,
    _to_naive,
)

__all__ = [
    "AkshareUnavailableError",
    "DdgsSourceError",
    "GoogleRssError",
    "NewsItem",
    "TELEGRAPH_SOURCE",
    "build_url",
    "clip",
    "dedup_news",
    "fetch_code_name_map",
    "fetch_ddgs",
    "fetch_google_rss",
    "fetch_stock_news",
    "fetch_telegraph",
    "multi_source_count",
    "normalize_title",
    "parse_rss",
    "rank_news",
    "symbol_to_akshare6",
    "_to_naive",
]
