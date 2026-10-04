"""T8 回归：不含 6 位代码的问题也必须真正执行 news_search，而不是被 symbol 守卫静默跳过。"""

import pytest

from app.cache import market_cache
from app.config import Settings
from app.graph.nodes.analysts.news import NewsAnalystNode


@pytest.fixture(autouse=True)
def _clear_cache():
    market_cache.clear()
    yield
    market_cache.clear()


def _state() -> dict:
    # 问题与参数都不含 6 位 A 股代码、也没有 symbol。
    return {
        "question": "今天有什么财经新闻",
        "route": [
            {
                "analyst": "news",
                "budget": 1,
                "tool_calls": [
                    {"tool_key": "news_search", "arguments": {"query": "今天有什么财经新闻", "max_results": 3}}
                ],
            }
        ],
    }


async def test_news_search_executed_without_code(monkeypatch):
    async def fake_search_news(query, *, max_results=5, time_limit="d"):
        return {
            "news": [{"date": "2026-10-04", "title": "新闻标题ABC", "body": "正文", "url": "http://x"}],
            "meta": {"status": "ok", "count": 1},
        }

    monkeypatch.setattr("app.graph.tool_runtime._search_news", fake_search_news)

    node = NewsAnalystNode(Settings())
    out = await node(_state())

    finding = out["findings"][0]
    assert finding["failed"] is False
    assert "news_search" in finding["tools_used"], "旧实现在这里静默跳过，tools_used 为空"
    assert out["evidence"]


async def test_missing_symbol_skip_logs_warning(monkeypatch, caplog):
    # 一个需要 symbol、但无代码的调用：跳过时必须以 warning 呈现（旧实现 debug）。
    state = {
        "question": "某公司怎么样",
        "route": [
            {
                "analyst": "news",
                "budget": 1,
                "tool_calls": [
                    {"tool_key": "quote", "arguments": {}}  # quote 工具需要 symbol
                ],
            }
        ],
    }

    import logging

    logging.getLogger("app.graph.nodes.analysts.base").propagate = True
    node = NewsAnalystNode(Settings())
    out = await node(state)

    assert out["findings"][0]["tools_used"] == []
    assert any("跳过" in r.getMessage() and r.levelno >= logging.WARNING for r in caplog.records)
