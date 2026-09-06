from app.gateway.normalizer import normalize_tool_result


def test_partial_is_preserved():
    result = normalize_tool_result(
        "klines_market_klines_post",
        {},
        {"partial": True, "note": "history incomplete"},
    )
    assert result.status == "partial"
    assert result.partial is True
    # partial/note 是元信息，已被单独处理；不重复提取为指标
    # 但 ToolResult 的 partial 标志正确传播
    assert any(d.metric == "partial" or d.value == True
               for d in result.normalized) is False  # 不再作为指标提取


def test_error_is_normalized():
    result = normalize_tool_result("foo", {}, None, error="timeout")
    assert result.status == "error"
    assert result.error == "timeout"
