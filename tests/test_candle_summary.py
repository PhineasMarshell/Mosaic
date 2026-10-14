"""T7 回归：candle_summary 的 price_last 必须是最后一根收盘价，混合 payload 不丢非 K 线指标。"""

from app.models.market import NormalizedDatum, ToolResult
from app.research.evidence import build_evidence


def _result(pairs) -> ToolResult:
    return ToolResult(
        tool="get_market_klines",
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


def test_dated_candle_is_preserved_as_claim_level_evidence():
    result = ToolResult(
        tool="get_market_klines",
        operation_id="get_market_klines",
        tool_key="klines",
        arguments={"symbol": "SH600519"},
        status="partial",
        partial=True,
        normalized=[
            NormalizedDatum(metric=f"candles[0].{field}", value=value, tool="get_market_klines")
            for field, value in {"t": "2026-10-09", "o": 1255.42, "h": 1282.0,
                                 "l": 1255.42, "c": 1263.0, "v": 3511051}.items()
        ],
    )

    evidence = build_evidence([result], id_prefix="technical", stable_ids=True)
    candle = next(item for item in evidence if item.metric == "candle")

    assert candle.as_of_date == "2026-10-09"
    assert candle.instrument == "SH600519"
    assert candle.value == {"t": "2026-10-09", "o": 1255.42, "h": 1282.0,
                            "l": 1255.42, "c": 1263.0, "v": 3511051}
    assert candle.result_partial is True
