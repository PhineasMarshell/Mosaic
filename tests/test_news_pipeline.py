"""新闻面多源接入测试（A9）—— 解析/聚合纯函数直测 + monkeypatch 假响应。

本文件 mock 掉了什么、因此没有覆盖什么：
- akshare 三个接口、Google RSS 的 httpx 抓取、DDGS 搜索全部打桩（模块全局
  monkeypatch），因此**不覆盖真实网络行为**——真链路由 scripts/verify_news.py
  （A0 探针）与 Phase B e2e 手工实测覆盖；
- 不 mock LLM（本文件不触发任何 LLM 路径）。

fixture 自洽：autouse 清空 market_cache（全局单例污染防线），TTL 断言在
单用例内顺序执行。
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.cache import _make_cache_key, _resolve_ttl, market_cache
from app.config import Settings
from app.graph.tool_runtime import ToolRuntime
from app.models.market import NormalizedDatum, ToolResult
from app.research.news_sources import (
    AkshareUnavailableError,
    DdgsSourceError,
    GoogleRssError,
    NewsItem,
    _to_naive,
    akshare_sources,
    build_url,
    clip,
    dedup_news,
    multi_source_count,
    parse_rss,
    rank_news,
    symbol_to_akshare6,
)
from app.research.news_sources.aggregate import normalize_title


@pytest.fixture(autouse=True)
def _clear_market_cache():
    market_cache.clear()
    yield
    market_cache.clear()


def _news_item(
    i: int, *, title: str | None = None, url: str | None = None, source: str = "东财", at: datetime | None = None
) -> NewsItem:
    return NewsItem(
        title=title or f"标题{i}",
        url=url if url is not None else f"https://example.com/{i}",
        published_at=at,
        source=source,
        text=f"正文{i}",
    )


# ── 1. 纯函数 ────────────────────────────────────────────────────


class TestBuildUrl:
    def test_cn_default(self):
        url = build_url("贵州茅台")
        assert url.startswith("https://news.google.com/rss/search?q=")
        assert "hl=zh-CN" in url and "gl=CN" in url and "ceid=CN:zh-Hans" in url

    def test_en_locale(self):
        url = build_url("AAPL", hl="en-US", gl="US", ceid="US:en")
        assert "hl=en-US" in url and "gl=US" in url and "ceid=US:en" in url


def _rss_xml(items: list[str]) -> bytes:
    body = "".join(items)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Google News</title>'
        f"{body}</channel></rss>"
    ).encode()


def _rss_item(title: str, pub: str = "Mon, 05 Oct 2026 08:41:11 GMT", desc: str = "内容<b>摘要</b>") -> str:
    return (
        f"<item><title>{title}</title><link>https://example.com/x</link>"
        f"<pubDate>{pub}</pubDate><description>{desc}</description></item>"
    )


class TestParseRss:
    def test_normal_parse_with_suffix_strip_and_rfc822(self):
        items = parse_rss(_rss_xml([_rss_item("贵州茅台：今日暂停！ - 东方财富")]))
        assert len(items) == 1
        item = items[0]
        assert item.title == "贵州茅台：今日暂停！"
        assert item.source == "东方财富"
        assert item.url == "https://example.com/x"
        assert item.published_at is not None
        assert item.published_at.tzinfo is None  # RFC822 UTC → naive 本地
        assert item.text == "内容摘要"  # HTML 标签剥离
        assert "<b>" not in item.text

    def test_title_without_suffix_source_empty(self):
        items = parse_rss(_rss_xml([_rss_item("无后缀标题")]))
        assert items[0].title == "无后缀标题"
        assert items[0].source == ""

    def test_max_items_truncation(self):
        xml = _rss_xml([_rss_item(f"标题{i} - 来源{i}") for i in range(10)])
        items = parse_rss(xml, max_items=3)
        assert len(items) == 3

    def test_non_xml_raises_google_rss_error(self):
        with pytest.raises(GoogleRssError):
            parse_rss(b"<html><body>error page</body></html>")

    def test_empty_items_ok(self):
        assert parse_rss(_rss_xml([])) == []

    def test_bad_pubdate_tolerated(self):
        items = parse_rss(_rss_xml([_rss_item("t", pub="not-a-date")]))
        assert items[0].published_at is None


class TestSymbolToAkshare6:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("SH600519", "600519"),
            ("SZ000001", "000001"),
            ("BJ430047", "430047"),
            ("600519", "600519"),
            ("sh600519", "600519"),
            (" 600519 ", "600519"),
        ],
    )
    def test_valid(self, raw, expected):
        assert symbol_to_akshare6(raw) == expected

    @pytest.mark.parametrize("raw", ["", "AAPL", "BTC/USDT", "12345", "SH60051", "6005191"])
    def test_invalid_returns_none(self, raw):
        assert symbol_to_akshare6(raw) is None


class TestToNaive:
    def test_tz_aware_converts_to_local_naive(self):
        aware = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
        naive = _to_naive(aware)
        assert naive is not None and naive.tzinfo is None
        # astimezone 转本地：与 aware.astimezone() 的墙钟一致
        assert naive == aware.astimezone().replace(tzinfo=None)

    def test_naive_passthrough(self):
        naive = datetime(2026, 10, 5, 12, 0)
        assert _to_naive(naive) == naive

    def test_none_passthrough(self):
        assert _to_naive(None) is None


class TestDedupNews:
    def test_same_url_dedup(self):
        a = _news_item(1, url="https://x.com/1")
        b = _news_item(2, title="不同标题", url="https://x.com/1")
        kept, dup = dedup_news([a, b])
        assert len(kept) == 1 and kept[0] is a and dup == 1

    def test_same_normalized_title_dedup(self):
        a = _news_item(1, title="贵州茅台：今日暂停！", url="https://a.com")
        b = _news_item(2, title="贵州茅台： 今日暂停！", url="https://b.com")
        kept, dup = dedup_news([a, b])
        assert len(kept) == 1 and dup == 1

    def test_empty_url_not_used_as_key(self):
        a = _news_item(1, title="标题甲", url="")
        b = _news_item(2, title="标题乙", url="")
        kept, dup = dedup_news([a, b])
        assert len(kept) == 2 and dup == 0

    def test_all_different(self):
        kept, dup = dedup_news([_news_item(i) for i in range(3)])
        assert len(kept) == 3 and dup == 0


class TestRankNews:
    def test_descending_and_none_sinks(self):
        t1 = datetime(2026, 10, 8, 10, 0)
        t2 = datetime(2026, 10, 8, 9, 0)
        a = _news_item(1, at=t1)
        b = _news_item(2, at=t2)
        c = _news_item(3, at=None)
        ranked = rank_news([c, b, a])
        assert ranked == [a, b, c]

    def test_stable_for_equal_time(self):
        t = datetime(2026, 10, 8, 10, 0)
        a = _news_item(1, at=t)
        b = _news_item(2, at=t)
        assert rank_news([a, b]) == [a, b]


class TestClip:
    def test_clip_long_adds_ellipsis(self):
        assert clip("x" * 10, 5) == "xxxxx…"

    def test_clip_short_unchanged(self):
        assert clip("abc", 10) == "abc"

    def test_clip_none(self):
        assert clip(None, 5) == ""


class TestMultiSourceCount:
    def test_counts_titles_in_two_sources(self):
        a = _news_item(1, title="同题新闻", source="东财")
        b = _news_item(2, title="同题新闻", source="Google")
        c = _news_item(3, title="独条新闻", source="东财")
        assert multi_source_count([a, b, c]) == 1

    def test_empty_source_not_counted(self):
        a = _news_item(1, title="同题新闻", source="")
        b = _news_item(2, title="同题新闻", source="")
        assert multi_source_count([a, b]) == 0

    def test_multi_source_count_on_pre_dedup_pool(self):
        """回归：multi_source_titles 必须在**去重前**合池上算。

        去重后同标题条目已被折叠成一条，只剩一个 source，统计恒为 0 ——
        那会让 Critic「≥2 源才算已证实」的抓手彻底失效。
        """
        pool = [
            _news_item(1, title="贵州茅台：今日暂停！", source="证券时报网"),
            _news_item(2, title="贵州茅台：今日暂停！", source="东方财富"),
            _news_item(3, title="独条新闻", source="证券时报网"),
        ]
        assert multi_source_count(pool) == 1
        deduped, dup = dedup_news(pool)
        assert dup == 1
        assert multi_source_count(deduped) == 0  # 这就是必须在去重前算的原因


# ── 2. akshare 适配层（monkeypatch 模块全局 ak）──────────────────


class TestAkshareSources:
    def test_fetch_stock_news_mapping(self, monkeypatch):
        df = pd.DataFrame(
            {
                "关键词": ["贵州茅台"],
                "新闻标题": ["贵州茅台：今日暂停！"],
                "新闻内容": ["公告内容全文"],
                "发布时间": ["2026-10-06 08:44:00"],
                "文章来源": ["证券时报网"],
                "新闻链接": ["http://finance.eastmoney.com/a/1.html"],
            }
        )

        class _FakeAk:
            @staticmethod
            def stock_news_em(symbol):
                assert symbol == "600519"
                return df

        monkeypatch.setattr(akshare_sources, "ak", _FakeAk())
        import asyncio

        items = asyncio.run(akshare_sources.fetch_stock_news("600519"))
        assert len(items) == 1
        item = items[0]
        assert item.title == "贵州茅台：今日暂停！"
        assert item.source == "证券时报网"
        assert item.url == "http://finance.eastmoney.com/a/1.html"
        assert item.text == "公告内容全文"
        assert item.published_at == datetime(2026, 10, 6, 8, 44, 0)

    def test_fetch_telegraph_mapping_date_object_and_str(self, monkeypatch):
        """实测 发布日期 是 datetime.date（1.19.1）；str 类型同样兼容。"""
        df_date = pd.DataFrame(
            {
                "标题": ["快讯甲"],
                "内容": ["内容甲"],
                "发布日期": [pd.Timestamp("2026-10-08").date()],
                "发布时间": ["14:00:48"],
            }
        )
        df_str = df_date.copy()
        df_str["发布日期"] = ["2026-10-08"]

        class _FakeAk:
            def __init__(self, df):
                self.df = df

            @staticmethod
            def stock_info_global_cls():
                return _FakeAk.current_df

        _FakeAk.current_df = df_date
        monkeypatch.setattr(akshare_sources, "ak", _FakeAk)
        import asyncio

        items = asyncio.run(akshare_sources.fetch_telegraph())
        assert items[0].published_at == datetime(2026, 10, 8, 14, 0, 48)
        assert items[0].source == "财联社电报"
        assert items[0].url == ""

        _FakeAk.current_df = df_str
        items = asyncio.run(akshare_sources.fetch_telegraph())
        assert items[0].published_at == datetime(2026, 10, 8, 14, 0, 48)

    def test_fetch_code_name_map(self, monkeypatch):
        df = pd.DataFrame({"code": ["000001", "600519"], "name": ["平安银行", "贵州茅台"]})

        class _FakeAk:
            @staticmethod
            def stock_info_a_code_name():
                return df

        monkeypatch.setattr(akshare_sources, "ak", _FakeAk())
        import asyncio

        mapping = asyncio.run(akshare_sources.fetch_code_name_map())
        assert mapping == {"000001": "平安银行", "600519": "贵州茅台"}

    def test_ak_none_raises_unavailable(self, monkeypatch):
        monkeypatch.setattr(akshare_sources, "ak", None)
        import asyncio

        with pytest.raises(AkshareUnavailableError):
            asyncio.run(akshare_sources.fetch_stock_news("600519"))
        with pytest.raises(AkshareUnavailableError):
            asyncio.run(akshare_sources.fetch_telegraph())
        with pytest.raises(AkshareUnavailableError):
            asyncio.run(akshare_sources.fetch_code_name_map())


# ── 3. 聚合工具 ──────────────────────────────────────────────────


def _patch_code_name(monkeypatch):
    async def fake_code_name(*, timeout=60):
        return {"600519": "贵州茅台"}

    monkeypatch.setattr("app.graph.tool_runtime.fetch_code_name_map", fake_code_name)


class TestInternalSymbolNews:
    async def test_both_sources_success(self, monkeypatch):
        t1 = datetime(2026, 10, 8, 14, 0)
        t2 = datetime(2026, 10, 8, 9, 0)

        async def fake_east(symbol6, *, timeout=30):
            assert symbol6 == "600519"
            # 跨源同题（§1.4 实证：东财头条与 Google 头条一字不差）→ multi_source=1
            return [_news_item(1, title="贵州茅台：今日暂停！", source="证券时报网", at=t1)]

        async def fake_google(query, *, timeout=15.0, **kw):
            assert query == "贵州茅台"
            return [
                _news_item(
                    2,
                    title="贵州茅台：今日暂停！",
                    source="东方财富",
                    at=t2,
                )
            ]

        monkeypatch.setattr("app.graph.tool_runtime.fetch_stock_news", fake_east)
        monkeypatch.setattr("app.graph.tool_runtime.fetch_google_rss", fake_google)
        _patch_code_name(monkeypatch)

        runtime = ToolRuntime(Settings())
        result = await runtime.execute("internal_symbol_news", {"symbol": "SH600519", "top_k": 8}, set())
        assert result.status == "success"
        assert result.errors_is_empty if False else result.error is None
        # meta + top_k + stats；去重后实际条目 2 条
        assert len(result.normalized) <= 8 + 2
        metrics = [d.metric for d in result.normalized]
        assert metrics[0] == "news_meta" and metrics[-1] == "news_stats"
        meta = result.normalized[0].value
        assert meta["symbol6"] == "600519" and meta["name"] == "贵州茅台"
        assert meta["sources_ok"] == ["eastmoney", "google"]
        assert meta["multi_source_titles"] == 1  # 同标题双源确认（去重前合池统计）
        assert meta["duplicates_removed"] == 1
        top = [d for d in result.normalized if d.metric.startswith("news_top_")]
        # dedup 保留先出现者（东财，14:00）——Google 同题条目被折叠
        assert len(top) == 1
        assert top[0].value["source"] == "证券时报网"
        stats = result.normalized[-1].value
        assert stats["multi_source_titles"] == 1
        assert stats["per_source"] == {"证券时报网": 1}

    async def test_east_ok_google_fail_partial(self, monkeypatch):
        async def fake_east(symbol6, *, timeout=30):
            return [_news_item(1, at=datetime(2026, 10, 8, 10, 0))]

        async def fake_google(query, *, timeout=15.0, **kw):
            raise GoogleRssError("proxy down")

        monkeypatch.setattr("app.graph.tool_runtime.fetch_stock_news", fake_east)
        monkeypatch.setattr("app.graph.tool_runtime.fetch_google_rss", fake_google)
        _patch_code_name(monkeypatch)

        runtime = ToolRuntime(Settings())
        result = await runtime.execute("internal_symbol_news", {"symbol": "600519"}, set())
        assert result.status == "partial"
        assert result.partial is True
        assert "google" in (result.note or "")
        assert any(d.metric.startswith("news_top_") for d in result.normalized)

    async def test_both_fail_error_empty_normalized(self, monkeypatch):
        async def fake_east(symbol6, *, timeout=30):
            raise RuntimeError("east down")

        async def fake_google(query, *, timeout=15.0, **kw):
            raise GoogleRssError("proxy down")

        monkeypatch.setattr("app.graph.tool_runtime.fetch_stock_news", fake_east)
        monkeypatch.setattr("app.graph.tool_runtime.fetch_google_rss", fake_google)
        _patch_code_name(monkeypatch)

        runtime = ToolRuntime(Settings())
        result = await runtime.execute("internal_symbol_news", {"symbol": "600519"}, set())
        assert result.status == "error"
        assert result.normalized == []

    async def test_unrecognized_symbol_error(self, monkeypatch):
        runtime = ToolRuntime(Settings())
        result = await runtime.execute("internal_symbol_news", {"symbol": "AAPL"}, set())
        assert result.status == "error"
        assert "无法识别" in (result.error or "")

    async def test_symbol_guard_fills_6digit_code(self, monkeypatch):
        """A4 设计点：symbol_news 不进白名单，问题里的 6 位代码会被守卫自动补齐。"""
        from app.graph.nodes.analysts.news import NewsAnalystNode

        async def fake_east(symbol6, *, timeout=30):
            return [_news_item(1, at=datetime(2026, 10, 8, 10, 0))]

        monkeypatch.setattr("app.graph.tool_runtime.fetch_stock_news", fake_east)
        monkeypatch.setattr("app.graph.tool_runtime.fetch_google_rss", AsyncMock(return_value=[]))
        _patch_code_name(monkeypatch)

        state = {
            "question": "贵州茅台600519最近有什么消息",
            "route": [
                {
                    "analyst": "news",
                    "budget": 1,
                    "tool_calls": [{"tool_key": "symbol_news", "arguments": {}}],
                }
            ],
        }
        node = NewsAnalystNode(Settings())
        out = await node(state)
        assert out["findings"][0]["failed"] is False
        assert "symbol_news" in out["findings"][0]["tools_used"]
        assert out["errors"] == []


class TestInternalMarketTelegraph:
    async def test_keyword_filter_and_ranking(self, monkeypatch):
        items = [
            _news_item(1, title="央行降准公告", source="财联社电报", at=datetime(2026, 10, 8, 14, 0)),
            _news_item(2, title="白酒板块走强", source="财联社电报", at=datetime(2026, 10, 8, 13, 0)),
            _news_item(3, title="央行开展逆回购", source="财联社电报", at=datetime(2026, 10, 8, 12, 0)),
        ]

        async def fake_telegraph(*, timeout=15):
            return items

        monkeypatch.setattr("app.graph.tool_runtime.fetch_telegraph", fake_telegraph)
        runtime = ToolRuntime(Settings())
        result = await runtime.execute("internal_market_telegraph", {"top_k": 5, "keyword": "央行"}, set())
        assert result.status == "success"
        meta = result.normalized[0]
        assert meta.metric == "telegraph_meta"
        assert meta.value["total"] == 3 and meta.value["kept"] == 2 and meta.value["keyword"] == "央行"
        tops = [d for d in result.normalized if d.metric.startswith("telegraph_") and d.metric != "telegraph_meta"]
        assert [d.value["title"] for d in tops] == ["央行降准公告", "央行开展逆回购"]

    async def test_empty_args_default_top_k(self, monkeypatch):
        async def fake_telegraph(*, timeout=15):
            return [_news_item(i, source="财联社电报", at=datetime(2026, 10, 8, 10, i)) for i in range(20)]

        monkeypatch.setattr("app.graph.tool_runtime.fetch_telegraph", fake_telegraph)
        runtime = ToolRuntime(Settings())
        result = await runtime.execute("internal_market_telegraph", {}, set())
        assert result.status == "success"
        assert result.normalized[0].value["kept"] == 15  # 默认 top_k=15
        assert all(d.metric != "news_stats" for d in result.normalized)

    async def test_fetch_fail_error(self, monkeypatch):
        async def fake_telegraph(*, timeout=15):
            raise RuntimeError("cls down")

        monkeypatch.setattr("app.graph.tool_runtime.fetch_telegraph", fake_telegraph)
        runtime = ToolRuntime(Settings())
        result = await runtime.execute("internal_market_telegraph", {}, set())
        assert result.status == "error" and result.normalized == []


class TestInternalNewsDigest:
    async def test_ddgs_retry_once_then_success(self, monkeypatch):
        """DDGS 限流实证：首败重试 1 次成功 → success。"""
        google_items = [_news_item(1, title="白酒板块新闻", source="Google", at=datetime(2026, 10, 8, 10, 0))]
        ddgs_items = [_news_item(2, title="白酒板块评论", source="搜狐", at=datetime(2026, 10, 8, 9, 0))]

        async def fake_google(query, *, timeout=15.0, **kw):
            return google_items

        calls = {"n": 0}

        async def fake_ddgs(query, *, time_limit="d", timeout=20, max_results=8):
            calls["n"] += 1
            if calls["n"] == 1:
                raise DdgsSourceError("rate limited")
            return ddgs_items

        monkeypatch.setattr("app.graph.tool_runtime.fetch_google_rss", fake_google)
        monkeypatch.setattr("app.graph.tool_runtime.fetch_ddgs", fake_ddgs)
        runtime = ToolRuntime(Settings())
        result = await runtime.execute("internal_news_digest", {"query": "白酒板块"}, set())
        assert calls["n"] == 2  # 首败 + 重试 1 次，不多不少
        assert result.status == "success"
        assert result.normalized[0].metric == "news_meta"
        assert result.normalized[0].value["query"] == "白酒板块"
        assert result.normalized[0].value["sources_ok"] == ["google", "ddgs"]

    async def test_both_fail_error(self, monkeypatch):
        async def fake_google(query, *, timeout=15.0, **kw):
            raise GoogleRssError("down")

        async def fake_ddgs(query, *, time_limit="d", timeout=20, max_results=8):
            raise DdgsSourceError("limited")

        monkeypatch.setattr("app.graph.tool_runtime.fetch_google_rss", fake_google)
        monkeypatch.setattr("app.graph.tool_runtime.fetch_ddgs", fake_ddgs)
        runtime = ToolRuntime(Settings())
        result = await runtime.execute("internal_news_digest", {"query": "白酒板块"}, set())
        assert result.status == "error"
        assert result.normalized == []

    async def test_empty_query_error(self):
        runtime = ToolRuntime(Settings())
        result = await runtime.execute("internal_news_digest", {}, set())
        assert result.status == "error"
        assert "query" in (result.error or "")


# ── 4. TTL ───────────────────────────────────────────────────────


class TestTtl:
    def test_resolve_ttl_news_search_week_default(self):
        settings = Settings()
        assert _resolve_ttl("news_search", settings) == settings.news_ttl_week_seconds
        assert _resolve_ttl("internal_symbol_news", settings) == settings.news_symbol_ttl_seconds
        assert _resolve_ttl("internal_market_telegraph", settings) == settings.news_telegraph_ttl_seconds
        assert _resolve_ttl("internal_news_digest", settings) == settings.news_digest_ttl_seconds

    def test_resolve_ttl_news_search_tiers_by_argument(self):
        """回归：execute() 会用 _resolve_ttl 对同一 key 再 set 一次，必须能按 time_limit 分档。"""
        settings = Settings()
        assert _resolve_ttl("news_search", settings, {"time_limit": "d"}) == settings.news_ttl_day_seconds
        assert _resolve_ttl("news_search", settings, {"time_limit": "w"}) == settings.news_ttl_week_seconds
        assert _resolve_ttl("news_search", settings, {"time_limit": "m"}) == settings.news_ttl_month_seconds
        # 无参 / 非法档位 → week 兜底
        assert _resolve_ttl("news_search", settings, {}) == settings.news_ttl_week_seconds
        assert _resolve_ttl("news_search", settings, {"time_limit": "zzz"}) == settings.news_ttl_week_seconds

    async def test_news_search_ttl_tiers(self, monkeypatch):
        """time_limit=d/w/m 三档 TTL 各自生效（污染类断言在单用例内顺序执行）。"""

        async def fake_search(query, *, max_results=5, time_limit="d"):
            return {"news": [{"title": "t", "body": "b", "url": "u", "date": "2026-10-04"}], "meta": {"status": "ok"}}

        monkeypatch.setattr("app.graph.tool_runtime._search_news", fake_search)
        runtime = ToolRuntime(Settings())
        expected = {
            "d": Settings().news_ttl_day_seconds,
            "w": Settings().news_ttl_week_seconds,
            "m": Settings().news_ttl_month_seconds,
        }
        for tier, ttl_value in expected.items():
            args = {"query": f"分档{tier}", "time_limit": tier}
            result = await runtime.execute("news_search", args, set())
            assert result.status == "success"
            key = _make_cache_key("news_search", args)
            entry = market_cache._store[key]
            remaining = entry.expires_at - __import__("time").monotonic()
            # 三档 TTL 依次递增（d<w<m），且各自接近配置值
            assert remaining > ttl_value - 60, f"time_limit={tier} 的 TTL 未按档位生效"

    async def test_new_tools_ttl_match_config(self, monkeypatch):
        """三个新工具的缓存 TTL 与配置键对上（污染类断言在单用例内顺序执行）。"""
        settings = Settings()
        runtime = ToolRuntime(settings)

        async def fake_east(symbol6, *, timeout=30):
            return [_news_item(1, at=datetime(2026, 10, 8, 10, 0))]

        async def fake_google(query, *, timeout=15.0, **kw):
            return []

        async def fake_telegraph(*, timeout=15):
            return [_news_item(1, source="财联社电报", at=datetime(2026, 10, 8, 10, 0))]

        async def fake_ddgs(query, *, time_limit="d", timeout=20, max_results=8):
            return [_news_item(2, source="搜狐", at=datetime(2026, 10, 8, 9, 0))]

        monkeypatch.setattr("app.graph.tool_runtime.fetch_stock_news", fake_east)
        monkeypatch.setattr("app.graph.tool_runtime.fetch_google_rss", fake_google)
        monkeypatch.setattr("app.graph.tool_runtime.fetch_telegraph", fake_telegraph)
        monkeypatch.setattr("app.graph.tool_runtime.fetch_ddgs", fake_ddgs)
        _patch_code_name(monkeypatch)

        import time as _time

        cases = [
            ("internal_symbol_news", {"symbol": "600519"}, settings.news_symbol_ttl_seconds, "internal_symbol_news"),
            ("internal_market_telegraph", {}, settings.news_telegraph_ttl_seconds, "internal_market_telegraph"),
            ("internal_news_digest", {"query": "白酒"}, settings.news_digest_ttl_seconds, "internal_news_digest"),
        ]
        for tool, args, ttl_value, cache_tool in cases:
            result = await runtime.execute(tool, args, set())
            assert result.status in ("success", "partial"), f"{tool} 应可成功（fake 数据正常）"
            key = _make_cache_key(cache_tool, args)
            entry = market_cache._store[key]
            remaining = entry.expires_at - _time.monotonic()
            assert remaining > ttl_value - 60, f"{tool} 的 TTL 与配置键不符"


# ── 5. 注册表 ────────────────────────────────────────────────────


class TestRegistry:
    def test_four_news_tools_declared(self):
        from app.gateway.tool_registry import ALL_TOOLS, by_category

        news_tools = by_category.get("news", [])
        assert len(news_tools) == 4
        by_key = {t.key: t for t in ALL_TOOLS}
        assert by_key["symbol_news"].tool_name == "internal_symbol_news"
        assert by_key["symbol_news"].domain == "a_share"
        assert by_key["telegraph"].tool_name == "internal_market_telegraph"
        assert by_key["telegraph"].domain == "a_share"
        assert by_key["news_digest"].tool_name == "internal_news_digest"
        assert by_key["news_digest"].domain == "cross"
        for t in news_tools:
            assert t.category == "news"
            assert t.http_method == "INTERNAL"

    def test_registry_text_domain_filter(self):
        """registry_text 是**严格等值**过滤（§1.6 实测）：cross 不会自动附加到别的域。

        因此 a_share 只看到两条 a_share 新闻工具；us_stock 单独查时**看不到**
        cross 的 news_search/news_digest —— 真实链路里由 Supervisor 显式传
        [explicit_domain, "cross"] 一起带上（supervisor.py:92-95）。
        """
        from app.gateway.tool_registry import registry_text

        a_share = registry_text(domains=["a_share"])
        assert "- symbol_news:" in a_share and "- telegraph:" in a_share
        assert "- news_digest:" not in a_share and "- news_search:" not in a_share

        us = registry_text(domains=["us_stock"])
        assert "- symbol_news:" not in us and "- telegraph:" not in us

        # Supervisor 的真实调用形态：显式域 + cross 一起传
        us_with_cross = registry_text(domains=["us_stock", "cross"])
        assert "- news_digest:" in us_with_cross and "- news_search:" in us_with_cross
        assert "- symbol_news:" not in us_with_cross and "- telegraph:" not in us_with_cross

    def test_by_key_by_name_no_duplicates(self):
        """T24：key 全局唯一；新闻工具的 operationId 不进共享索引（无 BY_NAME 歧义）。"""
        from app.gateway.tool_registry import ALL_TOOLS, BY_KEY, BY_NAME, SHARED_BY_NAME

        assert len(BY_KEY) == len(ALL_TOOLS)  # key 全局唯一
        news_names = {t.tool_name for t in ALL_TOOLS if t.category == "news"}
        for name in news_names:
            assert name not in SHARED_BY_NAME  # 新 operationId 无复用、无歧义
            # BY_NAME 里必须能解析到 news 工具本身（未被别的域条目抢占）
            assert BY_NAME[name].category == "news"


# ── 6. 路由 ──────────────────────────────────────────────────────


class TestRouting:
    def test_route_candidate_categories_with_news_enabled(self):
        from app.graph.nodes.supervisor import route_candidate_categories

        assert "news" in route_candidate_categories(Settings(news_enabled=True))
        assert "news" not in route_candidate_categories(Settings(news_enabled=False))


# ── 7. digest 覆写 + Critic 纪律（契约级）────────────────────────


class TestNewsDigestOverride:
    def _symbol_news_result(self) -> ToolResult:
        meta = {
            "symbol": "SH600519",
            "symbol6": "600519",
            "name": "贵州茅台",
            "sources_ok": ["eastmoney", "google"],
            "sources_failed": [],
            "kept": 2,
            "window_start": "2026-10-08 14:00",
            "window_end": "2026-10-08 09:00",
            "multi_source_titles": 1,
        }
        return ToolResult(
            tool="symbol_news",
            arguments={},
            status="success",
            normalized=[
                NormalizedDatum(domain="media", tool="symbol_news", metric="news_meta", value=meta),
                NormalizedDatum(domain="media", tool="symbol_news", metric="news_top_1", value={"title": "a"}),
                NormalizedDatum(domain="media", tool="symbol_news", metric="news_top_2", value={"title": "b"}),
                NormalizedDatum(
                    domain="media",
                    tool="symbol_news",
                    metric="news_stats",
                    value={"per_source": {}, "multi_source_titles": 1},
                ),
            ],
        )

    def test_digest_mentions_counts_sources_and_cross_confirmation(self):
        from app.graph.nodes.analysts.news import NewsAnalystNode

        node = NewsAnalystNode(Settings())
        digest = node._make_digest(["symbol_news"], [self._symbol_news_result()])
        assert "2条" in digest
        assert "东财+Google" in digest
        assert "1条双源确认" in digest
        assert len(digest) <= 200

    def test_digest_fallback_without_news_datums(self):
        from app.graph.nodes.analysts.news import NewsAnalystNode

        node = NewsAnalystNode(Settings())
        empty = ToolResult(tool="quote", arguments={}, status="error", normalized=[])
        digest = node._make_digest(["quote"], [empty])
        assert digest  # 回退基类摘要，不为空


def test_critic_news_rules_attached():
    """A7 契约：critic.py 存在跨域 _NEWS_RULES 且在 prompt 组装处无条件附加。"""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "app" / "graph" / "nodes" / "critic.py").read_text(encoding="utf-8")
    assert "_NEWS_RULES" in text
    assert "news_meta.multi_source_titles" in text
    # 无条件附加：不放在 if rules 分支内
    assert 'prompt_pieces.extend(["\\n=== 新闻证据纪律 ===\\n", _NEWS_RULES])' in text


def test_planner_prompt_has_news_tool_guidance():
    """A8 契约：planner 提示词含四类新闻工具的选用指引。"""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "app" / "agent" / "prompts_graph.py").read_text(encoding="utf-8")
    for key in ("symbol_news", "telegraph", "news_digest", "news_search"):
        assert key in text


def test_normalize_title_strips_punct_and_case():
    assert normalize_title("贵州茅台： 今日暂停！") == normalize_title("贵州茅台今日暂停")
    assert normalize_title("Hello, World!") == "helloworld"
