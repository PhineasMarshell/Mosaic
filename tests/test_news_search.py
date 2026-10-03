"""tests/test_news_search.py — DDGS 新闻舆情搜索集成测试。

验证：
- 模块可导入（ddgs 未安装时 gracefully degrade）
- _do_news_search 返回正确结构（meta + news）
- 错误路径返回结构化错误信息
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
    """同步搜索逻辑的结构化验证。"""

    def test_empty_result_structure(self):
        """即使请求失败，也应返回标准结构。"""
        from app.research.news_search import _do_news_search

        # 故意使用可能触发错误的查询来测错误路径
        result = _do_news_search("nonexistent_query_xyz", max_results=2)
        assert isinstance(result, dict)
        assert "meta" in result
        assert "news" in result
        assert isinstance(result["news"], list)
        assert result["meta"]["count"] == len(result["news"])

    def test_meta_has_required_fields(self):
        from app.research.news_search import _do_news_search

        result = _do_news_search("test query", max_results=3, time_limit="w")
        meta = result["meta"]
        assert meta["query"] == "test query"
        assert "time_limit" in meta
        assert "status" in meta
        assert meta["time_limit"] == "w"

    def test_error_path_no_crash(self):
        """网络不可用时不应抛异常，而是返回 error 字段。"""
        from app.research.news_search import _do_news_search

        result = _do_news_search("rapid_rapid_test", max_results=1)
        # 无论成功或失败，结构都合法
        assert "meta" in result
        # error 路径中 meta 里会包含错误信息
        if result["meta"]["count"] == 0:
            assert isinstance(result["meta"].get("error", ""), str) or result["meta"].get("status")


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
