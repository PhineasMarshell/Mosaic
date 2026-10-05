"""tests/test_news_search.py — DDGS 新闻舆情搜索测试。

验证：
- 模块可导入（ddgs 未安装时 gracefully degrade）
- _do_news_search 返回正确结构（meta + news）
- 错误路径返回结构化错误信息（T34 起 TestSyncSearch 全部 monkeypatch DDGS，
  密封不打真实网络；TestAsyncWrapper 仍为真实网络的冒烟用例）
- search_news 是异步函数，走 asyncio.to_thread 路径
"""

import pytest


class TestDDGSAvailability:
    """验证 ddgs 库的可用性检查。"""

    def test_ddgs_available_returns_bool(self):
        from app.research.news_search import _ddgs_available

        result = _ddgs_available()
        assert isinstance(result, bool)

    def test_ddgs_import_succeeds(self):
        from app.research.news_search import DDGS

        assert DDGS is not None

    def test_ddgs_available_true(self):
        """ddgs 应在测试环境中可用（已 pip install）。"""
        from app.research.news_search import _ddgs_available

        assert _ddgs_available() is True


class TestSyncSearch:
    """同步搜索逻辑的结构化验证。

    T34：旧版错误路径用例打**真实 DDGS**（非密封、慢且依赖网络）且断言恒真
    （``status`` 键在成功/零结果/异常三种结局下都存在，探针实测旧断言恒为
    True）。现全部改为 monkeypatch 模块全局 ``DDGS``，密封且可确定性地触发
    每条分支。
    """

    @staticmethod
    def _install_ddgs(monkeypatch, fake) -> None:
        """生产用 ``with DDGS() as ddgs`` 构造 —— 打桩成返回 fake 的工厂。"""
        from app.research import news_search

        monkeypatch.setattr(news_search, "DDGS", lambda: fake)

    @staticmethod
    def _make_ddgs(items=None, raise_on=None):
        class _FakeDDGS:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def news(self, query, **kwargs):
                if raise_on is not None:
                    raise raise_on
                return iter(items or [])

        return _FakeDDGS()

    @staticmethod
    def _news_item(i: int) -> dict:
        return {
            "title": f"headline {i}",
            "body": f"body {i}",
            "url": f"https://example.com/{i}",
            "source": "example",
            "date": "2026-10-05",
        }

    def test_ddgs_error_path_returns_structured_error(self, monkeypatch):
        """DDGS 抛异常 → 不炸、返回结构化结果，且 meta.error 必须写入真实异常信息。"""
        from app.research.news_search import _do_news_search

        self._install_ddgs(monkeypatch, self._make_ddgs(raise_on=RuntimeError("network down")))
        result = _do_news_search("any_query", max_results=2)
        assert isinstance(result, dict)
        assert result["news"] == []
        meta = result["meta"]
        assert meta["count"] == 0
        assert meta["query"] == "any_query"
        assert meta["status"] == "搜索失败"
        assert meta["error"] == "network down"

    def test_empty_results_structure(self, monkeypatch):
        """DDGS 正常返回但零结果 → 结构合法、status 说明零条、无 error 键。"""
        from app.research.news_search import _do_news_search

        self._install_ddgs(monkeypatch, self._make_ddgs(items=[]))
        result = _do_news_search("nonexistent_query_xyz", max_results=2)
        assert isinstance(result, dict)
        assert "meta" in result
        assert result["news"] == []
        assert result["meta"]["count"] == 0
        assert "成功获取 0 条" in result["meta"]["status"]
        assert "error" not in result["meta"]

    def test_meta_has_required_fields(self, monkeypatch):
        """成功路径：meta 携带 query / time_limit / status，count 与 news 对齐。"""
        from app.research.news_search import _do_news_search

        self._install_ddgs(monkeypatch, self._make_ddgs(items=[self._news_item(1), self._news_item(2)]))
        result = _do_news_search("test query", max_results=3, time_limit="w")
        meta = result["meta"]
        assert meta["query"] == "test query"
        assert "time_limit" in meta
        assert "status" in meta
        assert meta["time_limit"] == "w"
        assert meta["count"] == len(result["news"]) == 2

    def test_empty_query_skipped_without_touching_ddgs(self, monkeypatch):
        """空 query（"" / 纯空白）→ 走"跳过"分支，DDGS 完全不被构造。

        该分支自初始提交就存在于 news_search.py，但从未有测试覆盖（T34 补上）。
        打桩成"一被构造就炸"的假 DDGS：分支若失效（空 query 传给 DDGS），本用例必红。
        """
        from app.research.news_search import _do_news_search

        def _booby_trapped_factory():
            raise AssertionError("空 query 不应构造 DDGS")

        from app.research import news_search

        monkeypatch.setattr(news_search, "DDGS", _booby_trapped_factory)

        for bad_query in ("", "   "):
            result = _do_news_search(bad_query, max_results=2)
            assert result["news"] == []
            meta = result["meta"]
            assert meta["count"] == 0
            assert meta["status"] == "跳过（未提供搜索关键词）"
            assert "error" not in meta


class TestAsyncWrapper:
    """异步封装验证。"""

    @pytest.mark.asyncio
    async def test_search_news_is_coroutine(self):
        """async search_news 应能被 await。"""
        from app.research.news_search import search_news

        # 即使失败也应该返回而不是抛异常
        result = await search_news("async_test", max_results=1)
        assert isinstance(result, dict)
        assert "meta" in result


class TestNewsSearchCache:
    """news_search 结果缓存验证（P4-3）。"""

    @pytest.mark.asyncio
    async def test_same_args_hits_cache(self, monkeypatch):
        """同一参数连续两次 execute，底层 _search_news 只被调 1 次。"""
        from unittest.mock import AsyncMock

        from app.config import Settings
        from app.graph.tool_runtime import ToolRuntime

        fake = AsyncMock(
            return_value={
                "meta": {"count": 1, "status": "ok"},
                "news": [{"title": "cache_hit_test", "body": "x"}],
            }
        )
        monkeypatch.setattr("app.graph.tool_runtime._search_news", fake)

        runtime = ToolRuntime(Settings())
        args = {"query": "p43_cache_same_xyz", "max_results": 3}
        r1 = await runtime.execute("news_search", args, set())
        r2 = await runtime.execute("news_search", args, set())
        assert fake.await_count == 1
        assert r1.status == "success"
        assert r2.status == "success"

    @pytest.mark.asyncio
    async def test_different_args_bypasses_cache(self, monkeypatch):
        """不同参数不命中缓存，底层 _search_news 被调 2 次。"""
        from unittest.mock import AsyncMock

        from app.config import Settings
        from app.graph.tool_runtime import ToolRuntime

        fake = AsyncMock(
            return_value={
                "meta": {"count": 1, "status": "ok"},
                "news": [{"title": "cache_miss_test", "body": "y"}],
            }
        )
        monkeypatch.setattr("app.graph.tool_runtime._search_news", fake)

        runtime = ToolRuntime(Settings())
        await runtime.execute("news_search", {"query": "p43_diff_a_xyz", "max_results": 2}, set())
        await runtime.execute("news_search", {"query": "p43_diff_b_xyz", "max_results": 2}, set())
        assert fake.await_count == 2
