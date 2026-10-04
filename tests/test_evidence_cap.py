"""T6 回归：build_evidence 必须把证据链硬截到 _MAX_EVIDENCE_ITEMS 并保留最新、说明截断。"""

from app.models.market import NormalizedDatum, ToolResult
from app.research.evidence import _MAX_EVIDENCE_ITEMS, build_evidence


def _many_results(tools: int, per_tool: int) -> list[ToolResult]:
    results = []
    for t in range(tools):
        tool = f"tool_{t}"
        data = [NormalizedDatum(metric=f"m_{t}_{i}", value=i, tool=tool) for i in range(per_tool)]
        results.append(ToolResult(tool=tool, arguments={}, status="success", normalized=data))
    return results


def test_evidence_capped_and_newest_kept():
    evidence = build_evidence(_many_results(tools=12, per_tool=50))

    assert len(evidence) == _MAX_EVIDENCE_ITEMS
    # 每个来源都有代表
    assert len({e.source_tool for e in evidence}) == 12

    # tool_0 保留的是其最新（最高 index）条目
    tool0_values = [e.value for e in evidence if e.source_tool == "tool_0"]
    assert min(tool0_values) >= 43
    assert max(tool0_values) == 49

    noted = [e for e in evidence if e.note and "上限" in e.note]
    assert noted, "截断必须留下说明 note"
    assert "520" in noted[0].note


def test_evidence_under_cap_unchanged():
    evidence = build_evidence(_many_results(tools=1, per_tool=5))
    assert len(evidence) == 5
    assert not [e for e in evidence if e.note and "上限" in e.note]
