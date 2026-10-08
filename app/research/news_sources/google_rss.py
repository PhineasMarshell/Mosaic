"""Google News RSS 信源 — URL 构造 / 抓取 / stdlib XML 解析。

URL 模板（A0 实测）：``https://news.google.com/rss/search?q=…&hl=zh-CN&gl=CN&ceid=CN:zh-Hans``
- 单查询 100 条，必须在解析层截断（max_items），不能全量进 datum。
- ``<item>`` 的 title 末尾带 ``" - 来源"`` 后缀（如 ``贵州茅台：今日暂停！ - 东方财富``），
  剥离成独立 source 字段。
- pubDate 是 RFC822，用 ``email.utils.parsedate_to_datetime``；description 是 HTML
  片段，只取纯文本前 N 字做摘要。
- **代理硬依赖**（A0 实测直连 ConnectTimeout）：httpx 必须 ``trust_env=True`` 走环境代理；
  代理断开 → ConnectTimeout → 聚合层记「源失败」走 partial 降级。
- 用 stdlib ``xml.etree.ElementTree`` 解析，不引 feedparser（RSS 2.0 结构简单足够）。
"""

import asyncio
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import quote

import httpx

from app.research.news_sources.types import GoogleRssError, NewsItem, _to_naive

_URL_TEMPLATE = "https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={ceid}"

#: title 末尾 " - 来源名" 后缀（A0 实测格式：`贵州茅台：今日暂停！ - 东方财富`）
_SOURCE_SUFFIX_RE = re.compile(r"^(?P<title>.+?)\s+-\s+(?P<source>[^-]+)$")
_TAG_RE = re.compile(r"<[^>]+>")


def build_url(query: str, *, hl: str = "zh-CN", gl: str = "CN", ceid: str = "CN:zh-Hans") -> str:
    """构造 Google News RSS 搜索 URL（纯函数）。"""
    return _URL_TEMPLATE.format(query=quote(query), hl=hl, gl=gl, ceid=ceid)


def _element_text(el: ET.Element | None) -> str:
    """取元素的全部文本（含嵌套子标签里的文本），再剥 HTML 标签。

    不能用 ``findtext``：它只返回**第一个**文本节点，``<b>``/``<a>`` 等嵌套
    标签里的文字会整段丢失（实测 ``<description>内容<b>摘要</b></description>``
    用 findtext 只拿到 ``内容``）。
    """
    if el is None:
        return ""
    return "".join(el.itertext())


def _strip_html(desc: str, *, max_chars: int) -> str:
    """description HTML 片段 → 纯文本前 N 字摘要。"""
    text = unescape(_TAG_RE.sub("", desc or "")).strip()
    if len(text) > max_chars:
        return text[:max_chars] + "…"
    return text


def _split_title_suffix(raw_title: str) -> tuple[str, str]:
    """`标题 - 来源` → (标题, 来源)；无后缀时 source 为空。"""
    m = _SOURCE_SUFFIX_RE.match(raw_title.strip())
    if m:
        return m.group("title").strip(), m.group("source").strip()
    return raw_title.strip(), ""


def parse_rss(content: bytes, *, max_items: int = 20, max_desc_chars: int = 200) -> list[NewsItem]:
    """解析 Google News RSS XML → NewsItem 列表（纯函数，可直测）。

    非 XML / 无 channel / HTTP 错误页面 → 抛 GoogleRssError（不许漏到聚合层之外）。
    """
    head = content.lstrip()[:20]
    if not head.startswith(b"<?xml") and not head.startswith(b"<rss"):
        raise GoogleRssError(f"非 XML 响应，开头={content[:60]!r}")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise GoogleRssError(f"XML 解析失败: {exc}") from exc

    channel = root.find("channel")
    if channel is None:
        raise GoogleRssError("RSS 无 <channel> 节点")

    items: list[NewsItem] = []
    for item in channel.findall("item"):
        raw_title = _element_text(item.find("title")).strip()
        if not raw_title:
            continue
        title, source = _split_title_suffix(raw_title)
        pub_raw = _element_text(item.find("pubDate")).strip()
        published: datetime | None = None
        if pub_raw:
            try:
                published = _to_naive(parsedate_to_datetime(pub_raw))
            except (TypeError, ValueError):
                published = None
        items.append(
            NewsItem(
                title=title,
                url=_element_text(item.find("link")).strip(),
                published_at=published,
                source=source,
                text=_strip_html(_element_text(item.find("description")), max_chars=max_desc_chars),
            )
        )
        if len(items) >= max_items:
            break
    return items


async def fetch(
    query: str,
    *,
    timeout: float = 15.0,
    max_items: int = 20,
    hl: str = "zh-CN",
    gl: str = "CN",
    ceid: str = "CN:zh-Hans",
) -> list[NewsItem]:
    """抓取并解析 Google News RSS（async；走环境代理）。

    Args:
        query: 搜索关键词（中文或英文均可）
        timeout: 单次请求超时秒数
        max_items: 解析层截断上限（上游单查询 100 条，不许全量外泄）
        hl/gl/ceid: 语言/地区参数（英文口用 en-US/US/US:en）
    """
    url = build_url(query, hl=hl, gl=gl, ceid=ceid)
    # trust_env=True：A0 实测 Google RSS 直连 ConnectTimeout，必须走环境代理；
    # 代理断开时 ConnectTimeout 会从这里抛出，由聚合层记「源失败」。
    async with httpx.AsyncClient(trust_env=True, follow_redirects=True) as client:
        resp = await asyncio.wait_for(client.get(url), timeout=timeout)
    if resp.status_code != 200:
        raise GoogleRssError(f"HTTP {resp.status_code}")
    return parse_rss(resp.content, max_items=max_items)
