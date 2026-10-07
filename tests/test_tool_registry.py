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


def test_news_category_contains_only_news_search():
    news_tools = by_category.get("news", [])
    assert len(news_tools) == 1
    assert news_tools[0].key == "news_search"


def test_sentiment_category_not_present():
    assert "sentiment" not in by_category
