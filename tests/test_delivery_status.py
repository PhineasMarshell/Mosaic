"""阶段 5（保守版）：审计非 pass 的结果不得作为正常完成返回或写入研究/每日状态。

锁住的验收点（方案 §4 阶段 5）：

1. 两轮 revise 后仍未通过 → ``delivery_status=blocked``，不持久化，SSE 不发"调查完成"；
2. ``research_more`` 因总预算耗尽 → 同样不可伪装成功；
3. Critic ``pass`` → 同步 API / SSE / 存储行为保持原样（兼容性红线）；
4. 调用方只能靠 ``delivery_status`` 判断可信，**不能再推断 ``errors == []``**。

本文件 mock 了什么
------------------
- **LLM**：Supervisor / Reasoning / Critic 三个节点用 fake 返回固定 JSON；
  整体替换 ``__call__`` 的位置均带 ``# T35-OK``（这些节点不是本文件的验证目标，
  本文件验证的是**图拓扑 + 终态派生 + 交付门控**）。
- **工具执行**：``ToolRuntime.execute`` 换成记录调用的 fake，不触网。

因此**没有覆盖**
----------------
- 真实 LLM 的审计质量、真实 gateway 数据、阶段 3 / 阶段 4 的能力。
"""

from __future__ import annotations

import json
import logging
import types

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.agent.persistence import persist_research
from app.config import Settings
from app.graph.builder import build_graph
from app.graph.nodes.critic import Critique
from app.graph.nodes.finalize import resolve_delivery
from app.memory.storage import MarketMemory
from app.models.market import ToolResult
from app.models.response import build_response_from_state

#: 一条最小计划：只调一个无需 symbol 的聚合工具，保证 evidence gate 有证据
#: （否则 gate 的零证据降级会往 errors 里写一条，掩盖本文件要验的终态语义）。
_PLAN_JSON = (
    '{"intent":{"domain":"a_share","task":"market_summary","time_scope":"today","question":"测试"},'
    '"steps":[{"tool_key":"limit_up_count","arguments":{},"purpose":"涨停家数"}]}'
)


# ------------------------------------------------------------------ #
# 图级：真实 build_graph + ainvoke，只有 LLM 与工具执行是 fake          #
# ------------------------------------------------------------------ #


def _patch_fake_llm(monkeypatch, critic_script: list[str], reasoning_report=None):
    """把三个 LLM 节点换成固定输出的 fake（不换节点 __call__，只换 client）。"""
    from app.graph.nodes import critic as crit_mod
    from app.graph.nodes import reasoning as rea_mod
    from app.graph.nodes import supervisor as sup_mod

    def _fake_completions(payloads: list[str]):
        calls = {"n": 0}

        async def create(**kwargs):
            payload = payloads[min(calls["n"], len(payloads) - 1)]
            calls["n"] += 1
            message = types.SimpleNamespace(content=payload)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))

    orig_sup_init = sup_mod.SupervisorNode.__init__

    def sup_init(self, settings):
        orig_sup_init(self, settings)
        self.client = _fake_completions([_PLAN_JSON])

    monkeypatch.setattr(sup_mod.SupervisorNode, "__init__", sup_init)

    orig_rea_init = rea_mod.ReasoningNode.__init__
    report = reasoning_report or _report()

    def rea_init(self, settings, **kwargs):
        orig_rea_init(self, settings, **kwargs)
        self._engine = types.SimpleNamespace(reason=_fake_reason(report))

    monkeypatch.setattr(rea_mod.ReasoningNode, "__init__", rea_init)

    orig_crit_init = crit_mod.CriticNode.__init__

    def crit_init(self, settings):
        orig_crit_init(self, settings)
        self.client = _fake_completions(critic_script)

    monkeypatch.setattr(crit_mod.CriticNode, "__init__", crit_init)

    async def fake_execute(self, tool_name, arguments, called_signatures, deadline=None):
        from app.models.market import NormalizedDatum

        return ToolResult(
            tool=tool_name,
            arguments=arguments,
            status="success",
            normalized=[NormalizedDatum(metric="limit_up.count", value=42, tool=tool_name)],
        )

    monkeypatch.setattr("app.graph.tool_runtime.ToolRuntime.execute", fake_execute)
    monkeypatch.setattr("app.graph.tool_runtime.ToolRuntime.truncate", lambda self, r: None)


def _fake_reason(report):
    async def reason(**kwargs):
        return report

    return reason


def _report():
    from app.models.response import MarketIntelligence

    return MarketIntelligence(
        market_state="走强",
        state_label="RISK_ON",
        what_happened="今日市场走强，商业航天领涨",
        confidence="high",
    )


def _critique_payload(verdict: str) -> str:
    return json.dumps(
        {
            "issues": [
                {
                    "kind": "unsupported_claim",
                    "claim": "商业航天领涨",
                    "severity": "high",
                    "action": "remove_or_qualify",
                    "rationale": "缺个股明细",
                }
            ],
            "reason": "措辞缺证据",
            "verdict": verdict,
        }
    )


@pytest.mark.asyncio
async def test_exhausted_revise_runs_end_as_blocked(monkeypatch):
    """revise 轮次（`max_rewrites`，默认 1）耗尽仍未通过 → 终态 blocked，errors 为空。"""
    _patch_fake_llm(monkeypatch, critic_script=[_critique_payload("revise")])

    graph = build_graph(Settings())
    state = await graph.ainvoke({"question": "今天 A 股发生了什么？", "domain": "a_share"})

    result = build_response_from_state(state, question="今天 A 股发生了什么？")
    assert result.final_audit_status == "revise_exhausted"
    assert result.delivery_status == "blocked"
    assert result.report is None, "未经审计的正文不得作为结论返回"
    assert result.errors == [], "审计没通过不是运行失败——这正是不能靠 errors 判断可信的原因"
    assert state["revision_count"] == Settings().effective_max_rewrites


@pytest.mark.asyncio
async def test_exhausted_research_more_runs_end_as_blocked(monkeypatch):
    """research_more 因轮次耗尽 → 同样 blocked，不伪装成功。"""
    _patch_fake_llm(
        monkeypatch,
        critic_script=[
            json.dumps(
                {
                    "issues": [
                        {
                            "kind": "missing_evidence",
                            "claim": "缺涨停个股明细",
                            "severity": "high",
                            "action": "research_more",
                            "required_tool_keys": ["limit_up_pool"],
                        }
                    ],
                    "reason": "证据不足",
                }
            )
        ],
    )

    graph = build_graph(Settings())
    state = await graph.ainvoke({"question": "今天 A 股发生了什么？", "domain": "a_share"})

    result = build_response_from_state(state, question="今天 A 股发生了什么？")
    assert result.final_audit_status == "research_exhausted"
    assert result.delivery_status == "blocked"
    assert result.report is None


@pytest.mark.asyncio
async def test_pass_run_keeps_existing_behavior(monkeypatch):
    """兼容性红线：verdict=pass 的路径与改造前完全一致。"""
    _patch_fake_llm(monkeypatch, critic_script=[json.dumps({"issues": [], "reason": "证据充分"})])

    graph = build_graph(Settings())
    state = await graph.ainvoke({"question": "今天 A 股发生了什么？", "domain": "a_share"})

    result = build_response_from_state(state, question="今天 A 股发生了什么？")
    assert result.final_audit_status == "pass"
    assert result.delivery_status == "verified"
    assert result.report is not None
    assert result.report.what_happened == "今日市场走强，商业航天领涨"
    assert result.errors == []
    # 快乐路径不经过 finalize_audit 节点：state 里没有终态字段，由组装器按
    # verdict=pass 兜底派生 —— 不额外花一次节点调用。
    assert state.get("final_audit_status") is None


# ------------------------------------------------------------------ #
# 纯函数：终态派生                                                      #
# ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "verdict,expected_audit,expected_delivery",
    [
        ("pass", "pass", "verified"),
        ("revise", "revise_exhausted", "blocked"),
        ("research_more", "research_exhausted", "blocked"),
        ("error", "error", "failed"),
    ],
)
def test_resolve_delivery_matrix(verdict, expected_audit, expected_delivery):
    audit, delivery, _reason = resolve_delivery(Critique(verdict=verdict, reason="r"), [])
    assert audit == expected_audit
    assert delivery == expected_delivery


def test_missing_critique_is_not_pass():
    """没有 critique（理论上不该发生）→ 判 failed，绝不当 pass。"""
    audit, delivery, _ = resolve_delivery(None, [])
    assert audit == "error"
    assert delivery == "failed"


def test_errors_empty_does_not_imply_verified():
    """本方案的核心反例：errors=[] + verdict=revise → 交付状态仍是 blocked。"""
    audit, delivery, _ = resolve_delivery(Critique(verdict="revise", reason="证据不足"), errors=[])
    assert delivery == "blocked"
    assert delivery != "verified"


# ------------------------------------------------------------------ #
# 持久化门控                                                            #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_blocked_result_is_not_written_to_memory(tmp_path):
    mem = MarketMemory(db_path=tmp_path / "m.db")
    state = {
        "question": "今天 A 股发生了什么？",
        "report": _report(),
        "critique": Critique(verdict="revise", reason="证据不足"),
        "errors": [],
        "final_audit_status": "revise_exhausted",
        "delivery_status": "blocked",
    }
    result = build_response_from_state(state, question=state["question"])

    assert result.delivery_status == "blocked"
    persisted = await persist_research(result, state["question"], "conv-x", memory=mem)

    assert persisted is False
    assert mem.get_daily_state() is None
    assert "今天 A 股发生了什么？" not in mem.get_conversation_history("conv-x", last_n=10)


@pytest.mark.asyncio
async def test_verified_result_still_written_to_memory(tmp_path):
    """对照组：verified 仍然照常写入（不得因门控而误伤）。"""
    mem = MarketMemory(db_path=tmp_path / "m.db")
    state = {
        "question": "今天 A 股发生了什么？",
        "report": _report(),
        "critique": Critique(verdict="pass", reason="ok"),
        "errors": [],
    }
    result = build_response_from_state(state, question=state["question"])

    persisted = await persist_research(result, state["question"], "conv-y", memory=mem)

    assert result.delivery_status == "verified"
    assert persisted is True
    assert (mem.get_daily_state() or {}).get("state_label") == "RISK_ON"
    assert "今天 A 股发生了什么？" in mem.get_conversation_history("conv-y", last_n=10)


# ------------------------------------------------------------------ #
# SSE / 同步 API：blocked 不发"调查完成"                                #
# ------------------------------------------------------------------ #


class _FakeGraph:
    def __init__(self, final_state):
        self.final_state = final_state

    async def astream(self, input_state, stream_mode=None, config=None):
        yield "values", self.final_state


def _sse_events(monkeypatch, final_state) -> list[tuple[str, dict]]:
    monkeypatch.setattr(
        main, "_orchestrator", types.SimpleNamespace(_ensure_graph=lambda: _FakeGraph(final_state)), raising=False
    )
    monkeypatch.setattr(
        main,
        "_settings",
        types.SimpleNamespace(research_budget_seconds=30, stream_heartbeat_seconds=5, graph_recursion_limit=25),
        raising=False,
    )
    monkeypatch.setattr(main, "persist_research", _no_persist(monkeypatch), raising=False)

    events: list[tuple[str, dict]] = []
    with TestClient(main.app).stream("POST", "/api/ask/stream", json={"question": "今天 A 股发生了什么？"}) as resp:
        assert resp.status_code == 200
        name = None
        for line in resp.iter_lines():
            if line.startswith("event: "):
                name = line[7:].strip()
            elif line.startswith("data: ") and name:
                events.append((name, json.loads(line[6:])))
                name = None
    return events


def _no_persist(monkeypatch):
    calls: list[bool] = []

    async def fake(result, question, conversation_id, **kwargs):
        calls.append(result.delivery_status == "verified")
        return result.delivery_status == "verified"

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


def test_stream_blocked_never_says_investigation_complete(monkeypatch, caplog):
    """blocked：SSE 不得出现 step=done / "调查完成"，也不得发带 report 的 result。"""
    monkeypatch.setattr(main, "_orchestrator", None, raising=False)
    monkeypatch.setattr(main, "_settings", None, raising=False)
    final_state = {
        "question": "今天 A 股发生了什么？",
        "domain": "a_share",
        "report": _report(),  # 图里本来是有正文的
        "results": [],
        "critique": {"verdict": "revise", "reason": "证据不足"},
        "errors": [],
        "final_audit_status": "revise_exhausted",
        "delivery_status": "blocked",
    }
    with caplog.at_level(logging.WARNING, logger="app.main"):
        events = _sse_events(monkeypatch, final_state)

    steps = [d.get("step") for n, d in events if n == "progress"]
    assert "done" not in steps, "blocked 不允许出现『调查完成』"
    assert "blocked" in steps

    final = [d for n, d in events if n == "result"][-1]
    assert final["delivery_status"] == "blocked"
    assert final["code"] == "blocked"
    assert not final.get("report"), "blocked 不得把未经审计的正文发出去"
    assert final["critique"]["verdict"] == "revise"


def test_stream_verified_still_sends_done_and_result(monkeypatch):
    """对照组：verified 保持原有 SSE 行为。"""
    monkeypatch.setattr(main, "_orchestrator", None, raising=False)
    monkeypatch.setattr(main, "_settings", None, raising=False)
    final_state = {
        "question": "今天 A 股发生了什么？",
        "domain": "a_share",
        "report": _report(),
        "results": [],
        "critique": {"verdict": "pass", "reason": "ok"},
        "errors": [],
    }
    events = _sse_events(monkeypatch, final_state)

    steps = [d.get("step") for n, d in events if n == "progress"]
    assert "done" in steps
    final = [d for n, d in events if n == "result"][-1]
    assert final["delivery_status"] == "verified"
    assert final["report"] is not None


def test_sync_endpoint_returns_structured_blocked_payload(monkeypatch):
    """同步 API：blocked 是结构化 200（不是基础设施失败），但没有正文报告。"""
    monkeypatch.setattr(main, "_orchestrator", None, raising=False)
    monkeypatch.setattr(main, "_settings", None, raising=False)

    from app.models.response import ResearchResponse

    blocked = ResearchResponse(
        question="今天 A 股发生了什么？",
        report=None,
        critique={"verdict": "revise", "reason": "证据不足"},
        final_audit_status="revise_exhausted",
        delivery_status="blocked",
        delivery_reason="审计未通过（verdict=revise），不作为可信结论交付",
    )

    async def fake_run(self, question, domain=None, conversation_id=None):
        return blocked

    monkeypatch.setattr("app.agent.orchestrator.Orchestrator.run", fake_run)
    persisted = []

    async def fake_persist(result, question, conversation_id, **kwargs):
        persisted.append(result.delivery_status)
        return result.delivery_status == "verified"

    monkeypatch.setattr(main, "persist_research", fake_persist)

    resp = TestClient(main.app).post("/api/ask", json={"question": "今天 A 股发生了什么？"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["delivery_status"] == "blocked"
    assert body["final_audit_status"] == "revise_exhausted"
    assert body["report"] is None
    assert body["retryable"] is True
    assert persisted == [], "blocked 结果绝不能进入持久化路径"
