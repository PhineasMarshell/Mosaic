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
