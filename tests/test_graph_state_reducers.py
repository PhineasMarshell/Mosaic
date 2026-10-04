"""T16 回归：research_more 回环时 results/evidence/findings 不得成倍重复。

旧实现这四个字段全用 operator.add：critic 判 research_more 回 supervisor 后
analyst 再跑一轮，同签名 ToolResult、同 id Evidence、同 analyst finding 全部
追加，reasoning 的 normalized_data 里同一份行情出现两遍。
回环语义应与 route 一致：同键覆盖（保留最新），不同键追加（保留并行 analyst 合并）。
"""

import asyncio

from langgraph.graph import END, StateGraph

from app.graph.state import ResearchState, _merge_evidence, _merge_findings, _merge_results
from app.models.evidence import Evidence
from app.models.market import NormalizedDatum, ToolResult


def _payload(value: float, analyst: str = "technical", symbol: str = "600519") -> dict:
    tr = ToolResult(
        tool="quote_tencent_quote_get",
        arguments={"symbol": symbol},
        status="success",
        normalized=[NormalizedDatum(metric="price", value=value, tool="quote")],
    )
    return {
        "results": [tr],
        "evidence": [Evidence(id=f"{analyst}-001", source_tool="quote_tencent_quote_get", metric="price", value=value)],
        "findings": [
            {
                "analyst": analyst,
                "digest": f"执行了 1 个工具 (value={value})",
                "tools_used": ["quote_tencent_quote_get"],
                "failed": False,
            }
        ],
        "errors": [],
    }


def test_same_signature_loop_back_overwrites():
    """同签名两轮（模拟 research_more 回环后缓存命中的重复执行）→ 各字段只剩 1 条最新值。"""
    g = StateGraph(ResearchState)
    g.add_node("round1", lambda state: _payload(1.0))
    g.add_node("round2", lambda state: _payload(2.0))
    g.set_entry_point("round1")
    g.add_edge("round1", "round2")
    g.add_edge("round2", END)
    final = asyncio.run(g.compile().ainvoke({"question": "q"}, config={"recursion_limit": 10}))

    assert len(final["results"]) == 1
    assert final["results"][0].normalized[0].value == 2.0  # 保留最新一轮
    assert len(final["evidence"]) == 1
    assert final["evidence"][0].value == 2.0
    assert len(final["findings"]) == 1
    # reasoning prompt 拼的就是 results 的 normalized：不得出现两份行情
    normalized_values = [d.value for r in final["results"] for d in r.normalized]
    assert normalized_values == [2.0]


def test_parallel_analysts_still_merge():
    """三个并行 analyst（不同 tool/不同 id）必须照常合并 —— 去重不能破坏并行写入。"""
    a = _merge_results([], _payload(1.0, "technical")["results"])
    b = _merge_results(a, _payload(2.0, "fundamental", symbol="000001")["results"])
    assert len(b) == 2  # 不同 (tool, arguments) 追加

    ev = _merge_evidence([], _payload(1.0, "technical")["evidence"])
    ev = _merge_evidence(ev, _payload(2.0, "fundamental")["evidence"])
    assert [e.id for e in ev] == ["technical-001", "fundamental-001"]

    fd = _merge_findings([], _payload(1.0, "technical")["findings"])
    fd = _merge_findings(fd, _payload(2.0, "fundamental")["findings"])
    assert [f["analyst"] for f in fd] == ["technical", "fundamental"]
