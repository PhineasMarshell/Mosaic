"""Stage two regressions for evidence identity, references and review coverage."""

import json
import types

from app.config import Settings
from app.gateway.tool_registry import resolve_tool
from app.graph.nodes.critic import _coverage_issues, _format_evidence_for_review
from app.graph.nodes.reasoning import _select_evidence
from app.graph.run_log import log_executions, log_plan
from app.graph.state import _merge_evidence
from app.models.evidence import Evidence
from app.models.market import NormalizedDatum, ToolResult
from app.research.entity_check import check_report_entities, extract_entity_mentions
from app.research.evidence import build_evidence
from app.research.reasoning import ReasoningEngine, _parse_evidence


def _result(tool="get_market_quotes", metric="price", value=1, *, key="quote", timestamp="2026-10-09"):
    return ToolResult(tool=tool, operation_id=tool, tool_key=key, arguments={"symbol": "600519"},
        status="success", normalized=[NormalizedDatum(tool=tool, metric=metric, value=value, timestamp=timestamp)])


def test_stable_ids_survive_new_results_and_reducer_loop():
    first = build_evidence([_result(metric="price", value=1)], id_prefix="technical", stable_ids=True)
    second = build_evidence([_result(metric="volume"), _result(metric="price", value=2)],
                            id_prefix="technical", stable_ids=True)
    merged = _merge_evidence(first, second)
    assert len({item.id for item in merged}) == 2
    assert next(item for item in merged if item.metric == "price").value == 2
    assert next(item for item in merged if item.metric == "price").id == first[0].id
    assert first[0].tool_key == "quote"
    assert first[0].operation_id == first[0].source_tool == "get_market_quotes"


def test_duplicate_legacy_id_without_source_is_ambiguous():
    ledger = [Evidence(id="dup-001", source_tool="a", metric="x", value=1),
              Evidence(id="dup-001", source_tool="b", metric="x", value=2)]
    assert _parse_evidence([{"id": "dup-001"}], ledger) == []


def test_report_evidence_preserves_original_partial_note():
    original = Evidence(id="technical-001", source_tool="get_ashare_sentiment", metric="up_count",
                        value=100, status="partial", partial=True, note="仅返回前100条")
    parsed = _parse_evidence([{"id": "technical-001", "note": "样本有限"}], [original])[0]
    assert parsed.status == "partial" and parsed.partial
    assert parsed.note == "仅返回前100条；样本有限"


async def test_invalid_ids_removed_from_claims_evidence_and_body():
    payload = {"what_happened": "见 technical-002 和 technical-003", "market_state": "mixed",
               "evidence": [{"id": "technical-004", "value": 999}],
               "claims": [{"claim": "上涨", "evidence_ids": ["technical-006"]}],
               "confidence": "high"}
    engine = ReasoningEngine(Settings())

    async def create(**kwargs):
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)))])

    engine.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    report = await engine.reason("q", [], [Evidence(id="technical-001", source_tool="quote", metric="price", value=1)])
    assert report.evidence == [] and report.claims == []
    assert all(x not in report.what_happened for x in ("technical-002", "technical-003"))
    assert "technical-004" not in [e.id for e in report.evidence]
    assert len(report.evidence_violations) == 3
    assert report.confidence == "low"


def test_critic_keeps_market_datums_and_records_omissions():
    datums = [NormalizedDatum(tool="get_ashare_sentiment", metric=f"noise_{i}", value=i) for i in range(395)]
    datums += [NormalizedDatum(tool="get_ashare_sentiment", metric="up_count", value=2000,
                               timestamp="2026-10-09", note="partial feed"),
               NormalizedDatum(tool="get_ashare_sentiment", metric="down_count", value=3000,
                               timestamp="2026-10-09")]
    result = ToolResult(tool="get_ashare_sentiment", operation_id="get_ashare_sentiment",
                        tool_key="sentiment", arguments={}, status="partial", partial=True,
                        note="截断", normalized=datums)
    text, stats = _format_evidence_for_review([result], [], None)
    assert "up_count" in text and "down_count" in text and "partial feed" in text
    assert "partial=True" in text and "note=截断" in text
    assert stats["datums_omitted"] == 387
    assert any(row["reason"] == "per_tool_datum_limit" for row in stats["omitted"])
    assert any(row["reason"] == "market_minimum" for row in stats["kept"])


def test_reasoning_truncation_keeps_prior_reference_and_market_minimum():
    items = [Evidence(id=f"technical-{i}", source_tool="other", metric="noise", value=i) for i in range(600)]
    items[0].id = "technical-reference"
    items[1].metric = "up_count"
    kept, stats = _select_evidence(items, {"claims": [{"evidence_ids": ["technical-reference"]}]})
    assert "technical-reference" in {item.id for item in kept}
    assert any(item.metric == "up_count" for item in kept)
    assert stats["omitted"] > 0 and stats["kept_reasons"]["report_reference"] == 1


def test_partial_and_undated_breadth_cannot_support_all_market_claim():
    report = {"what_happened": "全市场全部上涨。"}
    evidence = [Evidence(id="a", source_tool="get_ashare_sentiment", metric="up_count", value=5000,
                         status="partial", partial=True, timestamp="2026-10-09"),
                Evidence(id="b", source_tool="get_ashare_sentiment", metric="down_count", value=0)]
    assert _coverage_issues(report, evidence)[0].kind == "unsupported_claim"
    assert _coverage_issues({"what_happened": "无法确认全市场全部上涨。"}, evidence) == []


def test_unsupported_causation_and_fund_rotation_get_issues():
    for sentence in ("OpenAI营收不及预期导致AI硬件抛售。", "资金从AI/光通信流向锂电池。",
                     "内房股受港股上涨带动。"):
        assert _coverage_issues({"what_happened": sentence}, [])
        assert _coverage_issues({"what_happened": "可能" + sentence}, []) == []


def test_dated_direct_relation_and_complete_breadth_are_accepted():
    statement = "OpenAI营收不及预期导致AI硬件抛售"
    direct = Evidence(id="news-1", source_tool="internal_news_digest", metric="news_1",
                      value=statement, timestamp="2026-10-09 10:00")
    assert _coverage_issues({"what_happened": statement}, [direct]) == []
    assert _coverage_issues({"what_happened": "2026-10-08 " + statement}, [direct])
    breadth = [Evidence(id="a", source_tool="get_ashare_sentiment", metric="up_count", value=5000,
                        timestamp="2026-10-09"),
               Evidence(id="b", source_tool="get_ashare_sentiment", metric="down_count", value=0,
                        timestamp="2026-10-09")]
    assert _coverage_issues({"what_happened": "全市场全部上涨。"}, breadth) == []


def test_entity_leads_removed_and_news_link_required():
    assert extract_entity_mentions("包括广合科技、例如胜宏科技、涉及行云科技") == ["广合科技", "胜宏科技", "行云科技"]
    unknown, _ = check_report_entities("包括行云科技", [Evidence(id="a", source_tool="other",
                                                    metric="price", value="行云科技")])
    assert unknown == ["行云科技"]
    unknown, _ = check_report_entities("包括行云科技", [Evidence(id="b", source_tool="internal_news_digest",
                                                    metric="news_1", value={"title": "行云科技发布公告"})])
    assert unknown == []


def test_quote_symbols_keep_entity_code_link():
    result = ToolResult(tool="get_market_quotes", operation_id="get_market_quotes", tool_key="quote",
                        arguments={"symbols": ["600519"]}, status="success",
                        normalized=[NormalizedDatum(tool="get_market_quotes", metric="price", value=1800)])
    evidence = build_evidence([result], id_prefix="technical", stable_ids=True)
    assert evidence[0].instrument == "600519"
    unknown, _ = check_report_entities("贵州茅台上涨", evidence)
    assert "贵州茅台" not in unknown


def test_internal_tool_identity_consistent_in_plan_execution_and_evidence(monkeypatch):
    events = []
    monkeypatch.setattr("app.graph.run_log.emit", lambda event, **fields: events.append((event, fields)))
    for key in ("telegraph", "news_digest"):
        operation = resolve_tool(key).tool_name
        step = types.SimpleNamespace(tool_key=key, arguments={}, priority="high")
        log_plan({}, types.SimpleNamespace(steps=[step]))
        result = _result(tool=operation, key=key)
        log_executions({}, [result])
        evidence = build_evidence([result], id_prefix="news", stable_ids=True)[0]
        assert events[-2][1]["steps"][0]["source_tool"] == operation
        assert events[-1][1]["results"][0]["source_tool"] == operation
        assert (evidence.tool_key, evidence.operation_id, evidence.source_tool) == (key, operation, operation)
