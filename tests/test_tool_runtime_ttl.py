"""T17 回归：news_search 的 6 小时 TTL 不得被 execute() 的缓存写入覆盖成 30 秒。

旧实现：内部工具 _execute_internal 用 settings.news_search_ttl_seconds(21600) set 缓存，
但 execute() 随后用 _resolve_ttl("news_search")（无映射 → 默认 30s）对同一 key 再 set，
把限流保护 TTL 覆盖掉。修复后 _resolve_ttl 感知 settings，news_search 解析为 21600。
"""

import time

import pytest

from app.cache import _make_cache_key, _resolve_ttl, market_cache
from app.config import Settings
from app.graph.tool_runtime import ToolRuntime


@pytest.fixture(autouse=True)
def _clear_market_cache():
    market_cache.clear()
    yield
    market_cache.clear()


def test_resolve_ttl_uses_settings_for_news_search():
    settings = Settings()
    assert _resolve_ttl("news_search", settings) == settings.news_search_ttl_seconds
    # 不传 settings 保持旧映射行为（其它测试依赖）
    assert _resolve_ttl("nonexistent_tool_xyz") == 30.0


async def test_news_search_cache_ttl_survives_execute(monkeypatch):
    """离线可跑：patch 的是模块全局 _search_news（生产代码 tool_runtime.py 读全局，
    不读 self._search_news —— 旧写法打实例属性无效，测试实际打真实 DDGS 网络）。"""
    runtime = ToolRuntime(Settings())

    async def fake_search(query, *, max_results=5, time_limit="d"):
        return {"news": [{"title": "t", "body": "b", "url": "u", "date": "2026-10-04"}], "meta": {"status": "ok"}}

    monkeypatch.setattr("app.graph.tool_runtime._search_news", fake_search)
    result = await runtime.execute("news_search", {"query": "A股"}, set())
    assert result.status == "success"

    key = _make_cache_key("news_search", {"query": "A股"})
    entry = market_cache._store[key]
    remaining = entry.expires_at - time.monotonic()
    # 旧实现这里 ≈30s（被 execute() 的默认 TTL 覆盖）
    assert remaining > float(Settings().news_search_ttl_seconds) - 60
