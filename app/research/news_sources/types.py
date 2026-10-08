"""新闻面多源信源适配层 — 类型定义。

NewsItem 是所有信源（东财个股新闻 / 财联社电报 / Google 资讯 RSS / DDGS）
归一后的统一条目结构；各源失败用独立异常类型表达，聚合层转为「源失败」。

时间统一约定：解析即归一为 naive 本地时间（``_to_naive``），
东财/电报上游实测就是 naive ``YYYY-MM-DD HH:MM:SS``；
Google RFC822（UTC）与 DDGS ISO8601（带 tz）转 naive 本地后可比。
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class NewsItem:
    """单个信源的归一化新闻条目。"""

    title: str
    url: str = ""  # 电报实测无链接字段，允许空
    published_at: datetime | None = None  # naive 本地时间；缺失为 None（排序沉底）
    source: str = ""  # 东财=文章来源；Google=title 剥离后缀；电报='财联社电报'；DDGS=source 字段
    text: str = ""


def _to_naive(dt: datetime | None) -> datetime | None:
    """tz-aware → naive 本地时间；naive 原样返回；None → None。

    纯函数：排序与去重都依赖「所有时间可比」这一前提，必须可直测。
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt


class GoogleRssError(Exception):
    """Google News RSS 抓取/解析失败（HTTP 错误、重定向到 HTML、非 XML 响应等）。"""


class AkshareUnavailableError(Exception):
    """akshare 库未安装，无法使用东财/电报/名称表信源。"""


class DdgsSourceError(Exception):
    """DDGS 搜索失败（限流、网络异常等）——聚合层据此决定重试一次。"""
