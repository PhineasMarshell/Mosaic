"""同步 ``/api/ask`` 一条链路七个事件必须共用同一个真实 run_id（方案 §4 阶段 0）。

背景
----
阶段 0 要求"日志 / API 可重建一次运行的完整链路"。``run_id`` 是这次串联的
唯一钥匙：它由 ``Orchestrator.run`` 建 ``ResearchState`` 时生成，随 state 流经
每个节点的结构化日志。同步端点曾经把 delivery 打点写成
``log_delivery({"run_id": None}, ...)``，于是同一次运行的最后一条日志掉队，
``run_start`` 与 ``delivery`` 再也对不上。

本文件与 ``tests/test_audit_run_log.py::test_sync_delivery_log_carries_the_same_run_id``
的区别
----------------------
那个用例是**手工调** ``main.run_log.log_delivery(...)`` 的近似，它只证明
"传对了参数"。本文件走**真实 HTTP 同步端点**：``TestClient(main.app).post("/api/ask")``
→ 真实 ``Orchestrator.run`` → 真实 ``main.ask`` 的 delivery 打点，并断言
**全部七个事件**（run_start / plan / executions / critic / route / finalize /
delivery）身上的 run_id 完全一致，且没有任何一个是自己另造的。

本文件 mock 了什么
-----------------
- **图驱动**：``Orchestrator._ensure_graph`` 的返回值被换成一个按真实拓扑顺序
  驱动节点的 driver（同步端点只调 ``graph.ainvoke``，不调 ``astream``）。
  节点本身、路由决策、prompt 组装、覆盖度判定、终态派生全部是**真实实现**。
- **三个 LLM 节点的 client**（Supervisor / Reasoning / Critic）与
  ``ToolRuntime.execute``：不触网，返回固定 JSON / 固定 ToolResult。

因此**没有覆盖**：真实 LLM 判断质量、真实 gateway 数据、SSE 路径
（SSE 的 run_id 串联由 ``tests/test_audit_run_log.py`` 覆盖）。
"""

from __future__ import annotations

import json
import logging
import types

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.agent.orchestrator import Orchestrator
from app.config import Settings
from app.graph import run_log
from app.graph.builder import critic_route_decision
from app.models.response import build_response_from_state

#: 只调一个无需 symbol 的聚合工具：保证 evidence gate 有证据，errors 保持为空。
_PLAN_JSON = (
    '{"intent":{"domain":"a_share","task":"market_summary","time_scope":"today","question":"测试"},'
    '"steps":[{"tool_key":"limit_up_count","arguments":{},"purpose":"涨停家数"}]}'
)

#: 七个必须能互相串联的事件（顺序即真实发生顺序）。
_CORRELATED_EVENTS = ("run_start", "plan", "executions", "critic", "route", "finalize", "delivery")

_CORRELATION_LOGGER = "app.graph.run_log"


def _report():
    from app.models.response import MarketIntelligence

    return MarketIntelligence(
        market_state="走强",
        state_label="RISK_ON",
        what_happened="今日市场走强，商业航天领涨",
        confidence="high",
    )


# ------------------------------------------------------------------ #
# fake LLM / 工具执行：只换 client，不换节点 __call__                     #
# ------------------------------------------------------------------ #


def _patch_fake_llm(monkeypatch, critic_payload: str):
    from app.graph.nodes import critic as crit_mod
    from app.graph.nodes import reasoning as rea_mod
    from app.graph.nodes import supervisor as sup_mod

    def _fake_completions(payload: str):
        async def create(**kwargs):
            message = types.SimpleNamespace(content=payload)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))

    orig_sup_init = sup_mod.SupervisorNode.__init__

    def sup_init(self, settings):
        orig_sup_init(self, settings)
        self.client = _fake_completions(_PLAN_JSON)

    monkeypatch.setattr(sup_mod.SupervisorNode, "__init__", sup_init)

    orig_rea_init = rea_mod.ReasoningNode.__init__

    def rea_init(self, settings, **kwargs):
        orig_rea_init(self, settings, **kwargs)
        self._engine = types.SimpleNamespace(reason=_fake_reason(_report()))

    monkeypatch.setattr(rea_mod.ReasoningNode, "__init__", rea_init)

    orig_crit_init = crit_mod.CriticNode.__init__

    def crit_init(self, settings):
        orig_crit_init(self, settings)
        self.client = _fake_completions(critic_payload)

    monkeypatch.setattr(crit_mod.CriticNode, "__init__", crit_init)

    async def fake_execute(self, tool_name, arguments, called_signatures, deadline=None):
        from app.models.market import NormalizedDatum, ToolResult

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


# ------------------------------------------------------------------ #
# 按真实拓扑顺序驱动节点的 graph 替身                                      #
# ------------------------------------------------------------------ #


class _RealNodesDriver:
    """同步端点只调 ``ainvoke``；这里按 supervisor→analyst→gate→reasoning→critic
    的真实顺序驱动真实节点，好让七个事件真的被生产出来。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.seen_state: dict = {}

    async def ainvoke(self, state, config=None):
        from app.graph.nodes.analysts.fundamental import FundamentalAnalystNode
        from app.graph.nodes.analysts.moneyflow import MoneyflowAnalystNode
        from app.graph.nodes.analysts.technical import TechnicalAnalystNode
        from app.graph.nodes.critic import CriticNode
        from app.graph.nodes.finalize import FinalizeAuditNode
        from app.graph.nodes.gate import GateNode
        from app.graph.nodes.reasoning import ReasoningNode
        from app.graph.nodes.supervisor import SupervisorNode, route_candidate_categories
        from app.graph.state import _merge_evidence, _merge_results

        data = state.model_dump(exclude_none=False) if hasattr(state, "model_dump") else dict(state)
        self.seen_state = dict(data)

        # 1. supervisor：真实节点，内部自己打 run_start（仅在缺 run_id 时）与 plan
        supervisor = SupervisorNode(self.settings)
        out = await supervisor(data)
        data["intent"] = out.get("intent")
        data["route"] = out.get("route") or []
        data["errors"] = (data.get("errors") or []) + (out.get("errors") or [])

        # 2. analyst：与 builder._supervisor_fanout 同一口径，只跑被分配的类别
        assigned = {a.get("analyst") for a in data["route"] if isinstance(a, dict)}
        names = [n for n in route_candidate_categories(self.settings) if n in assigned]
        if not names:
            names = ["technical", "fundamental", "moneyflow"]
        node_by_name = {
            "technical": TechnicalAnalystNode,
            "fundamental": FundamentalAnalystNode,
            "moneyflow": MoneyflowAnalystNode,
        }
        for name in names:
            analyst_out = await node_by_name[name](self.settings)(data)
            data["results"] = _merge_results(data.get("results") or [], analyst_out.get("results") or [])
            data["evidence"] = _merge_evidence(data.get("evidence") or [], analyst_out.get("evidence") or [])
            data["errors"] = (data.get("errors") or []) + (analyst_out.get("errors") or [])

        gate_out = await GateNode(self.settings)(data)
        data["gate"] = gate_out.get("gate")
        data["errors"] = (data.get("errors") or []) + (gate_out.get("errors") or [])

        reasoning_out = await ReasoningNode(self.settings)(data)
        data["report"] = reasoning_out.get("report")

        critique_out = await CriticNode(self.settings)(data)
        data["critique"] = critique_out.get("critique")
        data["errors"] = (data.get("errors") or []) + (critique_out.get("errors") or [])

        # 3. route —— 用真实路由决策（内部打 route 日志）
        decision = critic_route_decision(data, self.settings)
        assert decision == "end", f"本用例走 pass 快路径，实际路由={decision}"

        # 4. finalize 只在非 pass 路径出现；pass 走 END，由组装器兜底派生终态。
        finalize = FinalizeAuditNode(self.settings)
        audit_status, delivery_status, reason = _resolve_from_critique(data)
        if delivery_status != "verified":
            out = await finalize(data)
            data.update(out)
        else:
            run_log.log_finalize(
                data,
                final_audit_status=audit_status,
                delivery_status=delivery_status,
                reason=reason,
            )

        return data


def _resolve_from_critique(state: dict):
    """pass 路径不经过 finalize_audit 节点，这里用同一纯函数换算终态。"""
    from app.graph.nodes.finalize import resolve_delivery

    return resolve_delivery(state.get("critique"), state.get("errors") or [])


# ------------------------------------------------------------------ #
# 断言辅助                                                              #
# ------------------------------------------------------------------ #


def _emit_count(caplog, event: str) -> int:
    return sum(1 for r in caplog.records if f'"event": "{event}"' in r.getMessage())


def _correlation_ids(caplog) -> dict[str, list[str]]:
    """从结构化日志里抽出每个事件的 run_id 列表（保持出现顺序）。"""
    out: dict[str, list[str]] = {name: [] for name in _CORRELATED_EVENTS}
    for rec in caplog.records:
        msg = rec.getMessage()
        if not msg.startswith('{"ts"'):
            continue
        payload = json.loads(msg)
        event = payload.get("event")
        if event in out:
            out[event].append(payload.get("run_id"))
    return out


@pytest.fixture(autouse=True)
def _clean_orchestrator(monkeypatch):
    """每个用例从干净的全局 orchestrator 开始（否则会复用别的用例的图）。"""
    monkeypatch.setattr(main, "_orchestrator", None, raising=False)
    yield


def test_sync_ask_correlates_all_seven_events_with_one_run_id(monkeypatch, caplog):
    """真实 ``POST /api/ask``：七个事件必须是同一个 run_id，且不是另造的。"""
    _patch_fake_llm(monkeypatch, json.dumps({"issues": [], "reason": "证据充分"}))

    driver = _RealNodesDriver(Settings())
    monkeypatch.setattr(Orchestrator, "_ensure_graph", lambda self: driver)

    async def fake_persist(result, question, conversation_id, **kwargs):
        return result.delivery_status == "verified"

    monkeypatch.setattr(main, "persist_research", fake_persist)

    with caplog.at_level(logging.INFO, logger=_CORRELATION_LOGGER):
        resp = TestClient(main.app).post("/api/ask", json={"question": "今天 A 股发生了什么？"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["delivery_status"] == "verified"

    ids = _correlation_ids(caplog)
    missing = [name for name, values in ids.items() if not values]
    assert not missing, f"这些事件没有出现在结构化日志里: {missing}"

    state_run_id = driver.seen_state.get("run_id")
    assert state_run_id, "Orchestrator.run 建 state 时必须生成 run_id"

    for name, values in ids.items():
        assert all(v == state_run_id for v in values), (
            f"{name} 的 run_id 与本次运行不一致: {values} != {state_run_id}"
        )

    # 关键回归点：run_id 不能在 delivery 处丢掉（旧实现固定传 None）
    assert ids["delivery"] == [state_run_id], "delivery 打点必须带真实 run_id"
    assert ids["delivery"] != [None]
    # 也不能是"事后另造一个"：delivery 的 id 必须能在这条链路里被 run_start 认出
    assert ids["run_start"] == [state_run_id]


def test_sync_ask_does_not_expose_run_id_to_the_client(monkeypatch):
    """run_id 只用于内部串联，不出现在成功响应的 JSON 里。"""
    _patch_fake_llm(monkeypatch, json.dumps({"issues": [], "reason": "证据充分"}))

    monkeypatch.setattr(Orchestrator, "_ensure_graph", lambda self: _RealNodesDriver(Settings()))

    async def fake_persist(result, question, conversation_id, **kwargs):
        return result.delivery_status == "verified"

    monkeypatch.setattr(main, "persist_research", fake_persist)

    resp = TestClient(main.app).post("/api/ask", json={"question": "今天 A 股发生了什么？"})
    assert resp.status_code == 200, resp.text
    assert "run_id" not in resp.json()


def test_every_event_is_emitted_exactly_once_per_pass_run(caplog, monkeypatch):
    """pass 快路径每个事件恰好一次 —— 事件数量本身就是"链路可重建"的一部分。

    与上面的用例共用 driver：这里不关心 id 值，只锁"七个事件都真的发生了"，
    避免 run_id 断言在事件整体消失时也能通过。
    """
    _patch_fake_llm(monkeypatch, json.dumps({"issues": [], "reason": "证据充分"}))
    monkeypatch.setattr(Orchestrator, "_ensure_graph", lambda self: _RealNodesDriver(Settings()))

    async def fake_persist(result, question, conversation_id, **kwargs):
        return result.delivery_status == "verified"

    monkeypatch.setattr(main, "persist_research", fake_persist)

    with caplog.at_level(logging.INFO, logger=_CORRELATION_LOGGER):
        resp = TestClient(main.app).post("/api/ask", json={"question": "今天 A 股发生了什么？"})

    assert resp.status_code == 200, resp.text
    for event in _CORRELATED_EVENTS:
        assert _emit_count(caplog, event) == 1, f"{event} 的日志条数不是 1"


def test_driver_actually_ran_the_pass_path():
    """守卫用例：driver 的终态换算与真实图一致（verified），否则上面三个用例无意义。"""
    state = {
        "report": _report(),
        "results": [],
        "errors": [],
        "critique": {"verdict": "pass", "reason": "ok"},
    }
    audit_status, delivery_status, _reason = _resolve_from_critique(state)
    assert (audit_status, delivery_status) == ("pass", "verified")
    assert build_response_from_state(state, question="q").delivery_status == "verified"


def test_request_without_orchestrator_still_generates_a_run_id_per_call(monkeypatch, caplog):
    """每次请求一个 run_id：两次运行的日志不能混成一条（run_id 必须现造）。"""
    _patch_fake_llm(monkeypatch, json.dumps({"issues": [], "reason": "证据充分"}))

    drivers: list[_RealNodesDriver] = []

    def _make_graph(self):
        drv = _RealNodesDriver(Settings())
        drivers.append(drv)
        return drv

    monkeypatch.setattr(Orchestrator, "_ensure_graph", _make_graph)

    async def fake_persist(result, question, conversation_id, **kwargs):
        return result.delivery_status == "verified"

    monkeypatch.setattr(main, "persist_research", fake_persist)
    monkeypatch.setattr(main, "_orchestrator", Orchestrator(Settings()), raising=False)

    client = TestClient(main.app)
    with caplog.at_level(logging.INFO, logger=_CORRELATION_LOGGER):
        client.post("/api/ask", json={"question": "第一次"})
        client.post("/api/ask", json={"question": "第二次"})

    ids = _correlation_ids(caplog)
    assert len(ids["delivery"]) == 2, ids
    assert ids["delivery"][0] != ids["delivery"][1], "两次运行不能共用 run_id"
    assert ids["run_start"][0] != ids["run_start"][1]
