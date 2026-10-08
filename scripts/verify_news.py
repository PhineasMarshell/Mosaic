"""一次性探针：实测新闻面多源信源可用性（新闻面接入的 A0 闸门）。

用法：
    .venv/Scripts/python.exe scripts/verify_news.py

探针清单（NEWS_PLAN.md §2-A0）：
    1. akshare stock_news_em('600519')      — 东财个股新闻（字段/条数）
    2. akshare stock_info_global_cls()      — 财联社电报（字段/条数）
    3. akshare stock_info_a_code_name()     — 全A code↔name 表（行数量级/首载耗时）
    4. Google News RSS 中文口 + 英文口       — 是否 XML、条数、首条 title/pubDate
    5. Google RSS 代理断开对照（子进程清代理 → 预期 ConnectTimeout 复现）
    6. DDGS wt-wt 连测 3 次                  — 记录成功次数（限流是常态）

闸门判定：
    - 通过：akshare 两路（1+2）可用 且 Google RSS（4）可用 → 全量实施
    - 部分通过：akshare 可用、Google RSS 不可用 → 砍 Google 源开工
    - 失败：akshare 两路全挂 → 停止 Phase A
"""

import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx

os.environ.setdefault("TQDM_DISABLE", "1")

MAX_RETRIES = 3
TIMEOUT = 20.0

GOOGLE_RSS_URL = "https://news.google.com/rss/search?q={query}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
GOOGLE_RSS_URL_EN = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"


def probe(name: str, fn):
    """带重试的探针执行器：最多 MAX_RETRIES 次，返回 (ok, note, elapsed_ms)。"""
    last_note = ""
    for attempt in range(1, MAX_RETRIES + 1):
        t0 = time.monotonic()
        try:
            ok, note = fn()
        except Exception as exc:  # noqa: BLE001 探针脚本要吞掉一切异常
            ok, note = False, f"EXC {type(exc).__name__}: {exc}"
        elapsed = (time.monotonic() - t0) * 1000
        suffix = f" | attempt={attempt} ({elapsed:.0f}ms)"
        print(f"[{name}] {'OK ' if ok else 'FAIL'} {note[:200]}{suffix}")
        if ok:
            return True, note, elapsed
        last_note = note
        time.sleep(1.0)
    return False, last_note, -1.0


# ------------------------------------------------------------------ #
# akshare 三路                                                        #
# ------------------------------------------------------------------ #


def probe_akshare_stock_news() -> tuple[bool, str]:
    import akshare as ak

    df = ak.stock_news_em(symbol="600519")
    cols = list(df.columns)
    expected = ["关键词", "新闻标题", "新闻内容", "发布时间", "文章来源", "新闻链接"]
    missing = [c for c in expected if c not in cols]
    if missing:
        return False, f"列缺失 {missing}，实际列={cols}"
    first = df.iloc[0]
    return True, (
        f"{len(df)}条 cols={cols} 首条标题={first['新闻标题']!r} "
        f"来源={first['文章来源']!r} 发布时间={first['发布时间']!r}"
    )


def probe_akshare_telegraph() -> tuple[bool, str]:
    import akshare as ak

    df = ak.stock_info_global_cls()
    cols = list(df.columns)
    expected = ["标题", "内容", "发布日期", "发布时间"]
    missing = [c for c in expected if c not in cols]
    if missing:
        return False, f"列缺失 {missing}，实际列={cols}"
    first = df.iloc[0]
    return True, f"{len(df)}条 cols={cols} 首条标题={first['标题']!r} 日期={first['发布日期']!r}"


def probe_akshare_code_name() -> tuple[bool, str]:
    import akshare as ak

    df = ak.stock_info_a_code_name()
    if not {"code", "name"}.issubset(df.columns):
        return False, f"列异常：{list(df.columns)}"
    return True, f"{len(df)}行 首行={df.iloc[0].to_dict()}"


# ------------------------------------------------------------------ #
# Google News RSS                                                     #
# ------------------------------------------------------------------ #


def _fetch_rss(url: str) -> tuple[bool, str]:
    with httpx.Client(trust_env=True, follow_redirects=True) as client:
        resp = client.get(url, timeout=TIMEOUT)
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    content = resp.content
    if not content.lstrip().startswith(b"<?xml"):
        return False, f"非 XML 响应，开头={content[:80]!r}"
    root = ET.fromstring(content)
    channel = root.find("channel")
    if channel is None:
        return False, "无 <channel> 节点"
    items = channel.findall("item")
    if not items:
        return False, "0 条 item"
    first = items[0]
    title = (first.findtext("title") or "").strip()
    pub = (first.findtext("pubDate") or "").strip()
    try:
        pub_dt = parsedate_to_datetime(pub)
    except Exception:  # noqa: BLE001
        pub_dt = None
    return True, f"{len(items)}条 首条title={title!r} pubDate={pub!r} parsed={pub_dt}"


def probe_google_rss_cn() -> tuple[bool, str]:
    return _fetch_rss(GOOGLE_RSS_URL.format(query=quote("贵州茅台")))


def probe_google_rss_en() -> tuple[bool, str]:
    return _fetch_rss(GOOGLE_RSS_URL_EN.format(query=quote("AAPL")))


# 代理断开对照：在子进程里清掉代理环境变量跑（严禁污染当前 shell）
_PROXY_CLEAR_SNIPPET = r"""
import httpx
from urllib.parse import quote
url = "https://news.google.com/rss/search?q={q}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
try:
    with httpx.Client(trust_env=True, follow_redirects=True) as client:
        resp = client.get(url, timeout=15.0)
    print(f"OK {resp.status_code}")
except Exception as exc:
    print(f"EXC {type(exc).__name__}")
"""


def probe_google_rss_no_proxy() -> tuple[bool, str]:
    """预期 ConnectTimeout 复现（降级路径的设计前提）。返回 (复现=True, note)。"""
    env = {k: v for k, v in os.environ.items() if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    r = subprocess.run(
        [sys.executable, "-c", _PROXY_CLEAR_SNIPPET],
        capture_output=True,
        text=True,
        timeout=40,
        env=env,
    )
    out = (r.stdout or "").strip()
    if "EXC ConnectTimeout" in out:
        return True, f"ConnectTimeout 复现（降级前提成立）stdout={out!r}"
    return False, f"预期 ConnectTimeout 未复现：stdout={out!r} stderr={r.stderr[-120:]!r}"


# ------------------------------------------------------------------ #
# DDGS                                                                #
# ------------------------------------------------------------------ #


def probe_ddgs() -> tuple[bool, str]:
    from ddgs import DDGS

    successes = 0
    notes = []
    for i in range(1, 4):
        try:
            with DDGS() as ddgs:
                items = list(ddgs.news("贵州茅台", region="wt-wt", safesearch="moderate", max_results=5, timelimit="d"))
            notes.append(f"#{i}:{len(items)}条")
            if items:
                successes += 1
        except Exception as exc:  # noqa: BLE001
            notes.append(f"#{i}:{type(exc).__name__}")
        time.sleep(1.0)
    return successes > 0, f"连测3次成功{successes}/3 {' '.join(notes)}"


# ------------------------------------------------------------------ #
# 主流程                                                              #
# ------------------------------------------------------------------ #


def main() -> None:
    print("=" * 70)
    print("新闻面多源 A0 探针闸门")
    print("=" * 70)
    ak_news_ok, _, _ = probe("1.akshare个股新闻", probe_akshare_stock_news)
    ak_tele_ok, _, _ = probe("2.akshare电报", probe_akshare_telegraph)
    probe("3.akshare全A名称表", probe_akshare_code_name)
    g_cn_ok, _, _ = probe("4a.GoogleRSS中文", probe_google_rss_cn)
    g_en_ok, _, _ = probe("4b.GoogleRSS英文", probe_google_rss_en)
    probe("5.GoogleRSS断代理对照", probe_google_rss_no_proxy)
    probe("6.DDGS(wt-wt)x3", probe_ddgs)

    akshare_ok = ak_news_ok and ak_tele_ok
    google_ok = g_cn_ok or g_en_ok

    print()
    print("=" * 70)
    if akshare_ok and google_ok:
        print("闸门判定：通过（akshare 两路 + Google RSS 可用）→ 全量实施")
    elif akshare_ok:
        print("闸门判定：部分通过（akshare 可用、Google RSS 不可用）→ 砍 Google 源开工")
    else:
        print("闸门判定：失败（akshare 两路全挂）→ 停止 Phase A，交回主 Agent")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
