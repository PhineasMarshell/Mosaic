"""Evidence 构建单元测试 —— 不 mock 任何东西：normalize_tool_result 与
build_evidence 均为纯函数，直接喂真实 ToolResult/NormalizedDatum 结构。
未覆盖：真实网关载荷形状由 test_normalizer* 系列覆盖。"""

from app.gateway.normalizer import normalize_tool_result
from app.models.market import NormalizedDatum, ToolResult
from app.research.evidence import build_evidence


def test_evidence_keeps_tool_and_metric():
    result = normalize_tool_result(
        "public_sentiment_ashare_master_limit_up_count_get",
        {},
        {"sentiment": 54, "timestamp": "2026-09-04"},
    )
    evidence = build_evidence([result])
    assert evidence
    assert evidence[0].source_tool.startswith("public_sentiment")
    assert evidence[0].metric == "sentiment"


# ── B7：证据 domain 不得硬编码 crypto ─────────────────────────────


def _candle_result(domain: str = "us_stock") -> ToolResult:
    """构造 K 线结果（candle_summary 路径）；domain 模拟 datum 自带域。"""
    pairs = [
        ("candles[0].o", 210.0),
        ("candles[0].c", 230.0),
        ("candles[1].o", 230.0),
        ("candles[1].c", 236.0),
    ]
    return ToolResult(
        tool="get_market_klines",
        arguments={"symbol": "AAPL", "exchange": "xueqiu"},
        status="success",
        normalized=[NormalizedDatum(metric=m, value=v, tool="k", domain=domain) for m, v in pairs],
        raw={"candles": [{"c": 230.0}, {"c": 236.0}]},
    )


def test_candle_summary_domain_not_crypto_for_us_klines():
    """B7 回归：美股 K 线证据曾被写死 domain='crypto'（baseline: technical-001.domain='crypto'）。"""
    evidence = build_evidence([_candle_result()])
    summary = next(e for e in evidence if e.metric == "candle_summary")
    assert summary.domain != "crypto"
    assert summary.domain  # 不得为 None/空串（Evidence.domain 既有断言保持）


def test_candle_summary_domain_prefers_datum_domain():
    """datum 自带域（如 a_share K 线）优先于按 operationId 推断。"""
    evidence = build_evidence([_candle_result(domain="a_share")])
    summary = next(e for e in evidence if e.metric == "candle_summary")
    assert summary.domain == "a_share"


def test_snapshot_summary_fallback_domain_not_crypto():
    """快照摘要兜底分支（normalized 空、从 raw 提取）同样不得硬编码 crypto。"""
    result = ToolResult(
        tool="get_market_snapshot",
        arguments={"symbol": "XAU/USDT:USDT"},
        status="success",
        normalized=[],
        raw={"price": 84000.0, "high": 85000.0, "low": 83000.0, "open": 83500.0, "source": "test"},
    )
    evidence = build_evidence([result])
    assert evidence, "raw 快照摘要必须产出证据"
    assert all(e.domain != "crypto" for e in evidence)
    assert all(e.domain for e in evidence)


def test_evidence_domain_never_none_or_empty():
    """回归：datum 自带域为空串时必须落回按工具推断，不得变 None/空串。"""
    result = ToolResult(
        tool="get_market_klines",
        arguments={},
        status="success",
        normalized=[NormalizedDatum(metric="candles[0].c", value=1.0, tool="t", domain="")],
        raw=None,
    )
    evidence = build_evidence([result])
    assert evidence
    assert all(e.domain and e.domain != "crypto" for e in evidence)
