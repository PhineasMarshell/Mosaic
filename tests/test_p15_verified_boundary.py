"""P15 uses real audit/response/persistence code and temporary SQLite stores.

Only the API orchestrator is stubbed to replay a deterministic state; network
tools and LLM output quality are not covered. No audit node is replaced.
"""

from __future__ import annotations

import json
import logging
import types

import pytest
from fastapi.testclient import TestClient

from app.agent.evidence_gate import match_claims_to_evidence
from app.agent.persistence import persist_research
from app.graph.nodes.finalize import FinalizeAuditNode
from app.memory.storage import MarketMemory
from app.models.evidence import Evidence
from app.models.response import MarketIntelligence, ResearchResponse, build_response_from_state
from scripts.p14_acceptance import _row, _RunEvents, _telemetry

FACT = "贵州茅台(600519)在2026-10-09收盘价为1488元"


def _quote(**changes) -> Evidence:
    return Evidence(**{
        "id": "p15-quote-001", "source_tool": "internal_iwencai_market_query",
        "domain": "a_share", "tool_key": "quote", "metric": "close", "value": 1488.0,
        "instrument": "SH600519", "instrument_name": "贵州茅台", "as_of_date": "2026-10-09",
        "retrieved_at": "2026-10-14T01:00:00Z", "source": "同花顺问财", "authority": "同花顺问财",
        "completeness": "complete", "coverage": "instrument", "status": "success",
        "partial": False, "result_partial": False, **changes,
    })


def _state(claims: list[dict], evidence: list[Evidence] | None = None, **changes) -> dict:
    evidence = evidence or []
    report = MarketIntelligence(
        requested_date=changes.get("requested_date", "2026-10-09"),
        as_of_date=changes.get("as_of_date", "2026-10-09"), market_closed=changes.get("market_closed", False),
        market_state="窄行情事实", what_happened="；".join(item["claim"] for item in claims),
        claims=claims, evidence=[item.model_dump() for item in evidence],
    )
    return {
        "run_id": "p15-run", "question": "SH600519 2026-10-09收盘价", "domain": "a_share",
        "requested_date": "2026-10-09", "planned_as_of_date": "2026-10-09",
        "as_of_date": "2026-10-09", "market_closed": False, "report": report,
        "evidence": evidence, "results": [], "critique": {"verdict": "pass", "reason": "model passed"},
        "errors": [], **changes,
    }


def _counts(memory: MarketMemory) -> dict:
    return {table: memory.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("research_records", "conversations", "daily_states", "anomalies")}


@pytest.mark.parametrize("ids", [[], None, "absent"])
@pytest.mark.parametrize("claim", [
    "贵州茅台收盘价为1488元", "600519今日上涨", "全市场成交额为一万亿元", "贵州茅台发布公告",
    "贵州茅台已上市", "暂无可核验数据，但贵州茅台收盘价为1488元", "未验证；贵州茅台发布公告",
])
async def test_unreferenced_positive_claim_is_blocked(ids, claim, tmp_path):
    row = {"claim": claim, "claim_type": "fact"}
    if ids != "absent":
        row["evidence_ids"] = ids
    # Dict input exercises absent/None references before Pydantic normalization.
    state = _state([])
    state["report"] = {"claims": [row], "what_happened": claim}
    out = await FinalizeAuditNode(None)(state)
    assert out["delivery_status"] == "blocked"
    assert out["final_audit_status"] == "revise_exhausted"
    assert out["report"] is None
    assert {"missing_reference", "no_verified_claim", "verified_provenance_failed"} <= set(out["reason_codes"])
    assert not out.get("errors")
    result = build_response_from_state({**state, **out}, state["question"], "p15")
    memory = MarketMemory(db_path=tmp_path / "memory.db")
    try:
        assert await persist_research(result, result.question, "p15", memory=memory) is False
        assert result.persisted is False
        assert set(_counts(memory).values()) == {0}
    finally:
        memory.close()


async def test_mixed_claims_are_filtered_and_never_persist(tmp_path):
    evidence = _quote()
    unsafe = "贵州茅台发布重大公告"
    state = _state([
        {"claim": FACT, "claim_type": "fact", "evidence_ids": [evidence.id]},
        {"claim": unsafe, "claim_type": "fact", "evidence_ids": []},
    ], [evidence])
    out = await FinalizeAuditNode(None)(state)
    assert out["delivery_status"] == "degraded"
    assert {"missing_reference", "verified_provenance_failed"} <= set(out["reason_codes"])
    assert [row.claim for row in out["report"].claims] == [FACT]
    assert unsafe not in out["report"].what_happened
    assert not out.get("errors")
    result = build_response_from_state({**state, **out}, state["question"], "p15")
    memory = MarketMemory(db_path=tmp_path / "memory.db")
    try:
        assert await persist_research(result, result.question, "p15", memory=memory) is False
        assert set(_counts(memory).values()) == {0}
    finally:
        memory.close()


@pytest.mark.parametrize("claim", [
    "请求日 2026-10-10 A股休市", "最近交易日 2026-10-09 行情无法确认", "暂无可核验数据", "未验证",
])
async def test_code_owned_calendar_and_gap_claims_remain_allowed(claim):
    state = _state([{"claim": claim, "claim_type": "structure", "evidence_ids": []}],
                   requested_date="2026-10-10", market_closed=True)
    out = await FinalizeAuditNode(None)(state)
    assert (out["final_audit_status"], out["delivery_status"]) == ("pass", "verified")
    assert out["reason_codes"] == []
    assert not out.get("errors")
    result = build_response_from_state({**state, **out}, state["question"])
    assert result.delivery_status == "verified"
    assert result.report.claims[0].evidence_ids == []


@pytest.mark.parametrize("claim,closed", [
    ("请求日 2026-10-10 A股休市；贵州茅台发布公告", True),
    ("请求日 2026-10-10 A股休市", False),
    ("2026-10-11 A股休市", True),
    ("贵州茅台收盘价为1488元，未验证", True),
    ("数据可能不足，今天上涨", True),
    ("贵州茅台发布公告，暂无可核验数据", True),
])
async def test_structure_label_or_caveat_cannot_exempt_positive_claim(claim, closed):
    state = _state([{"claim": claim, "claim_type": "structure", "evidence_ids": []}],
                   requested_date="2026-10-10", market_closed=closed)
    out = await FinalizeAuditNode(None)(state)
    assert out["delivery_status"] == "blocked"
    assert "missing_reference" in out["reason_codes"]
    assert not out.get("errors")


@pytest.mark.parametrize("verdict", ["pass", "revise", "research_more"])
async def test_forged_and_empty_references_both_reach_terminal_codes(verdict):
    evidence = _quote()
    state = _state([
        {"claim": FACT, "evidence_ids": [evidence.id]},
        {"claim": "贵州茅台发布公告", "evidence_ids": ["unknown-forged-001"]},
        {"claim": "贵州茅台已上市", "evidence_ids": []},
    ], [evidence], critique={"verdict": verdict})
    out = await FinalizeAuditNode(None)(state)
    assert out["delivery_status"] == "blocked"
    assert {"invalid_reference", "missing_reference"} <= set(out["reason_codes"])
    assert not out.get("errors")


async def test_empty_ledger_cannot_be_replaced_by_model_report_evidence():
    evidence = _quote()
    state = _state([{"claim": FACT, "evidence_ids": [evidence.id]}], [evidence])
    state["evidence"] = []
    out = await FinalizeAuditNode(None)(state)
    assert out["delivery_status"] == "blocked"
    assert "invalid_reference" in out["reason_codes"]
    assert not out.get("errors")


@pytest.mark.parametrize("changes,code", [
    ({"as_of_date": None}, "missing_as_of_date"),
    ({"as_of_date": "2026-10-08"}, "stale_date"),
    ({"instrument": "SZ000001"}, "unmatched_instrument"),
    ({"domain": "us_stock"}, "domain_mismatch"),
    ({"source": None}, "missing_source"),
    ({"authority": None}, "missing_authority"),
    ({"retrieved_at": None}, "missing_retrieved_at"),
    ({"coverage": "unknown"}, "unknown_coverage"),
    ({"completeness": "unknown"}, "unknown_completeness"),
    ({"partial": True}, "partial_only"),
    ({"metric": ""}, "invalid_evidence"),
    ({"value": None}, "invalid_evidence"),
    ({"source_tool": "unknown"}, "missing_source_tool"),
])
async def test_good_citation_never_hides_bad_citation(changes, code):
    good = _quote()
    bad = _quote(id="p15-bad-002", **changes)
    state = _state([{"claim": FACT, "evidence_ids": [good.id, bad.id]}], [good, bad])
    out = await FinalizeAuditNode(None)(state)
    assert out["delivery_status"] != "verified"
    assert code in out["reason_codes"]
    assert not out.get("errors")


async def test_complete_narrow_fact_in_partial_result_can_be_verified():
    evidence = _quote(result_partial=True)
    state = _state([{"claim": FACT, "evidence_ids": [evidence.id]}], [evidence])
    out = await FinalizeAuditNode(None)(state)
    assert out["delivery_status"] == "verified"
    assert "partial_only" not in out["reason_codes"]
    assert not out.get("errors")


def test_default_unknown_completeness_is_not_strictly_verified():
    values = _quote().model_dump()
    values.pop("completeness")
    evidence = Evidence(**values)
    rows = match_claims_to_evidence({"claims": [{"claim": FACT, "evidence_ids": [evidence.id]}]},
                                    [evidence], strict_provenance=True)
    assert rows[0]["supported"] is False
    assert "unknown_completeness" in rows[0]["reason_codes"]


@pytest.mark.parametrize("claim_type", ["fact", "inference", "single_source", "comparison", "causation", "other"])
async def test_claim_type_never_waives_missing_reference(claim_type):
    state = _state([{"claim": "贵州茅台收盘价为1488元", "claim_type": claim_type, "evidence_ids": []}])
    out = await FinalizeAuditNode(None)(state)
    assert out["delivery_status"] == "blocked" and out["report"] is None
    assert "missing_reference" in out["reason_codes"]
    assert not out.get("errors")


async def test_verified_requires_report_and_deliverable_claim():
    empty = await FinalizeAuditNode(None)(_state([]))
    assert empty["delivery_status"] == "blocked"
    assert "no_verified_claim" in empty["reason_codes"]
    absent = await FinalizeAuditNode(None)(_state([], report=None))
    assert absent["delivery_status"] == "failed"
    assert "no_report" in absent["reason_codes"]
    assert not empty.get("errors") and not absent.get("errors")


def test_response_builder_keeps_strict_rejection_reasons_without_finalize():
    state = _state([{"claim": "贵州茅台收盘价为1488元", "evidence_ids": []}])
    result = build_response_from_state(state, state["question"])
    assert result.delivery_status == "blocked"
    assert result.report is None
    assert {"missing_reference", "no_verified_claim", "verified_provenance_failed"} <= set(result.reason_codes)
    assert result.blocked_reason == "verified_provenance_failed"
    assert result.errors == []


async def test_forged_verified_response_is_rejected_before_any_memory_access(tmp_path, caplog, monkeypatch):
    memory = MarketMemory(db_path=tmp_path / "memory.db")
    try:
        memory.save_daily_state(data={"state_label": "previous"})
        before = _counts(memory)
        report = MarketIntelligence(
            what_happened="贵州茅台收盘价为1488元",
            claims=[{"claim": "贵州茅台收盘价为1488元", "claim_type": "fact", "evidence_ids": []}],
        )
        result = ResearchResponse(question="贵州茅台收盘价", report=report, critique={"verdict": "pass"},
                                  final_audit_status="pass", delivery_status="verified", persisted=True)
        with caplog.at_level(logging.WARNING, logger="app.agent.persistence"):
            assert await persist_research(result, result.question, "p15", memory=memory) is False
        assert result.persisted is False
        assert {"missing_reference", "unverified_claim"} <= set(result.reason_codes)
        assert "missing_reference" in caplog.text
        assert _counts(memory) == before
        assert memory.get_daily_state()["state_label"] == "previous"
        def forbidden_memory_init():
            raise AssertionError("a rejected report must never initialize real memory")
        monkeypatch.setattr("app.memory.storage.get_memory", forbidden_memory_init)
        assert await persist_research(result, result.question, "p15") is False
    finally:
        memory.close()


async def test_complete_claim_still_persists_all_targets(tmp_path):
    evidence = _quote()
    state = _state([{"claim": FACT, "evidence_ids": [evidence.id]}], [evidence])
    out = await FinalizeAuditNode(None)(state)
    assert out["delivery_status"] == "verified"
    assert not out.get("errors")
    result = build_response_from_state({**state, **out}, state["question"], "p15")
    memory = MarketMemory(db_path=tmp_path / "memory.db")
    try:
        assert await persist_research(result, result.question, "p15", memory=memory) is True
        assert result.persisted is True
        assert _counts(memory) == {"research_records": 1, "conversations": 1, "daily_states": 1, "anomalies": 0}
        assert FACT[:50] in memory.get_conversation_history("p15")
    finally:
        memory.close()


def test_sync_and_sse_reject_same_unreferenced_pass(monkeypatch, tmp_path):
    from app import main

    state = _state([{"claim": "贵州茅台收盘价为1488元", "evidence_ids": []}])
    class Graph:
        async def astream(self, *_args, **_kwargs):
            yield "values", dict(state)
    class Orchestrator:
        async def run(self, question, domain=None, conversation_id=None):
            return build_response_from_state(dict(state), question, conversation_id)
        def _ensure_graph(self):
            return Graph()
    memory = MarketMemory(db_path=tmp_path / "memory.db")
    monkeypatch.setattr(main, "_get_orchestrator", lambda: Orchestrator())
    monkeypatch.setattr("app.memory.storage.get_memory", lambda: memory)
    monkeypatch.setattr(main, "_settings", types.SimpleNamespace(
        research_budget_seconds=30, stream_heartbeat_seconds=5, graph_recursion_limit=25,
    ))
    try:
        with TestClient(main.app) as client:
            sync = client.post("/api/ask", json={"question": state["question"]}).json()
            text = client.post("/api/ask/stream", json={"question": state["question"]}).text
        terminal = [json.loads(line[6:]) for line in text.splitlines()
                    if line.startswith("data: ") and json.loads(line[6:]).get("terminal") is True]
        assert len(terminal) == 1
        for payload in (sync, terminal[0]):
            assert payload["delivery_status"] == "blocked"
            assert payload["report"] is None and payload["persisted"] is False
            assert "missing_reference" in payload["reason_codes"]
        assert set(_counts(memory).values()) == {0}
    finally:
        memory.close()


def test_p14_ledger_counts_use_correlated_logs_even_when_report_is_filtered():
    payload = {"run_id": "p15-telemetry", "report": {"evidence": [{"id": "kept"}], "claims": []},
               "tool_results": [{"normalized": [{}, {}]}]}
    row = _row(payload, scenario="market", transport="sync", status_code=200)
    assert row["ledger_evidence_count"] is None
    events = [
        {"run_id": "other", "event": "reasoning_context", "ledger_evidence_count": 999},
        {"run_id": "p15-telemetry", "event": "reasoning_context", "ledger_evidence_count": 46,
         "finish_reason": "length", "parse_status": "invalid_json"},
        {"run_id": "p15-telemetry", "event": "critic_context", "ledger_evidence_count": 46},
        {"run_id": "p15-telemetry", "event": "executions", "results": [{"datum_count": 25}]},
    ]
    _telemetry(row, events)
    assert (row["ledger_evidence_count"], row["report_evidence_count"], row["collected_datum_count"]) == (46, 1, 25)
    assert row["finish_reason"] == "length" and row["parse_status"] == "invalid_json"
    blocked = _row({"run_id": "p15-telemetry", "report": None}, scenario="market", transport="sse", status_code=200)
    _telemetry(blocked, events)
    assert blocked["ledger_evidence_count"] == 46 and blocked["report_evidence_count"] == 0
    missing = _row(payload, scenario="market", transport="sync", status_code=200)
    _telemetry(missing, [])
    assert missing["ledger_evidence_count"] is None and missing["collected_datum_count"] == 2


@pytest.mark.parametrize("wrapped", [False, True])
def test_p14_log_collector_accepts_direct_and_wrapped_events(wrapped):
    event = {"run_id": "p15-telemetry", "event": "reasoning_context", "ledger_evidence_count": 46}
    message = json.dumps({"msg": json.dumps(event)}) if wrapped else json.dumps(event)
    collector = _RunEvents()
    collector.emit(logging.LogRecord("app.graph.run_log", logging.INFO, __file__, 1, message, (), None))
    assert collector.rows == [event]


@pytest.mark.parametrize("cleanup_warning,fail_sse", [(False, False), (True, False), (True, True)])
def test_p14_probe_writes_final_cleanup_status_and_preserves_business_error(
    monkeypatch, tmp_path, cleanup_warning, fail_sse,
):
    from app import main as app_main
    from scripts import p14_acceptance as probe

    artifact = tmp_path / "probe.jsonl"

    class TempDirectory:
        name = str(tmp_path)

        def __init__(self, **_kwargs):
            pass

        def cleanup(self):
            assert not artifact.exists(), "scenario rows must wait for the final cleanup status"
            if cleanup_warning:
                raise PermissionError("cleanup denied")

    payload = {
        "run_id": "p15-probe", "delivery_status": "blocked", "code": "blocked",
        "report": None, "persisted": False, "terminal": True,
    }

    class Client:
        def __init__(self, _app):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, path, **_kwargs):
            if path.endswith("/stream") and fail_sse:
                raise RuntimeError("request failed")
            return types.SimpleNamespace(
                status_code=200, json=lambda: dict(payload),
                text="data: " + json.dumps(payload) + "\n\n",
            )

    def settings():
        return types.SimpleNamespace()

    settings.cache_clear = lambda: None
    monkeypatch.setattr("app.config.get_settings", settings)
    monkeypatch.setattr(probe, "TestClient", Client)
    monkeypatch.setattr(probe.tempfile, "TemporaryDirectory", TempDirectory)
    monkeypatch.setattr(probe, "ARTIFACT", artifact)
    monkeypatch.setattr(probe, "QUESTIONS", {"market": "synthetic probe"})
    monkeypatch.setattr(app_main, "_settings", app_main._settings)
    monkeypatch.setattr(app_main, "_orchestrator", app_main._orchestrator)
    monkeypatch.setenv("MOSAIC_MEMORY_DB", str(tmp_path / "original.db"))
    handlers = list(logging.getLogger("app.graph.run_log").handlers)
    if fail_sse:
        with pytest.raises(RuntimeError, match="request failed"):
            probe.main()
    else:
        probe.main()

    rows = [json.loads(line) for line in artifact.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == (2 if fail_sse else 3)
    assert {row["cleanup_status"] for row in rows} == {"warning" if cleanup_warning else "ok"}
    assert rows[0]["run_id"] == "p15-probe"
    assert logging.getLogger("app.graph.run_log").handlers == handlers
