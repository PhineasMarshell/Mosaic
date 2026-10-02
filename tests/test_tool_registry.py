from app.gateway.tool_registry import by_category, resolve_tool, registry_text


def test_registry_resolves_core_tool():
    meta = resolve_tool("sentiment")
    assert meta.tool_name == "public_sentiment_ashare_master_sentiment_get"


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
