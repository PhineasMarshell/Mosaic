"""P13 回归：真实证据可交付路径、fallback 结构化诊断、sync/SSE 对称与门禁不放水。

覆盖 P13 §一（reasoning 截断后的可交付路径 + 候选拒绝原因）、§二（sync/SSE 同一
套 fallback/finalize 判定与唯一终态）、§三（THS 行情 datum→evidence 字段传播）、
§四（公告代码格式与年月过滤）。

全部数据为本地构造，不访问网络、不写 memory.db。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi.testclient import TestClient

from app import main as main_module
from app.config import Settings
from app.errors import LLMOutputError
from app.gateway.arguments import canonicalize_tool_arguments
from app.graph.nodes.finalize import FinalizeAuditNode
from app.graph.nodes.reasoning import ReasoningNode
from app.main import app
from app.models.evidence import Evidence
from app.models.response import build_terminal_payload, terminal_code_for
from app.research.evidence import build_evidence
from app.research.iwencai import normalize_announcements
from app.research.iwencai_query import normalize_query_data
from app.research.reasoning import (
    FALLBACK_REJECTION_REASONS,
    build_evidence_only_fallback,
    plan_evidence_only_fallback,
)

MARKET_TOOL = "internal_iwencai_market_query"


# ── §一：fallback 结构化诊断 ────────────────────────────────────────────────


def _fallback_row(**overrides) -> Evidence:
    values = {
        "id": "fallback-001",
        "source_tool": "iwencai_market",
        "metric": "iwencai_market[1].收盘价",
        "value": "1800.00",
        "instrument": "SH600519",
        "as_of_date": "2026-10-09",
        "source": "同花顺问财",
        "authority": "同花顺问财",
        "retrieved_at": "2026-10-13T01:00:00+00:00",
        "coverage": "instrument",
        "completeness": "complete",
    }
    values.update(overrides)
    return Evidence(**values)


def test_fallback_rejection_vocabulary_is_fixed_and_every_candidate_is_explained():
    rows = [
        _fallback_row(id="ok"),
        _fallback_row(id="bad-status", status="partial"),
        _fallback_row(id="bad-partial", partial=True),
        _fallback_row(id="bad-envelope", completeness="page"),
        _fallback_row(id="bad-incomplete", completeness="partial"),
        _fallback_row(id="bad-coverage", coverage="unknown"),
        _fallback_row(id="bad-date", as_of_date="2026-10-08"),
        _fallback_row(id="bad-symbol", instrument="SZ000001"),
        _fallback_row(id="bad-source", source=None, authority=None),
        _fallback_row(id="bad-retrieved", retrieved_at=None),
        _fallback_row(id="bad-fact", metric="iwencai_market[1].全市场涨跌家数"),
    ]
    plan = plan_evidence_only_fallback(
        rows, requested_date="2026-10-09", target_instrument="600519"
    )

    assert plan.enabled is True
    assert plan.candidate_count == len(rows)
    assert plan.accepted_count == 1
    assert plan.rejected_count == len(rows) - 1
    assert [item.id for item in plan.accepted] == ["ok"]

    reasons = {item.evidence_id: item.reasons for item in plan.candidates}
    assert reasons["ok"] == ()
    assert reasons["bad-status"] == ("status_not_success",)
    assert reasons["bad-partial"] == ("partial",)
    assert reasons["bad-envelope"] == ("unknown_completeness",)
    assert reasons["bad-incomplete"] == ("incomplete",)
    assert reasons["bad-coverage"] == ("unknown_coverage",)
    assert reasons["bad-date"] == ("date_mismatch",)
    assert reasons["bad-symbol"] == ("symbol_mismatch",)
    assert reasons["bad-source"] == ("missing_source",)
    assert reasons["bad-retrieved"] == ("missing_retrieved_at",)
    assert reasons["bad-fact"] == ("unsupported_fact_type",)

    # 词表闭集：不允许出现未登记的拒绝原因。
    for item in plan.candidates:
        assert set(item.reasons) <= set(FALLBACK_REJECTION_REASONS)

    diagnostics = plan.diagnostics()
    assert diagnostics["candidate_count"] == len(rows)
    assert diagnostics["accepted_count"] == 1
    assert diagnostics["rejected_count"] == len(rows) - 1
    assert diagnostics["rejection_reasons"]["date_mismatch"] == 1
    assert diagnostics["rejection_reasons"]["unknown_completeness"] == 1
    assert diagnostics["candidate_evidence_ids"]["accepted"] == ["ok"]
    assert "bad-date" in diagnostics["candidate_evidence_ids"]["rejected"]
    assert diagnostics["report_evidence_ids"] == ["ok"]

    # §一.3：每一个被拒候选都必须有自己的拒绝原因，一个都不能只剩"被拒"。
    rejections = diagnostics["candidate_rejections"]
    assert set(rejections) == {item.evidence_id for item in plan.candidates if not item.accepted}
    assert len(rejections) == diagnostics["rejected_count"]
    for evidence_id, codes in rejections.items():
        assert codes, f"{evidence_id} rejected without a recorded reason"
        assert set(codes) <= set(FALLBACK_REJECTION_REASONS)
    assert rejections["bad-fact"] == ["unsupported_fact_type"]
    assert rejections["bad-symbol"] == ["symbol_mismatch"]


def test_fallback_decision_and_report_can_never_disagree():
    rows = [_fallback_row(id="a", metric="iwencai_market[1].收盘价"),
            _fallback_row(id="b", metric="iwencai_market[1].成交量", value="1000")]
    plan = plan_evidence_only_fallback(
        rows, requested_date="2026-10-09", target_instrument="SH600519"
    )
    report = build_evidence_only_fallback(
        rows, requested_date="2026-10-09", target_instrument="SH600519", decision=plan
    )
    assert report is not None
    reported = [eid for claim in report.claims for eid in claim.evidence_ids]
    assert reported == list(plan.report_evidence_ids) == ["a", "b"]
    assert all(claim.claim_type == "fact" for claim in report.claims)


def test_fallback_is_inapplicable_without_explicit_date_or_instrument():
    plan = plan_evidence_only_fallback(
        [_fallback_row()], requested_date=None, target_instrument="600519"
    )
    assert plan.enabled is False
    assert plan.reason == "missing_requested_date"
    assert plan.accepted_count == 0
    assert plan.candidates[0].reasons == ("fallback_inapplicable",)
    assert plan.diagnostics()["reason"] == "missing_requested_date"

    plan = plan_evidence_only_fallback(
        [_fallback_row()], requested_date="2026-10-09", target_instrument=None
    )
    assert plan.enabled is False and plan.reason == "missing_target_instrument"


def test_fallback_rejects_conflicting_authorities():
    left = _fallback_row(id="left", source="源A", authority="源A", value="1800.00")
    right = _fallback_row(id="right", source="源B", authority="源B", value="1810.00")
    plan = plan_evidence_only_fallback(
        [left, right], requested_date="2026-10-09", target_instrument="SH600519"
    )
    assert plan.accepted_count == 0
    assert {item.reasons for item in plan.candidates} == {("authority_conflict",)}


def test_fallback_requires_complete_completeness_not_envelope_token():
    """P13 §三的失败模式回归：信封 token（page/search_results）不是数据完整性。"""
    for token in ("page", "search_results", "unknown", ""):
        assert build_evidence_only_fallback(
            [_fallback_row(completeness=token)],
            requested_date="2026-10-09",
            target_instrument="SH600519",
        ) is None
    for token in ("complete", "full"):
        assert build_evidence_only_fallback(
            [_fallback_row(completeness=token)],
            requested_date="2026-10-09",
            target_instrument="SH600519",
        ) is not None


# ── §一 + §二：截断后走同一套 fallback，且终态只有 degraded / blocked ──────


class _RaisingEngine:
    """最小 ReasoningEngine 替身：记录 finish_reason/parse_status 后抛错。"""

    def __init__(self, exc: Exception, *, finish_reason: str | None, parse_status: str | None):
        self._exc = exc
        self.last_finish_reason = finish_reason
        self.last_parse_status = parse_status
        self.last_llm_usage = None
        self.last_llm_duration_ms = 1.0
        self.last_prompt_chars = 10
        self.last_context_build_ms = 1.0
        self.last_parse_duration_ms = 1.0
        self.last_evidence_ids: list[str] = []
        self.last_completion_chars = 4198
        self.last_request_max_tokens = 2048
        self.last_target_filter = None

    async def reason(self, **_kwargs):
        raise self._exc


def _reasoning_state(evidence: list[Evidence], **overrides) -> dict:
    state = {
        "question": "查询贵州茅台 SH600519 在 2026-10-09 的行情",
        "domain": "a_share",
        "run_id": "p13-reasoning",
        "evidence": [item.model_dump() for item in evidence],
        "results": [],
        "requested_date": "2026-10-09",
        "as_of_date": "2026-10-09",
        "planned_as_of_date": "2026-10-09",
        "market_closed": False,
        "errors": [],
        "budget_deadline": time.monotonic() + 60,
        "started_at": time.monotonic(),
    }
    state.update(overrides)
    return state


def _truncated_node(**kwargs) -> ReasoningNode:
    node = ReasoningNode(Settings(_env_file=None, openai_api_key="synthetic-model"))
    node._engine = _RaisingEngine(
        LLMOutputError("Reasoning completion truncated (finish_reason=length)"), **kwargs
    )
    return node


def test_length_truncation_with_qualifying_evidence_yields_controlled_degraded():
    node = _truncated_node(finish_reason="length", parse_status="invalid_json")
    state = _reasoning_state([_fallback_row()])
    out = asyncio.run(node(state))

    assert out["delivery_status"] == "degraded"
    assert out["persisted"] is False
    assert out["report"] is not None
    assert out["degraded_reason_codes"] == ["evidence_only_fallback", "llm_output_error"]
    diagnostics = out["fallback_diagnostics"]
    assert diagnostics["enabled"] is True
    assert diagnostics["candidate_count"] == 1 and diagnostics["accepted_count"] == 1


def test_invalid_json_with_qualifying_evidence_yields_controlled_degraded():
    node = _truncated_node(finish_reason="stop", parse_status="invalid_json")
    out = asyncio.run(node(_reasoning_state([_fallback_row()])))
    assert out["delivery_status"] == "degraded"
    assert out["persisted"] is False
    assert out["report"] is not None


def test_length_truncation_without_qualifying_evidence_stays_blocked():
    """不完整证据 + 模型截断 ⇒ 继续 blocked，绝不输出未经审计的正文。"""
    node = _truncated_node(finish_reason="length", parse_status="invalid_json")
    out = asyncio.run(node(_reasoning_state([_fallback_row(completeness="page")])))

    assert out.get("report") is None
    assert out.get("delivery_status") is None
    assert out.get("fallback_diagnostics") is None
    assert out["errors"]


def test_date_and_symbol_mismatch_keep_the_run_blocked():
    node = _truncated_node(finish_reason="length", parse_status="invalid_json")
    for overrides in ({"as_of_date": "2026-10-08"}, {"instrument": "SZ000001"}):
        out = asyncio.run(node(_reasoning_state([_fallback_row(**overrides)])))
        assert out.get("report") is None and out.get("delivery_status") is None


def test_fallback_delivery_survives_finalize_without_persistence():
    evidence = _fallback_row()
    report = build_evidence_only_fallback(
        [evidence], requested_date="2026-10-09", target_instrument="600519"
    )
    state = _reasoning_state(
        [evidence],
        report=report,
        reason_codes=["evidence_only_fallback", "llm_output_error"],
        degraded_reason_codes=["evidence_only_fallback", "llm_output_error"],
        critique={"verdict": "research_more", "issues": []},
        errors=["reasoning output error"],
    )
    out = asyncio.run(FinalizeAuditNode(None)(state))
    assert out["delivery_status"] == "degraded"
    assert out["report"] is not None
    assert out["report"].claims[0].evidence_ids == ["fallback-001"]

    # degraded 绝不落库：共享的响应构造器把 persisted 钉死为 False。
    from app.models.response import build_response_from_state

    response = build_response_from_state({**state, **out}, question=state["question"])
    assert response.delivery_status == "degraded"
    assert response.persisted is False


def test_timeout_fallback_reports_a_timeout_reason_not_an_output_error():
    """交付理由必须可回溯到真实原因：超时截断的 run 不能写成 llm_output_error。"""
    evidence = _fallback_row()
    report = build_evidence_only_fallback(
        [evidence], requested_date="2026-10-09", target_instrument="600519"
    )
    state = _reasoning_state(
        [evidence],
        report=report,
        reason_codes=["evidence_only_fallback", "reasoning_timeout"],
        degraded_reason_codes=["evidence_only_fallback", "reasoning_timeout"],
        critique={"verdict": "research_more", "issues": []},
        errors=[],
    )
    out = asyncio.run(FinalizeAuditNode(None)(state))
    assert out["delivery_status"] == "degraded"
    assert out["report"] is not None
    assert out["reason_codes"] == ["evidence_only_fallback", "reasoning_timeout"]
    assert "llm_output_error" not in out["reason_codes"]


def test_compact_mode_offers_no_alias_for_ledger_rows_the_model_cannot_see():
    """compact aliases 只能引用展示给模型的 evidence（隐藏账本行不可被引用）。"""
    node = ReasoningNode(Settings(_env_file=None, openai_api_key="synthetic-model"))
    captured: dict = {}

    async def fake_reason(**kwargs):
        captured.update(kwargs)
        return _fallback_row()

    node._engine.reason = fake_reason  # type: ignore[method-assign]
    node._engine.last_finish_reason = "stop"
    node._engine.last_parse_status = "valid_json_object"

    async def fake_run_stage(awaitable, state, settings, *, stage, reserve=0.0):
        return await awaitable

    import app.graph.nodes.reasoning as reasoning_node

    original = reasoning_node.run_stage
    reasoning_node.run_stage = fake_run_stage  # type: ignore[assignment]
    try:
        asyncio.run(node(_reasoning_state([_fallback_row()])))
    finally:
        reasoning_node.run_stage = original  # type: ignore[assignment]

    # compact mode（显式日期 + 单一标的）下不发放别名。
    assert captured["evidence_aliases"] == {}
    assert captured["compact_mode"] is True


def test_success_path_still_records_fallback_admission_diagnostics(caplog):
    """正常成文也要留一份准入判定：applied=False。

    否则"这次没有降级"和"根本没判定过"在 JSONL 上无法区分（P13 §一.2）。
    """
    node = ReasoningNode(Settings(_env_file=None, openai_api_key="synthetic-model"))
    report = build_evidence_only_fallback(
        [_fallback_row()], requested_date="2026-10-09", target_instrument="600519"
    )
    assert report is not None

    async def fake_reason(**_kwargs):
        return report

    node._engine.reason = fake_reason  # type: ignore[method-assign]
    node._engine.last_finish_reason = "stop"
    node._engine.last_parse_status = "valid_json_object"

    async def fake_run_stage(awaitable, state, settings, *, stage, reserve=0.0):
        return await awaitable

    import app.graph.nodes.reasoning as reasoning_node

    original = reasoning_node.run_stage
    reasoning_node.run_stage = fake_run_stage  # type: ignore[assignment]
    try:
        with caplog.at_level(logging.INFO, logger="app.graph.run_log"):
            out = asyncio.run(node(_reasoning_state([_fallback_row()])))
    finally:
        reasoning_node.run_stage = original  # type: ignore[assignment]

    assert out["report"] is report
    assert "delivery_status" not in out
    diagnostics = out["fallback_diagnostics"]
    assert diagnostics["applied"] is False
    assert diagnostics["enabled"] is True
    assert diagnostics["candidate_count"] == 1
    assert diagnostics["accepted_count"] == 1

    events = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "app.graph.run_log"
    ]
    fallbacks = [event for event in events if event.get("event") == "evidence_fallback"]
    assert fallbacks, "success path must still emit an evidence_fallback audit event"
    assert fallbacks[-1]["phase"] == "not_required"
    assert fallbacks[-1]["applied"] is False
    assert fallbacks[-1]["accepted_count"] == 1


def test_success_after_fallback_clears_stale_markers_and_reaudits():
    """fallback 之后再成文：旧标记必须清掉，正文必须重新过 claim 级过滤。

    否则 finalize 会把这份**普通报告**当成 fallback 报告原样放行（跳过
    `build_degraded_report`），一份引用不存在 evidence 的正文就能以 degraded
    交付 —— 既误标交付理由，也绕过了审计门禁（P13 §一.5）。
    """
    evidence = _fallback_row()
    state = _reasoning_state([evidence])

    out1 = asyncio.run(
        _truncated_node(finish_reason="length", parse_status="invalid_json")(state)
    )
    assert out1["degraded_reason_codes"] == ["evidence_only_fallback", "llm_output_error"]

    forged = build_evidence_only_fallback(
        [evidence], requested_date="2026-10-09", target_instrument="600519"
    )
    assert forged is not None
    forged.title = "模型成文"
    forged.claims[0].evidence_ids = ["forged-999"]

    node = ReasoningNode(Settings(_env_file=None, openai_api_key="synthetic-model"))

    async def fake_reason(**_kwargs):
        return forged

    node._engine.reason = fake_reason  # type: ignore[method-assign]
    node._engine.last_finish_reason = "stop"
    node._engine.last_parse_status = "valid_json_object"

    async def fake_run_stage(awaitable, state, settings, *, stage, reserve=0.0):
        return await awaitable

    import app.graph.nodes.reasoning as reasoning_node

    resumed = {
        **state,
        **out1,
        # 回环后 state 里同时带着无关旧码与 fallback 旧标记。
        "reason_codes": ["missing_as_of_date", "evidence_only_fallback", "llm_output_error"],
    }
    original = reasoning_node.run_stage
    reasoning_node.run_stage = fake_run_stage  # type: ignore[assignment]
    try:
        out2 = asyncio.run(node(resumed))
    finally:
        reasoning_node.run_stage = original  # type: ignore[assignment]

    assert out2["report"] is forged
    assert out2["degraded_reason_codes"] == []
    assert out2["delivery_status"] is None
    assert "evidence_only_fallback" not in out2["reason_codes"]
    assert "llm_output_error" not in out2["reason_codes"]
    assert "missing_as_of_date" in out2["reason_codes"]  # 无关旧码必须保留

    merged = {**resumed, **out2, "critique": {"verdict": "research_more", "issues": []}}
    final = asyncio.run(FinalizeAuditNode(None)(merged))
    assert final["report"] is None
    assert final["delivery_status"] == "blocked"
    assert "invalid_reference" in final["reason_codes"]
    assert "evidence_only_fallback" not in final["reason_codes"]


# ── §二：sync / SSE 对称与唯一终态 ─────────────────────────────────────────

_AUDITED_FIELDS = ("delivery_status", "code", "reason_codes", "persisted", "status", "terminal")


def _degraded_state() -> dict:
    evidence = _fallback_row()
    report = build_evidence_only_fallback(
        [evidence], requested_date="2026-10-09", target_instrument="600519"
    )
    assert report is not None
    return {
        "question": "查询贵州茅台 SH600519 在 2026-10-09 的行情",
        "domain": "a_share",
        "run_id": "p13-parity",
        "report": report,
        "results": [],
        "critique": {"verdict": "research_more", "issues": []},
        "errors": [],
        "final_audit_status": "revise_exhausted",
        "delivery_status": "degraded",
        "requested_date": "2026-10-09",
        "as_of_date": "2026-10-09",
        "planned_as_of_date": "2026-10-09",
        "market_closed": False,
        "fallback_diagnostics": {
            "enabled": True,
            "reason": "ok",
            "candidate_count": 1,
            "accepted_count": 1,
            "rejected_count": 0,
            "rejection_reasons": {},
            "candidate_evidence_ids": {"accepted": ["fallback-001"], "rejected": []},
        },
    }


def test_sync_and_sse_terminal_payloads_agree_for_identical_state(monkeypatch):
    from app.models.response import build_response_from_state

    state = _degraded_state()

    class Graph:
        async def astream(self, initial_state, stream_mode=None, config=None):
            yield "values", state

    class Orchestrator:
        async def run(self, question, domain=None, conversation_id=None):
            return build_response_from_state(dict(state), question=question)

        def _ensure_graph(self):
            return Graph()

    monkeypatch.setattr(main_module, "_get_orchestrator", lambda: Orchestrator())

    async def fake_persist(result, question, conversation_id, **kwargs):
        result.persisted = False
        return False

    monkeypatch.setattr(main_module, "persist_research", fake_persist)

    with TestClient(app) as client:
        sync_payload = client.post("/api/ask", json={"question": "q"}).json()
        text = client.post("/api/ask/stream", json={"question": "q"}).text

    events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
    results = [payload for payload in events if payload.get("terminal") is True]
    assert len(results) == 1, "SSE 必须只有一个终态事件"
    stream_payload = results[0]

    for field in _AUDITED_FIELDS:
        assert sync_payload[field] == stream_payload[field], field
    assert sync_payload["delivery_status"] == "degraded"
    assert sync_payload["code"] == "degraded"
    assert sync_payload["persisted"] is False
    assert sync_payload["report"] is not None and stream_payload["report"] is not None
    sync_claims = [claim["claim"] for claim in sync_payload["report"]["claims"]]
    stream_claims = [claim["claim"] for claim in stream_payload["report"]["claims"]]
    assert sync_claims == stream_claims
    # §二.2：相同输入 / 相同 evidence 集合 / 相同 Reasoning 结果时，claims、evidence_ids
    # 与 report 也必须逐字一致。
    assert sync_payload["report"]["claims"] == stream_payload["report"]["claims"]
    assert sync_payload["report"]["evidence"] == stream_payload["report"]["evidence"]
    assert sync_payload["report"] == stream_payload["report"]
    assert sync_payload["candidate_count"] == stream_payload["candidate_count"] == 1
    assert sync_payload["accepted_count"] == stream_payload["accepted_count"] == 1


def test_sse_never_marks_progress_or_heartbeat_as_terminal(monkeypatch):
    class Graph:
        async def astream(self, initial_state, stream_mode=None, config=None):
            yield "values", {
                **initial_state,
                "report": None,
                "critique": {"verdict": "revise", "issues": []},
                "final_audit_status": "revise_exhausted",
                "delivery_status": "blocked",
                "reason_codes": ["rewrite_budget_exhausted"],
                "errors": [],
            }

    class Orchestrator:
        async def run(self, question, domain=None, conversation_id=None):  # pragma: no cover
            raise AssertionError("sync path not used")

        def _ensure_graph(self):
            return Graph()

    monkeypatch.setattr(main_module, "_get_orchestrator", lambda: Orchestrator())

    with TestClient(app) as client:
        text = client.post("/api/ask/stream", json={"question": "q"}).text

    events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
    assert events, "SSE 必须至少有一个事件"
    terminal = [payload for payload in events if payload.get("terminal") is True]
    assert len(terminal) == 1
    assert terminal[0]["code"] == "blocked"
    assert terminal[0]["status"] == "blocked"
    assert terminal[0]["persisted"] is False
    for payload in events:
        if payload.get("terminal") is not True:
            assert payload.get("step") != "done"
            assert "delivery_status" not in payload


def test_terminal_code_mapping_is_shared_and_total():
    assert terminal_code_for("verified") == "ok"
    assert terminal_code_for("degraded") == "degraded"
    assert terminal_code_for("blocked") == "blocked"
    assert terminal_code_for("failed") == "failed"
    assert terminal_code_for(None) == "failed"

    from app.models.response import ResearchResponse

    blocked = ResearchResponse(question="q", report=None, delivery_status="blocked")
    payload = build_terminal_payload(blocked, code="blocked", message="x", retryable=True)
    assert payload["status"] == "blocked" and payload["terminal"] is True
    assert payload["persisted"] is False and payload["report"] is None
    assert payload["code"] == "blocked" and payload["error"] == payload["detail"] == "x"


# ── §三：THS 行情 datum → evidence 字段传播 ───────────────────────────────


def _market_payload(columns, datas, *, code_count=1):
    return json.dumps({
        "status_code": 0,
        "code_count": code_count,
        "columns": columns,
        "datas": datas,
    }, ensure_ascii=False)


def _market_result(columns, datas, **kwargs):
    arguments = canonicalize_tool_arguments(MARKET_TOOL, {"query": "600519 2026-10-09 收盘价"})
    return normalize_query_data(_market_payload(columns, datas, **kwargs), MARKET_TOOL, arguments)


def test_ths_market_datum_completeness_describes_its_own_fields():
    result = _market_result(
        [{"key": "收盘价[20261009]", "index_name": "收盘价:前复权", "unit": "元", "timestamp": "20261009"}],
        [{"股票代码": "600519.SH", "股票简称": "贵州茅台", "收盘价[20261009]": "1800.00"}],
    )
    datum = result.normalized[0]
    # 日期来自上游列名，标的是上游代码，来源/检索时间都真实存在 ⇒ complete。
    assert datum.as_of_date == "2026-10-09"
    assert datum.instrument == "SH600519"
    assert datum.source == datum.authority == "同花顺问财"
    assert datum.retrieved_at
    assert datum.instrument_scope == "instrument"
    assert datum.completeness == "complete"
    # 信封层的分页/partial 语义没有被抹掉。
    assert result.completeness in {"page", "partial"}


def test_ths_market_row_without_its_own_date_is_not_complete():
    result = _market_result(
        [
            {"key": "最新价", "index_name": "最新价", "unit": "元"},
            {"key": "收盘价[20261009]", "index_name": "收盘价:前复权", "unit": "元", "timestamp": "20261009"},
        ],
        [{"股票代码": "600519.SH", "股票简称": "贵州茅台", "最新价": "1800.00", "收盘价[20261009]": "1800.00"}],
    )
    by_metric = {datum.metric: datum for datum in result.normalized}
    latest = next(d for metric, d in by_metric.items() if metric.endswith(".最新价"))
    dated = next(d for metric, d in by_metric.items() if metric.endswith(".收盘价[20261009]"))
    assert latest.as_of_date is None and latest.completeness == "partial"
    assert dated.as_of_date == "2026-10-09" and dated.completeness == "complete"


def test_real_market_evidence_reaches_controlled_degraded_without_whole_market_rows():
    result = _market_result(
        [
            {"key": "收盘价[20261009]", "index_name": "收盘价:前复权", "unit": "元", "timestamp": "20261009"},
            {"key": "成交量[20261009]", "index_name": "成交量", "unit": "手", "timestamp": "20261009"},
        ],
        [{
            "股票代码": "600519.SH", "股票简称": "贵州茅台",
            "收盘价[20261009]": "1800.00", "成交量[20261009]": "12345",
        }],
        code_count=27,
    )
    # code_count > 返回行数 ⇒ 信封 partial，但每条 datum 自身字段齐全。
    assert result.partial is True
    evidence = build_evidence([result])
    assert evidence and all(item.partial is False for item in evidence)
    assert all(item.result_partial is True for item in evidence)
    assert all(item.completeness == "complete" for item in evidence)

    plan = plan_evidence_only_fallback(
        evidence, requested_date="2026-10-09", target_instrument="600519"
    )
    assert plan.accepted_count == len(evidence) >= 1
    report = build_evidence_only_fallback(
        evidence, requested_date="2026-10-09", target_instrument="600519", decision=plan
    )
    assert report is not None
    # 只交付直接事实：每条 claim 都绑定一条真实 evidence，且不含全市场结论。
    assert report.claims and all(claim.claim_type == "fact" for claim in report.claims)
    assert all(len(claim.evidence_ids) == 1 for claim in report.claims)
    assert "全市场" not in report.what_happened
    assert report.why == [] and report.risks == [] and report.strong_areas == []


def test_whole_market_evidence_cannot_stand_in_for_one_instrument():
    """上游只给了全市场口径（没有标的代码）时，绝不能当作单只股票证据。"""
    result = _market_result(
        [{"key": "收盘价[20261009]", "index_name": "全市场收盘价", "unit": "元", "timestamp": "20261009"}],
        [{"收盘价[20261009]": "3200"}],
        code_count=1,
    )
    datum = result.normalized[0]
    assert datum.instrument is None
    # 有日期但没有标的 ⇒ 作用域是 unverified_instrument，而不是固化成某个标的。
    assert datum.instrument_scope == "unverified_instrument"
    evidence = build_evidence([result])
    assert evidence
    plan = plan_evidence_only_fallback(
        evidence, requested_date="2026-10-09", target_instrument="600519"
    )
    assert plan.accepted_count == 0
    assert plan.rejected_count == len(evidence)
    for item in plan.candidates:
        assert "symbol_mismatch" in item.reasons
    assert build_evidence_only_fallback(
        evidence, requested_date="2026-10-09", target_instrument="600519"
    ) is None


# ── §四：公告代码格式与年月过滤 ────────────────────────────────────────────


def _announcement_payload(codes, publish_date="2026-10-10 00:00:00"):
    return json.dumps({
        "status_code": 0,
        "data": [{
            "channel": "announcement",
            "title": "贵州茅台公告",
            "summary": "摘要",
            "url": "https://example.test/a.pdf",
            "publish_date": publish_date,
            "stock_infos": codes,
        }],
    }, ensure_ascii=False)


def test_announcement_accepts_plain_suffixed_and_prefixed_codes():
    """三种代码写法都必须被接受，并指向同一只股票（600519）。"""
    expected = {"600519": "600519", "600519.SH": "SH600519", "SH600519": "SH600519"}
    for code, instrument in expected.items():
        result = normalize_announcements(
            _announcement_payload([{"code": code, "name": "贵州茅台"}]),
            {"query": "600519 2026年10月公告", "size": 10},
        )
        assert result.normalized, code
        datum = result.normalized[0]
        assert datum.instrument == instrument
        assert datum.instrument[-6:] == "600519"
        assert datum.as_of_date == "2026-10-10"
        assert datum.completeness == "complete"


def test_announcement_wrong_month_or_symbol_is_rejected():
    wrong_month = normalize_announcements(
        _announcement_payload([{"code": "600519", "name": "贵州茅台"}], publish_date="2026-01-21 00:00:00"),
        {"query": "600519 2026年10月公告", "size": 10},
    )
    assert wrong_month.status == "error" and wrong_month.normalized == []
    assert "empty_normalized" in wrong_month.reason_codes

    wrong_symbol = normalize_announcements(
        _announcement_payload([{"code": "000001.SZ", "name": "平安银行"}]),
        {"query": "600519 2026年10月公告", "size": 10},
    )
    assert wrong_symbol.status == "error" and wrong_symbol.normalized == []


def test_announcement_without_returned_date_cannot_satisfy_a_month_request():
    payload = json.dumps({
        "status_code": 0,
        "data": [{
            "channel": "announcement",
            "title": "无日期公告",
            "stock_infos": [{"code": "600519", "name": "贵州茅台"}],
        }],
    }, ensure_ascii=False)
    result = normalize_announcements(payload, {"query": "600519 2026年10月公告", "size": 10})
    assert result.normalized == [] and result.status == "error"
    assert "empty_normalized" in result.reason_codes
    assert build_evidence([result]) == []


def test_matching_announcement_evidence_can_be_delivered_as_degraded():
    result = normalize_announcements(
        _announcement_payload([{"code": "600519", "name": "贵州茅台"}]),
        {"query": "600519 2026年10月公告", "size": 10},
    )
    assert result.status == "success"
    evidence = build_evidence([result])
    assert len(evidence) == 1
    claim_text = "贵州茅台公告《贵州茅台公告》"
    from app.graph.nodes.finalize import build_degraded_report
    from app.models.response import ClaimEvidence, MarketIntelligence

    report = MarketIntelligence(
        requested_date="2026-10-01",
        as_of_date="2026-10-10",
        claims=[ClaimEvidence(claim=claim_text, evidence_ids=[evidence[0].id], claim_type="fact")],
        what_happened=claim_text,
    )
    out, gaps, codes = build_degraded_report(
        {
            "domain": "a_share",
            "requested_date": "2026-10-01",
            "planned_as_of_date": "2026-10-10",
            "as_of_date": "2026-10-10",
            "market_closed": False,
            "evidence": evidence,
            "report": report,
        },
        report,
        {"verdict": "revise", "issues": []},
    )
    assert out is not None, (gaps, codes)
    assert [claim.claim for claim in out.claims] == [claim_text]


def test_announcement_evidence_cannot_support_a_price_move():
    from app.agent.evidence_gate import match_claims_to_evidence

    result = normalize_announcements(
        _announcement_payload([{"code": "600519", "name": "贵州茅台"}]),
        {"query": "600519 2026年10月公告", "size": 10},
    )
    evidence = build_evidence([result])
    rows = match_claims_to_evidence(
        {"claims": [{"claim": "600519 今日上涨 2%", "evidence_ids": [evidence[0].id]}]},
        evidence,
    )
    assert rows and not rows[0]["supported"]
    assert "news_mention_not_price" in rows[0]["reason_codes"]
