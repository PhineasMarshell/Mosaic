"""P16 harness safeguards and real Finalize telemetry.

Mock socket transport to prove replay cannot connect; CLI subprocesses use
only invalid arguments. Finalize, matcher and SQLite execute normally. No
real THS/LLM service is called and this file cannot establish live success.
"""

import json
import logging
import socket
import subprocess
import sys
from copy import deepcopy
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.errors import LLMOutputError
from app.graph.nodes.finalize import FinalizeAuditNode
from app.graph.nodes.reasoning import ReasoningNode
from app.models.evidence import Evidence
from app.models.response import MarketIntelligence
from app.research.reasoning import _parse_evidence
from scripts.p14_acceptance import _RunEvents
from scripts.p16_acceptance import ROOT, database_observation, observation, offline, validate_row


def quote_state():
    evidence = Evidence(
        id="p16-real-ledger-001", source_tool="internal_iwencai_market_query", domain="a_share",
        metric="close", value=1488.0, instrument="SH600519", as_of_date="2026-10-09",
        source="同花顺问财", authority="同花顺问财", retrieved_at="2026-10-14T01:00:00Z",
        completeness="complete", coverage="instrument",
    )
    return {
        "run_id": "p16-test", "question": "SH600519 在 2026-10-09 的收盘价是多少",
        "domain": "a_share", "requested_date": "2026-10-09", "as_of_date": "2026-10-09",
        "planned_as_of_date": "2026-10-09", "market_closed": False, "results": [], "errors": [],
        "critique": {"verdict": "pass"}, "evidence": [evidence],
        "report": MarketIntelligence(
            what_happened="600519在2026-10-09收盘价为1488元",
            claims=[{"claim": "600519在2026-10-09收盘价为1488元", "claim_type": "fact", "evidence_ids": [evidence.id]}],
            evidence=[evidence.model_dump()],
        ),
    }


@pytest.mark.parametrize("missing", [False, True])
async def test_finalize_records_actual_claim_rejection_and_original_metadata(missing):
    state = quote_state()
    if missing:
        state["report"].claims[0].evidence_ids = []
    collector = _RunEvents()
    logger = logging.getLogger("app.graph.run_log")
    logger.addHandler(collector)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        result = await FinalizeAuditNode(None)(state)
    finally:
        logger.removeHandler(collector)
        logger.setLevel(previous_level)
    assert not result.get("errors")
    audit = next(row for row in collector.rows if row["event"] == "claim_audit")
    summary = next(row for row in collector.rows if row["event"] == "ledger_summary")
    assert summary["ledger_evidence_count"] == 1
    assert audit["supported"] is not missing
    if missing:
        assert "missing_reference" in audit["reason_codes"]
        assert result["delivery_status"] == "blocked"
    else:
        assert result["delivery_status"] == "verified"
        cited = next(row for row in collector.rows if row["event"] == "claim_evidence")
        for key in ("value", "instrument", "as_of_date", "source", "authority", "retrieved_at",
                    "coverage", "completeness", "partial", "result_partial"):
            assert cited[key] == state["evidence"][0].model_dump()[key]


@pytest.mark.parametrize("legacy", [False, True])
async def test_report_hydration_cannot_drop_upstream_partial_before_persistence(tmp_path, legacy):
    from app.agent.persistence import persist_research
    from app.memory.storage import MarketMemory
    from app.models.response import ResearchResponse

    state = quote_state()
    original = state["evidence"][0]
    original.result_partial = True
    selector = {"claim": state["report"].claims[0].claim, "evidence_ids": [original.id]} if legacy else {"id": original.id}
    state["report"].evidence = _parse_evidence([selector], [original])
    # The last persistence boundary has only the hydrated report, not Graph's
    # ledger. Losing result_partial here used to erase a real upstream limit.
    result = ResearchResponse(question=state["question"], requested_date=state["requested_date"],
                              as_of_date=state["as_of_date"], market_closed=False, report=state["report"],
                              run_id="partial-hydration", critique={"verdict": "pass"},
                              final_audit_status="pass", delivery_status="verified")
    memory = MarketMemory(db_path=tmp_path / "memory.db")
    try:
        assert await persist_research(result, result.question, "p16-partial", memory=memory) is False
        assert "partial_only" in result.reason_codes
        assert memory.conn.execute("SELECT COUNT(*) FROM research_records").fetchone()[0] == 0
        assert result.report.evidence[0].instrument == "SH600519"
    finally:
        memory.close()


@pytest.mark.parametrize("closed", [False, True])
async def test_broad_request_keeps_market_ledger_despite_planned_index(monkeypatch, closed):
    # Replace only the LLM response and registry network loader. The real
    # ReasoningNode, engine, prompt construction and parsing execute unchanged.
    from app.config import Settings
    from app.research import reasoning
    from app.research.entity_registry import RegistrySnapshot

    date = "2026-10-09" if closed else "2026-10-14"
    index = Evidence(id="index-001", source_tool="internal_iwencai_index_query", domain="a_share",
                     metric="close", value=3400.0, instrument="SH000001", instrument_name="上证指数",
                     source="同花顺问财", authority="同花顺问财", retrieved_at="2026-10-14T01:00:00Z",
                     completeness="complete", coverage="index", as_of_date=date)
    breadth = index.model_copy(update={"id": "breadth-001", "metric": "up_count", "value": 3000,
                                      "instrument": None, "instrument_name": None, "coverage": "market"})
    claim = f"上证指数在{date}收盘价为3400.0点"
    model_payload = {"market_state": "有限观察", "what_happened": claim,
                     "evidence": [{"id": "E001"}],
                     "claims": [{"claim": claim, "claim_type": "fact", "evidence_ids": ["E001"]}]}
    completion = AsyncMock(return_value=SimpleNamespace(
        usage=None, choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
            content=json.dumps(model_payload, ensure_ascii=False)))],
    ))
    node = ReasoningNode(Settings(openai_api_key="offline-fixture"))
    monkeypatch.setattr(node._engine.client.chat.completions, "create", completion)
    monkeypatch.setattr(reasoning, "get_full_a_share_registry", AsyncMock(return_value=RegistrySnapshot()))
    result = await node({
        "run_id": "broad-ledger", "question": "2026-10-11 A 股发生了什么" if closed else "今天 A 股发生了什么",
        "domain": "a_share", "requested_date": "2026-10-11" if closed else date,
        "as_of_date": date, "planned_as_of_date": date, "market_closed": closed,
        "route": [{"tool_calls": [{"arguments": {"symbols": ["000300"]}}]}],
        "results": [], "evidence": [index, breadth], "errors": [],
    })
    assert result.get("errors", []) == []
    assert node._engine.last_target_filter["enabled"] is False
    prompt = completion.call_args.kwargs["messages"][1]["content"]
    assert "SH000001" in prompt and "up_count" in prompt
    assert result["report"].claims[0].evidence_ids == [index.id]
    assert result["fallback_diagnostics"]["enabled"] is False
    finalized = await FinalizeAuditNode(None)({
        "question": "2026-10-11 A 股发生了什么" if closed else "今天 A 股发生了什么",
        "domain": "a_share", "requested_date": "2026-10-11" if closed else date,
        "as_of_date": date, "planned_as_of_date": date, "market_closed": closed,
        "evidence": [index, breadth], "report": result["report"], "results": [], "errors": [],
        "critique": {"verdict": "pass"},
    })
    assert finalized.get("errors", []) == []
    assert finalized["delivery_status"] == "verified"


async def test_short_reference_contract_still_rejects_length_even_for_valid_json(monkeypatch):
    from app.config import Settings
    from app.research.reasoning import ReasoningEngine

    state = quote_state()
    engine = ReasoningEngine(Settings(openai_api_key="offline-fixture"))
    completion = AsyncMock(return_value=SimpleNamespace(
        usage=None, choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content=json.dumps({
            "market_state": "有限事实", "what_happened": state["report"].claims[0].claim,
            "evidence": [{"id": "E001"}], "claims": [state["report"].claims[0].model_dump()],
        })))],
    ))
    monkeypatch.setattr(engine.client.chat.completions, "create", completion)
    with pytest.raises(LLMOutputError, match="truncated"):
        await engine.reason(state["question"], [], state["evidence"], requested_date="2026-10-09", compact_mode=False)
    assert engine.last_finish_reason == "length" and engine.last_parse_status == "invalid_json"


def test_correlated_counts_are_from_their_own_stage():
    payload = {"run_id": "a", "status": "blocked", "reason_codes": [], "persisted": False}
    events = [
        {"event": "executions", "run_id": "b", "results": [{"datum_count": 999}]},
        {"event": "executions", "run_id": "a", "results": [{"datum_count": 13, "status": "success"}]},
        {"event": "reasoning_context", "run_id": "a", "ledger_evidence_count": 11, "selected_evidence_count": 4,
         "finish_reason": "length", "parse_status": "invalid_json"},
        {"event": "ledger_summary", "run_id": "a", "ledger_evidence_count": 12},
    ]
    row = observation(payload, events, scenario="quote", transport="sync", http_status=200,
                      heartbeat_count=0, terminal_count=1)
    assert (row["collected_datum_count"], row["ledger_evidence_count"], row["report_evidence_count"], row["claim_count"]) == (13, 12, 0, 0)
    assert row["finish_reason"] == "length" and row["parse_status"] == "invalid_json"
    assert len(row["actual_routes"]) == 1


@pytest.mark.parametrize("claim_type", ["fact", "structure"])
def test_closed_day_template_is_never_market_success_even_if_verified(claim_type):
    row = observation({"run_id": "closed-fixture", "status": "verified", "persisted": True,
                       "requested_date": "2026-10-11", "as_of_date": "2026-10-09", "market_closed": True,
                       "report": {"claims": [{"claim": "请求日2026-10-11 A股休市", "claim_type": claim_type,
                                              "evidence_ids": []}], "evidence": []}}, [],
                      scenario="closed_day", transport="sync", http_status=200, heartbeat_count=0,
                      terminal_count=1, origin="offline_fixture")
    assert row["pure_structure"] is True
    assert row["market_success"] is False
    assert row["observation_origin"] == "offline_fixture"


def test_replay_guard_blocks_dns_and_connections():
    with offline():
        with pytest.raises(AssertionError, match="never access"):
            socket.getaddrinfo("example.com", 443)
        with socket.socket() as client:
            with pytest.raises(AssertionError, match="never access"):
                client.connect(("127.0.0.1", 443))


@pytest.mark.parametrize("args", [[], ["--mode", "probe"], ["--mode", "replay", "--unknown"],
                                 ["--mode", "replay", "--scenario", "quote"], ["--mod", "live"]])
def test_cli_invalid_arguments_cannot_start_live(args):
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "p16_acceptance.py"), *args],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "Received question" not in result.stdout


def test_database_observation_is_committed_and_uses_existing_retrieval(tmp_path):
    observed = database_observation(tmp_path / "memory.db", "explicit-conversation", "absent")
    assert observed["persistence_counts"] == {"research_records": 0, "conversations": 0, "daily_states": 0}
    assert observed["history_retrieval"]["research_run_readable"] is False
    assert observed["history_retrieval"]["conversation_readable"] is False


@pytest.mark.parametrize("request_error", [False, True])
def test_live_harness_retains_final_cleanup_and_business_exception(monkeypatch, tmp_path, request_error):
    # Only harness transport is replaced; the fake response establishes no
    # Graph or service result. Real SQLite observation and cleanup are tested.
    import fastapi.testclient

    from app.memory import storage
    from scripts import p16_acceptance as harness

    payload = {"run_id": "cleanup-fixture", "status": "blocked", "code": "blocked",
               "final_audit_status": "revise_exhausted", "reason_codes": [], "persisted": False}

    class Client:
        def __init__(self, app):
            pass

        def post(self, endpoint, json):
            if request_error:
                raise RuntimeError("business failure")
            return SimpleNamespace(status_code=200, json=lambda: payload)

        def close(self):
            raise PermissionError("cleanup denied")

    monkeypatch.setattr(fastapi.testclient, "TestClient", Client)
    monkeypatch.setattr(storage, "_shared_memory", None)
    monkeypatch.setattr(harness, "ROOT", tmp_path)
    monkeypatch.setattr(harness, "observation", partial(harness.observation, origin="offline_fixture"))
    artifact = tmp_path / "acceptance.jsonl"
    args = SimpleNamespace(scenario=["quote"], transport="sync", artifact=artifact, label="fixture")
    if request_error:
        with pytest.raises(RuntimeError, match="business failure"):
            harness.live(args)
    else:
        harness.live(args)
    rows = [json.loads(line) for line in artifact.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["cleanup_status"] == "warning"
    assert rows[0]["observation_origin"] == "offline_fixture"
    assert rows[0]["cleanup_exception_types"] == ["PermissionError"]
    assert rows[0]["persistence_counts"] == dict.fromkeys(("research_records", "conversations", "daily_states"), 0)
    if request_error:
        assert rows[0]["business_exception_type"] == "RuntimeError"
        assert "run_complete" not in {row["kind"] for row in rows}
    else:
        assert rows[-1]["cleanup_status"] == "warning"


@pytest.mark.parametrize("mutation", ["persisted", "terminal", "cleanup", "forged", "missing"])
def test_artifact_replay_rejects_unsafe_observation(mutation):
    state = quote_state()
    row = observation({"run_id": "fixture", "status": "degraded", "final_audit_status": "revise_exhausted",
                       "persisted": False, "reason_codes": [], "as_of_date": "2026-10-09",
                       "requested_date": "2026-10-09", "report": state["report"].model_dump()}, [],
                      scenario="quote", transport="sse", http_status=200, heartbeat_count=2, terminal_count=1)
    row.update(cleanup_status="ok", ledger_evidence_count=1, persistence_counts=dict.fromkeys(
        ("research_records", "conversations", "daily_states"), 0))
    validate_row(row)
    broken = deepcopy(row)
    if mutation == "persisted":
        broken["persisted"] = True
    elif mutation == "terminal":
        broken["terminal_event_count"] = 2
    elif mutation == "cleanup":
        broken["cleanup_status"] = "pending"
    elif mutation == "forged":
        broken["report"]["claims"][0]["evidence_ids"] = ["forged-001"]
    else:
        broken["report"]["claims"][0]["evidence_ids"] = []
    with pytest.raises(AssertionError):
        validate_row(broken)
    assert json.loads(json.dumps(row))["persisted"] is False
