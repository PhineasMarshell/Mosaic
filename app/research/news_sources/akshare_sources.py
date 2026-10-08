"""akshare 信源适配 — 东财个股新闻 / 财联社电报 / 全A code↔name 表。

三个接口的字段映射来自 A0 实测（2026-10-08，akshare 1.19.1）：

- ``stock_news_em(symbol='600519')``：10 条，列 ``关键词/新闻标题/新闻内容/发布时间/文章来源/新闻链接``
- ``stock_info_global_cls()``：20 条，列 ``标题/内容/发布日期/发布时间``（无链接字段）；
  **实测 ``发布日期`` 返回 ``datetime.date`` 对象**（非字符串），合成时两种类型都兼容
- ``stock_info_a_code_name()``：5572 行，列 ``code/name``；带 tqdm 进度条噪音
  （导入前设 ``TQDM_DISABLE=1`` 尽力抑制）

使用纪律：akshare 是同步阻塞的，一律 ``asyncio.to_thread`` 包裹；东财系上游有
反爬，只在低频 + 长 TTL 缓存路径下使用（调用方 ToolRuntime 全部内建缓存）。
akshare 内部用 requests，超时参数不透传，超时控制放 ``asyncio.wait_for`` 外层
兜底（超时后线程仍在跑但不阻塞事件循环，可接受）。
"""

import asyncio
import os
from datetime import date, datetime

# tqdm 进度条噪音抑制（stock_info_a_code_name 翻页 18 页）；必须先于 akshare 导入
os.environ.setdefault("TQDM_DISABLE", "1")

try:
    import akshare as ak
except ImportError:
    ak = None  # type: ignore[assignment]

from app.research.news_sources.types import AkshareUnavailableError, NewsItem

#: 电报 datum 的固定来源标注
TELEGRAPH_SOURCE = "财联社电报"


def _akshare_available() -> bool:
    return ak is not None


def _ensure_akshare() -> None:
    if not _akshare_available():
        raise AkshareUnavailableError("akshare 库未安装，请运行 pip install akshare")


def _run_with_timeout(fn, *args, timeout: float, **kwargs):
    """to_thread + wait_for 组合：同步阻塞的 akshare 调用不卡事件循环。"""
    return asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=timeout)


def _parse_naive_datetime(text: str) -> datetime | None:
    """'2026-10-06 08:44:00' → naive datetime；解析失败 → None（排序沉底）。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None


def _compose_telegraph_datetime(day, time_text: str) -> datetime | None:
    """电报的 发布日期 + 发布时间 合成 naive datetime。

    实测 ``发布日期`` 是 ``datetime.date``（1.19.1），兼容 str 与 date 两种类型。
    """
    if day is None:
        return None
    if isinstance(day, datetime):
        day = day.date()
    if isinstance(day, date):
        day_str = day.strftime("%Y-%m-%d")
    else:
        day_str = str(day).strip()
    return _parse_naive_datetime(f"{day_str} {(time_text or '').strip()}")


async def fetch_stock_news(symbol6: str, *, timeout: float = 30) -> list[NewsItem]:
    """东财个股新闻（主路，A 股个股）：stock_news_em → NewsItem 列表。

    Args:
        symbol6: 6 位纯数字 A 股代码（不含 SH/SZ 前缀），由调用方先归一
    """
    _ensure_akshare()
    df = await _run_with_timeout(ak.stock_news_em, symbol=symbol6, timeout=timeout)
    items: list[NewsItem] = []
    for _, row in df.iterrows():
        items.append(
            NewsItem(
                title=str(row.get("新闻标题") or "").strip(),
                url=str(row.get("新闻链接") or "").strip(),
                published_at=_parse_naive_datetime(str(row.get("发布时间") or "")),
                source=str(row.get("文章来源") or "").strip(),
                text=str(row.get("新闻内容") or "").strip(),
            )
        )
    return [i for i in items if i.title]


async def fetch_telegraph(*, timeout: float = 15) -> list[NewsItem]:
    """财联社电报快讯（主路，大盘/盘面）：stock_info_global_cls → NewsItem 列表。

    实测无链接字段，NewsItem.url 允许为空（电报同款语义）。
    """
    _ensure_akshare()
    df = await _run_with_timeout(ak.stock_info_global_cls, timeout=timeout)
    items: list[NewsItem] = []
    for _, row in df.iterrows():
        items.append(
            NewsItem(
                title=str(row.get("标题") or "").strip(),
                url="",
                published_at=_compose_telegraph_datetime(row.get("发布日期"), str(row.get("发布时间") or "")),
                source=TELEGRAPH_SOURCE,
                text=str(row.get("内容") or "").strip(),
            )
        )
    return [i for i in items if i.title]


async def fetch_code_name_map(*, timeout: float = 60) -> dict[str, str]:
    """全A code↔name 表：stock_info_a_code_name → dict（code 6 位无前缀 → 中文名）。

    首载实测约 5-6s（5572 行、18 页翻页），调用方必须长 TTL 缓存（24h）摊销。
    """
    _ensure_akshare()
    df = await _run_with_timeout(ak.stock_info_a_code_name, timeout=timeout)
    return {str(code).zfill(6): str(name).strip() for code, name in zip(df["code"], df["name"], strict=False)}
