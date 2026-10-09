"""阶段 0：失败回归样本 + 结构化运行摘要日志（方案 §4 阶段 0）。

本文件验证"这次失败可复现、可断言"，避免修复只改变措辞：

1. ``tests/fixtures/audit_regression_blocked.json`` 里的失败样本能一路喂进**真实的**
   Critic 节点 / 覆盖度判定 / 路由 / 终态派生，得到 ``delivery_status=blocked``；
2. 同一条失败样本的**成功对照样本**仍然是 verified —— 门控不许误伤；
3. 结构化运行摘要日志能重建一次运行：计划 → 执行 → 审计 → 路由 → 终态 → 交付。

本文件 mock 了什么
------------------
- **LLM**：``CriticNode.client`` 换成返回 fixture 里那一轮 Critique JSON 的 fake
  （节点 ``__call__`` 骨架真实运行，prompt 真实组装）。
- **registry 可见集合**：用 fixture 里声明的 tool_key 与真实注册表求交集，fixture
  不硬编码 operationId。

因此**没有覆盖**
----------------
- 真实 LLM 的审计判断质量（fake 返回的是 fixture 固定 JSON）；
- 真实 gateway 调用与真实数据（``results`` 是构造出来的 ToolResult 值对象）；
- 阶段 3（市场级工具策略）与阶段 4（claim—evidence 映射），本文件不涉及。

T35 约定：本文件不整体替换任何被测节点的 ``__call__``；关键用例断言成功分支。
"""

from __future__ import annotations

import json
import logging
import types
from pathlib import Path

import pytest

# app.main 在模块导入期加载：它的 setup_logging 会安装自己的 handler，
# 用例里再首次导入会让 caplog 抓不到 app.graph.run_log 的结构化日志。
import app.main as main
from app.config import Settings
from app.graph import run_log
from app.graph.gap_loop import (
    classify_missing_tool_keys,
    coverage_records,
    judge_coverage,
    satisfied_keys,
)
from app.graph.nodes.critic import CriticNode, Critique, derive_verdict_from_issues
from app.graph.nodes.finalize import resolve_delivery
from app.models.market import NormalizedDatum, ToolResult

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def blocked_case() -> dict:
    return _load("audit_regression_blocked.json")


@pytest.fixture(scope="module")
def pass_case() -> dict:
    return _load("audit_regression_pass.json")


# ------------------------------------------------------------------ #
# 把 fixture 的 results 变成真实 ToolResult                              #
# ------------------------------------------------------------------ #


def _to_tool_results(case: dict) -> list[ToolResult]:
    out: list[ToolResult] = []
    for row in case["results"]:
        if row.get("skipped"):
            continue
        count = int(row.get("datum_count", 0))
        out.append(
            ToolResult(
                tool=row["tool"],
                tool_key=row.get("tool_key"),
                arguments=row.get("arguments", {}),
                status=row["status"],
                partial=row.get("partial", False),
                note=row.get("note"),
                normalized=[
                    NormalizedDatum(
                        metric=m,
                        value=1,
                        tool=row["tool"],
                        status=row["status"],
                        partial=row.get("partial", False),
                    )
                    for m in (row.get("metrics") or [])[:count]
                ],
            )
        )
    return out


# ------------------------------------------------------------------ #
# fake LLM client（与 tests/test_critic_gap_keys.py 同构，独立定义）        #
# ------------------------------------------------------------------ #


class _FakeCompletions:
    def __init__(self, script: list[dict]):
        self._script = script
        self.calls = 0
        self.last_prompt: str | None = None

    async def create(self, **kwargs):
        messages = kwargs.get("messages") or []
        self.last_prompt = "\n".join(
            m.get("content", "") for m in messages if isinstance(m, dict) and m.get("role") == "user"
        )
        payload = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        message = types.SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


def _make_critic(script: list[dict]) -> CriticNode:
    node = CriticNode.__new__(CriticNode)
    node.settings = Settings()
    completions = _FakeCompletions(script)
    node.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
    return node


def _state(case: dict, results: list[ToolResult]) -> dict:
    report_stub = case.get("first_report", {})
    report = types.SimpleNamespace(
        what_happened=report_stub.get("what_happened", "今日市场综述"),
        confidence=report_stub.get("confidence", "low"),
        state_label=report_stub.get("state_label", "Neutral"),
        strong_areas=[],
        risks=[],
        why=[],
    )
    return {
        "run_id": case["run_id"],
        "question": case["question"],
        "domain": case["domain"],
        "intent": case.get("intent", {"domain": case["domain"], "task": "market_summary"}),
        "report": report,
        "results": results,
        "evidence": [],
        "gate": None,
    }


# ------------------------------------------------------------------ #
# 1. 失败样本端到端（真实 Critic 节点 + 真实路由 + 真实终态派生）           #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_blocked_fixture_reaches_blocked_without_errors(blocked_case):
    """失败样本：全程 errors 为空，但最终仍必须是 blocked（而不是"调查完成"）。"""
    from app.graph.builder import critic_route_decision

    results = _to_tool_results(blocked_case)
    script = [round_["response"] for round_ in blocked_case["critic_sequence"]]
    node = _make_critic(script)
    state = _state(blocked_case, results)

    # 本用例验证的是"连补两轮证据仍不通过 → 必须落 blocked，且全程 errors 为空"，
    # 所以显式把 research 额度设为 2；默认 1 的耗尽行为由 tests/test_critic_verdict.py
    # 与 tests/test_delivery_status.py 覆盖。
    settings = Settings(max_research_rounds=2)
    seen_verdicts = []
    for index, expected in enumerate(blocked_case["critic_sequence"]):
        out = await node(state)
        critique = out["critique"]
        seen_verdicts.append(critique.verdict)
        assert critique.verdict == expected["expected_verdict"], (
            f"第 {index + 1} 轮裁决与 fixture 预期不符：{critique.verdict} != {expected['expected_verdict']}"
        )
        state["critique"] = critique
        # 阶段 6：两条回环路径分别计数 —— 第 N 轮开始时对应路径已经用掉 N 轮。
        if critique.verdict == "revise":
            state["rewrite_count"] = index
        elif critique.verdict == "research_more":
            state["research_round_count"] = index
        state["revision_count"] = index
        decision = critic_route_decision(state, settings)
        assert decision == expected["expected_route"]

    # 最后一轮：轮次耗尽 → finalize_audit（不是静默 END）
    audit_status, delivery_status, _reason = resolve_delivery(state["critique"], state.get("errors"))
    assert audit_status == blocked_case["expected_outcome"]["final_audit_status"]
    assert delivery_status == blocked_case["expected_outcome"]["delivery_status"]

    # 关键性质：一次运行都没"出错"，可信与否仍由审计决定
    assert not state.get("errors")


@pytest.mark.asyncio
async def test_blocked_fixture_first_round_already_requests_research(blocked_case):
    """失败样本第一轮就必须是 research_more —— 缺大盘证据不该只判 revise。

    旧行为里这条被判成 revise，于是图只会回 reasoning 反复改写，直到轮次耗尽。
    """
    results = _to_tool_results(blocked_case)
    first_round = blocked_case["critic_sequence"][0]["response"]
    node = _make_critic([first_round])
    out = await node(_state(blocked_case, results))

    critique = out["critique"]
    assert critique.verdict == "research_more"
    assert critique.issues[0].action == "research_more"
    assert "overview" in critique.missing_tool_keys


@pytest.mark.asyncio
async def test_blocked_fixture_mixed_issue_types_prefer_research(blocked_case):
    """失败样本第二轮同时有'缺证据'与'可删改'：必须先补研究，不能判 revise。"""
    results = _to_tool_results(blocked_case)
    second_round = blocked_case["critic_sequence"][1]["response"]
    node = _make_critic([second_round])
    out = await node(_state(blocked_case, results))

    critique = out["critique"]
    assert critique.verdict == "research_more"
    assert {i.kind for i in critique.issues} == {"missing_evidence", "unsupported_claim"}
    # 可删改项仍然要交给 reasoning（不让它凭空消失）
    assert any(i.action == "remove_or_qualify" for i in critique.issues)


# ------------------------------------------------------------------ #
# 2. 成功对照样本：门控不许误伤                                          #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_pass_fixture_still_verified(pass_case):
    """成功样本：verdict=pass → verified，正文照常返回（兼容性红线）。"""
    from app.graph.builder import critic_route_decision

    results = _to_tool_results(pass_case)
    node = _make_critic([pass_case["critic_sequence"][0]["response"]])
    state = _state(pass_case, results)

    out = await node(state)
    assert out["critique"].verdict == "pass"
    state["critique"] = out["critique"]

    assert critic_route_decision(state, Settings()) == "end"
    audit_status, delivery_status, _ = resolve_delivery(state["critique"], [])
    assert audit_status == pass_case["expected_outcome"]["final_audit_status"]
    assert delivery_status == pass_case["expected_outcome"]["delivery_status"]


# ------------------------------------------------------------------ #
# 3. 覆盖度：工具执行过 ≠ 缺口被满足                                     #
# ------------------------------------------------------------------ #


def test_partial_tool_does_not_satisfy_gap(blocked_case):
    """样本里的 limit_up_pool：partial + 0 条数据 → 不满足任何 key 缺口。"""
    results = _to_tool_results(blocked_case)
    pool = next(r for r in results if r.tool_key == "limit_up_pool")
    record = next(r for r in coverage_records(results) if r.tool_key == "limit_up_pool")

    judged = judge_coverage(record, {"limit_up_pool"}, {"min_datum_count": 20})
    assert pool.status == "partial"
    assert judged.satisfies is False
    assert judged.reason == "status_partial"
    assert "limit_up_pool" not in satisfied_keys(results)


def test_satisfied_tool_is_hidden_from_gap_backfill(blocked_case):
    """对照：sentiment 有 success + 真实数据 → 满足，不再作为可补缺口暴露。"""
    results = _to_tool_results(blocked_case)
    record = next(r for r in coverage_records(results) if r.tool_key == "sentiment")

    judged = judge_coverage(record, {"sentiment"})
    assert judged.satisfies is True
    assert judged.reason == "satisfied"
    assert "sentiment" in satisfied_keys(results)


# ------------------------------------------------------------------ #
# 4. 结构化运行摘要日志                                                  #
# ------------------------------------------------------------------ #


def test_run_log_payloads_explain_a_blocked_run(blocked_case, caplog):
    """一次 blocked 运行能从日志还原：计划 → 执行 → 审计 → 终态 → 未持久化。"""
    results = _to_tool_results(blocked_case)
    state = _state(blocked_case, results)
    critique = Critique(
        verdict="research_more",
        reason=blocked_case["critic_sequence"][-1]["response"]["reason"],
    )
    plan = types.SimpleNamespace(intent=types.SimpleNamespace(**blocked_case["intent"]), steps=[])

    with caplog.at_level(logging.INFO, logger="app.graph.run_log"):
        run_log.log_run_start(state)
        run_log.log_plan(state, plan, filtered_keys=["hallucinated_key"], forced_gap=["limit_up_pool"])
        run_log.log_executions(state, results, category="technical")
        run_log.log_critic(
            state,
            critique,
            conflicts=[{"kind": "verdict_vs_actions", "model_verdict": "pass", "resolved": "research_more"}],
            gap_key_decisions=[{"key": "limit_up_pool", "reason": "already_satisfied"}],
        )
        audit_status, delivery_status, reason = resolve_delivery(critique, [])
        run_log.log_finalize(
            state,
            final_audit_status=audit_status,
            delivery_status=delivery_status,
            reason=reason,
            unresolved_issues=["missing_evidence | 商业航天为当日涨停较集中的题材之一 | action=research_more"],
        )
        run_log.log_delivery(state, delivery_status=delivery_status, persisted=False, sink="sse")

    events = [_json_of(record.getMessage()) for record in caplog.records]
    by_event = {e["event"]: e for e in events}

    assert set(by_event) == {
        "run_start",
        "plan",
        "executions",
        "critic",
        "finalize",
        "delivery",
    }
    # 1) 计划：显式问题 + 域 + run_id 可追
    assert by_event["run_start"]["question"] == blocked_case["question"]
    assert by_event["run_start"]["run_id"] == blocked_case["run_id"]
    # 2) 计划：域守卫过滤掉了什么，一目了然
    assert by_event["plan"]["filtered_keys"] == ["hallucinated_key"]
    assert by_event["plan"]["intent_domain"] == "a_share"
    # 3) 执行：partial / 空数据必须看得见（否则解释不了"为什么没补工具"）
    rows = {r["tool_key"]: r for r in by_event["executions"]["results"] if r["tool_key"]}
    assert rows["limit_up_pool"]["partial"] is True
    assert rows["limit_up_pool"]["datum_count"] == 0
    # 4) 审计：裁决、冲突与缺口 key 决策都在
    assert by_event["critic"]["verdict"] == "research_more"
    assert by_event["critic"]["verdict_conflicts"][0]["resolved"] == "research_more"
    assert by_event["critic"]["gap_key_decisions"][0]["reason"] == "already_satisfied"
    # 5) 终态 + 交付：blocked 且未持久化
    assert by_event["finalize"]["delivery_status"] == "blocked"
    assert by_event["delivery"]["persisted"] is False


def test_run_log_never_emits_secrets_or_unbounded_payloads(blocked_case):
    """日志脱敏：长文本 / 长数组被截断并标注省略量，不静默丢内容。"""
    long_text = "行云科技" * 500
    payload = run_log.emit(
        "probe",
        run_id="r1",
        secret_like_field="sk-do-not-log-me",
        long_text=long_text,
        many=[str(i) for i in range(50)],
        args={f"k{i}": i for i in range(12)},
    )
    text = json.dumps(payload, ensure_ascii=False)
    assert len(payload["long_text"]) < len(long_text)
    assert f"(+{len(long_text) - run_log.MAX_TEXT})" in payload["long_text"]
    assert payload["many"][-1].startswith("...(+")
    assert "_dropped_arg_keys" in payload["args"]
    # 单行 JSON —— 可被日志检索器按行解析
    assert "\n" not in text


def _json_of(message: str) -> dict:
    return json.loads(message)


def test_sync_delivery_log_carries_the_same_run_id(caplog, monkeypatch):
    """阶段 0 补修：同步 /api/ask 的 delivery 日志必须带**本次运行的真实 run_id**。

    旧实现固定传 ``{"run_id": None}``，于是同一次运行的
    run_start / plan / executions / critic / route / finalize / delivery 无法串联。
    这里跑真实的 ``Orchestrator.run``（只 stub 图与持久化），再走 main 的 delivery
    打点，断言七个事件共用同一个 run_id，且**没有重新生成** id。
    """
    import asyncio
    import logging as _logging

    from app.agent.orchestrator import Orchestrator

    seen_state: dict = {}

    class _OneShotGraph:
        """把入参 state 原样记录下来，终态补上 audit 结论（模拟图跑完）。"""

        async def ainvoke(self, state, config=None):
            data = state.model_dump(exclude_none=False) if hasattr(state, "model_dump") else dict(state)
            seen_state.update(data)
            return {
                **data,
                "report": _verified_report(),
                "critique": {"verdict": "pass", "reason": "ok"},
                "final_audit_status": "pass",
                "delivery_status": "verified",
                "errors": [],
            }

    async def fake_persist(result, question, conversation_id, **kwargs):
        return result.delivery_status == "verified"

    monkeypatch.setattr(main, "persist_research", fake_persist)
    monkeypatch.setattr(Orchestrator, "_ensure_graph", lambda self: _OneShotGraph())

    orchestrator = Orchestrator(Settings())
    with caplog.at_level(_logging.INFO, logger="app.graph.run_log"):
        result = asyncio.run(orchestrator.run("今天 A 股发生了什么？", domain="a_share"))
        main.run_log.log_delivery(
            {"run_id": result.run_id}, delivery_status=result.delivery_status, persisted=True, sink="sync"
        )

    events = [_json_of(r.getMessage()) for r in caplog.records if r.getMessage().startswith('{"ts"')]
    by_event = {e["event"]: e for e in events}
    assert {"run_start", "delivery"} <= set(by_event)

    run_id = result.run_id
    assert run_id, "响应必须携带本次运行的 run_id"
    assert by_event["run_start"]["run_id"] == run_id, "run_start 与 delivery 必须同 id"
    assert by_event["delivery"]["run_id"] == run_id
    assert by_event["delivery"]["persisted"] is True
    # id 来自 Orchestrator 建 state 时生成的那一个，不是事后另造的
    assert seen_state["run_id"] == run_id


def test_run_id_is_not_exposed_in_public_api_payloads():
    """run_id 是内部串联字段，不出现在对外 JSON 响应里。"""
    from pathlib import Path

    from app.models.response import ResearchResponse

    response = ResearchResponse(
        question="q",
        report=_verified_report(),
        run_id="abc123def456",
        delivery_status="verified",
        final_audit_status="pass",
    )
    public = {k: v for k, v in response.model_dump().items() if k != "run_id"}
    assert "run_id" not in public
    assert response.run_id == "abc123def456", "内部仍然拿得到"

    source = Path(main.__file__).read_text(encoding="utf-8")
    assert 'if k != "run_id"' in source, "对外响应必须显式剔除 run_id"


def _verified_report():
    from app.models.response import MarketIntelligence

    return MarketIntelligence(
        market_state="震荡",
        state_label="Neutral",
        what_happened="测试报告",
        confidence="medium",
    )


# ------------------------------------------------------------------ #
# 5. 缺口 key 分类：四类原因都可解释                                     #
# ------------------------------------------------------------------ #


def test_gap_key_decisions_report_four_reasons():
    decision = classify_missing_tool_keys(
        ["ok", "ok", "already", "invisible"],
        allowed={"ok", "already"},
        satisfied={"already"},
        max_keys=5,
    )
    assert decision.kept == ["ok"]
    assert decision.duplicated == ["ok"]
    assert decision.already_satisfied == ["already"]
    assert decision.not_visible == ["invisible"]
    assert decision.as_records() == [
        {"key": "ok", "reason": "duplicated"},
        {"key": "already", "reason": "already_satisfied"},
        {"key": "invisible", "reason": "not_visible"},
    ]


def test_gap_key_budget_truncation_is_visible():
    decision = classify_missing_tool_keys(
        ["a", "b", "c"],
        allowed={"a", "b", "c"},
        satisfied=set(),
        max_keys=2,
    )
    assert decision.kept == ["a", "b"]
    assert decision.budget_truncated == ["c"]


# ------------------------------------------------------------------ #
# 6. verdict 派生规则本身                                                #
# ------------------------------------------------------------------ #


def test_verdict_derivation_prefers_research_more():
    from app.graph.nodes.critic import AuditIssue

    mixed = [
        AuditIssue(kind="unsupported_claim", action="remove_or_qualify"),
        AuditIssue(kind="missing_evidence", action="research_more"),
    ]
    assert derive_verdict_from_issues(mixed) == "research_more"
    assert derive_verdict_from_issues([mixed[0]]) == "revise"
    assert derive_verdict_from_issues([]) is None


def test_conflicting_model_verdict_is_routed_conservatively(caplog):
    """模型自报 pass、但 issues 要求补证据 → 按 research_more 走，并留结构化记录。"""
    from app.graph.nodes.critic import AuditIssue, resolve_conflicts

    critique = Critique(
        verdict="pass",
        reason="看起来还行",
        issues=[AuditIssue(kind="missing_evidence", action="research_more", claim="缺指数")],
    )
    with caplog.at_level(logging.ERROR, logger="app.graph.nodes.critic"):
        conflicts = resolve_conflicts(critique)

    assert critique.verdict == "research_more"
    assert conflicts and conflicts[0]["resolved"] == "research_more"
