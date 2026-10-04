"""T4 回归：Evidence id 必须跨 analyst 唯一；重复 id 时 reasoning 按 (id, source_tool) 消解。"""

import logging

from app.models.evidence import Evidence
from app.models.market import NormalizedDatum, ToolResult
from app.research.evidence import build_evidence
from app.research.reasoning import _parse_evidence


def _mkresult(tool: str, metric: str, value) -> ToolResult:
    return ToolResult(
        tool=tool,
        arguments={},
        status="success",
        normalized=[NormalizedDatum(metric=metric, value=value, tool=tool)],
    )


def test_different_prefixes_produce_disjoint_ids():
    r1 = build_evidence([_mkresult("ta", "x", 1)], id_prefix="technical")
    r2 = build_evidence([_mkresult("tb", "y", 2)], id_prefix="fundamental")
    assert {e.id for e in r1}.isdisjoint({e.id for e in r2})


def test_default_prefix_is_evidence():
    r = build_evidence([_mkresult("ta", "x", 1)])
    assert r[0].id == "evidence-001"


def test_merged_analyst_evidence_has_unique_ids():
    merged = (
        build_evidence([_mkresult("ta", "x", 1)], id_prefix="technical")
        + build_evidence([_mkresult("tb", "y", 2)], id_prefix="fundamental")
        + build_evidence([_mkresult("tc", "z", 3)], id_prefix="moneyflow")
    )
    ids = [e.id for e in merged]
    assert len(ids) == len(set(ids))


def test_duplicate_ids_resolved_by_source_tool(caplog):
    # 保证 warning 能到 caplog（避免全局 propagate=False 干扰）。
    target_logger = logging.getLogger("app.research.reasoning")
    saved_propagate = target_logger.propagate
    target_logger.propagate = True
    try:
        original = [
            Evidence(id="dup-001", source_tool="sentiment_tool", metric="m", value=54),
            Evidence(id="dup-001", source_tool="longhu_tool", metric="m", value=12.5),
        ]
        raw = [
            {"id": "dup-001", "source_tool": "sentiment_tool"},
            {"id": "dup-001", "source_tool": "longhu_tool"},
        ]
        items = _parse_evidence(raw, original)
        by_source = {i.source_tool: i.value for i in items}
        assert by_source["sentiment_tool"] == 54
        assert by_source["longhu_tool"] == 12.5
        assert any("重复 evidence id" in r.getMessage() for r in caplog.records)
    finally:
        target_logger.propagate = saved_propagate
