from app.gateway.tool_registry import resolve_tool, registry_text


def test_registry_resolves_core_tool():
    meta = resolve_tool("sentiment")
    assert meta.tool_name == "public_sentiment_ashare_master_sentiment_get"


def test_registry_text_contains_core_tools():
    text = registry_text()
    assert "overview" in text
    assert "sentiment" in text
