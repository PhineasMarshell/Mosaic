from app.gateway.normalizer import normalize_tool_result


def test_metadata_only_partial_is_error():
    """T1：只含 partial/note 元信息、没有任何数据点的载荷不能算 partial——
    Evidence Gate 会把 partial 当证据，零数据点的 partial 等同于伪证据，必须 error。"""
    result = normalize_tool_result(
        "klines_market_klines_post",
        {},
        {"partial": True, "note": "history incomplete"},
    )
    assert result.status == "error"
    assert result.normalized == []


def test_partial_with_data_is_preserved():
    """对照组：partial 且确实带了数据点时，partial 标志与 note 照常保留。"""
    result = normalize_tool_result(
        "klines_market_klines_post",
        {},
        {
            "partial": True,
            "note": "history incomplete",
            "candles": [{"t": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}],
        },
    )
    assert result.status == "partial"
    assert result.partial is True
    assert result.note and "history incomplete" in result.note
    assert len(result.normalized) > 0


def test_error_is_normalized():
    result = normalize_tool_result("foo", {}, None, error="timeout")
    assert result.status == "error"
    assert result.error == "timeout"
