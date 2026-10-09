"""工具注册表单元测试 —— 不 mock 任何东西：by_category / registry_text /
resolve_tool 均为进程内纯查询。未覆盖：多域过滤与 operationId 歧义语义
由 test_tool_registry_multi_domain.py 覆盖。"""

from app.gateway.tool_registry import by_category, registry_text, resolve_tool


def test_registry_resolves_core_tool():
    meta = resolve_tool("sentiment")
    assert meta.tool_name == "get_ashare_sentiment"


def test_registry_text_contains_core_tools():
    text = registry_text()
    assert "overview" in text
    assert "sentiment" in text


def test_news_category_contains_four_news_tools():
    """新闻面多源接入（A3）后 news 类工具为 4 条：news_search + 三个新聚合工具。"""
    news_tools = by_category.get("news", [])
    assert {t.key for t in news_tools} == {"news_search", "symbol_news", "telegraph", "news_digest"}
    assert {t.tool_name for t in news_tools} == {
        "news_search",
        "internal_symbol_news",
        "internal_market_telegraph",
        "internal_news_digest",
    }


def test_sentiment_category_has_xq_discussions():
    """新增的 sentiment 类别应包含 xq_discussions 工具。"""
    sentiment_tools = by_category.get("sentiment", [])
    assert len(sentiment_tools) > 0
    keys = {t.key for t in sentiment_tools}
    assert "xq_discussions" in keys
