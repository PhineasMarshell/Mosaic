"""Evidence 构建单元测试 —— 不 mock 任何东西：normalize_tool_result 与
build_evidence 均为纯函数，直接喂真实 ToolResult/NormalizedDatum 结构。
未覆盖：真实网关载荷形状由 test_normalizer* 系列覆盖。"""

from app.gateway.normalizer import normalize_tool_result
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
