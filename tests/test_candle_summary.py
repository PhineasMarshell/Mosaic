"""T7 回归：candle_summary 的 price_last 必须是最后一根收盘价，混合 payload 不丢非 K 线指标。"""

from app.models.market import NormalizedDatum, ToolResult
from app.research.evidence import build_evidence


def _result(pairs) -> ToolResult:
    return ToolResult(
        tool="klines_market_klines_post",
        arguments={},
        status="success",
        normalized=[NormalizedDatum(metric=m, value=v, tool="k") for m, v in pairs],
    )


def test_price_last_is_last_close_and_non_candle_kept():
    pairs = [
        ("candles[0].o", 1.0),
        ("candles[0].c", 2.0),
        ("candles[1].o", 2.0),
        ("candles[1].c", 4.0),
        ("openInterest", 8.5),
    ]
    evidence = build_evidence([_result(pairs)])
    by_metric = {e.metric: e.value for e in evidence}

    summary = by_metric["candle_summary"]
    assert summary["price_last"] == 4.0  # 旧实现取到 candles[1].o=2.0
    assert summary["price_min"] == 2.0
    assert summary["price_max"] == 4.0
    assert by_metric["openInterest"] == 8.5  # 旧实现 continue 把它丢掉


def test_high_low_used_for_range():
    pairs = [
        ("candles[0].o", 1.0),
        ("candles[0].h", 3.0),
        ("candles[0].l", 1.0),
        ("candles[0].c", 2.0),
        ("candles[1].o", 2.0),
        ("candles[1].h", 5.0),
        ("candles[1].l", 2.0),
        ("candles[1].c", 4.0),
    ]
    evidence = build_evidence([_result(pairs)])
    summary = next(e.value for e in evidence if e.metric == "candle_summary")
    assert summary["price_min"] == 1.0
    assert summary["price_max"] == 5.0
    assert summary["price_last"] == 4.0
