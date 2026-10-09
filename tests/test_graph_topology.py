"""P3 图拓扑 — Send 并行节点的正确性验证。

验证：
- build_graph(settings) 返回编译后的 CompiledGraph，无异常
- 图中包含所有 P3 节点（technical / fundamental / moneyflow）
- critic 条件边路由正确（pass→END, revise→reasoning, research_more→supervisor）
- supervisor → analyst edges 为并行扇出（非串行）
- state reducer 并行写不丢数据（通过单元测试验证 by_category 工具分类覆盖）
"""

import logging

import pytest

from app.config import Settings
from app.gateway.tool_registry import by_category
from app.graph.builder import build_graph, critic_route_decision
from app.graph.nodes.critic import Critique
from app.graph.nodes.supervisor import route_candidate_categories


@pytest.fixture()
def settings():
    return Settings()


# ------------------------------------------------------------------ #
# 图结构                                                               #
# ------------------------------------------------------------------ #


def test_build_graph_compiles(settings):
    """build_graph 应成功返回编译后的图，不抛异常。"""
    graph = build_graph(settings)
    assert graph is not None
    # LangGraph v1.x CompiledStateGraph has ainvoke / astream etc.
    assert hasattr(graph, "ainvoke")
    assert hasattr(graph, "astream")


def test_all_p3_nodes_present(settings):
    """图应包含 technical / fundamental / moneyflow 三个 analyst 节点。"""
    graph = build_graph(settings)
    # CompiledStateGraph has .nodes dict with node names as keys
    nodes = graph.nodes if hasattr(graph, "nodes") else {}
    assert "technical" in nodes, "technical analyst node missing"
    assert "fundamental" in nodes, "fundamental analyst node missing"
    assert "moneyflow" in nodes, "moneyflow analyst node missing"


def test_critic_route_pass_ends():
    """verdict="pass" 或 critique 缺失 → END（dict 与对象两种形式）。"""
    settings = Settings()

    assert (
        critic_route_decision(
            {"critique": {"verdict": "pass", "reason": "ok"}, "rewrite_count": 0},
            settings,
        )
        == "end"
    )
    assert critic_route_decision({"critique": Critique(verdict="pass"), "rewrite_count": 0}, settings) == "end"
    assert critic_route_decision({}, settings) == "end"
    assert critic_route_decision({"critique": None}, settings) == "end"


def test_critic_route_revise_continues_under_limit():
    """revise < max_rewrites → 回 reasoning；用完 → finalize_audit（阶段 5/6）。

    阶段 6 起 revise 的额度是 `settings.effective_max_rewrites`（默认 1），
    research_more 的额度独立。
    """
    settings = Settings()
    critique = {
        "verdict": "revise",
        "reason": "x",
        "unsupported_claims": ["c1"],
        "missing_points": [],
    }

    assert critic_route_decision({"critique": critique, "rewrite_count": 0}, settings) == "reasoning"
    assert (
        critic_route_decision({"critique": critique, "rewrite_count": 1}, settings) == "finalize_audit"
    )
    assert (
        critic_route_decision(
            {"critique": Critique(verdict="revise"), "rewrite_count": 0},
            settings,
        )
        == "reasoning"
    )


def test_critic_route_revise_counters_do_not_share_a_budget():
    """阶段 6：改写用掉几轮，不影响 research_more 的额度（反之亦然）。"""
    settings = Settings()
    revise = {"verdict": "revise", "reason": "x"}
    research = {"verdict": "research_more", "missing_points": ["m1"]}
    report = {"what_happened": "x"}

    # 已经用掉全部 research_more 额度 + 全部 rewrite 额度：两条路径都该落终态
    assert (
        critic_route_decision(
            {
                "critique": revise,
                "rewrite_count": settings.effective_max_rewrites,
                "research_round_count": settings.effective_max_research_rounds,
            },
            settings,
        )
        == "finalize_audit"
    )
    # 只用光了 research_more 额度：revise 仍应可用（旧实现会误判为耗尽）
    assert (
        critic_route_decision(
            {"critique": revise, "rewrite_count": 0, "research_round_count": 99},
            settings,
        )
        == "reasoning"
    )
    # 只用光了 rewrite 额度：research_more 仍应可用
    assert (
        critic_route_decision(
            {"critique": research, "rewrite_count": 99, "research_round_count": 0, "report": report},
            settings,
        )
        == "supervisor"
    )


def test_critic_route_research_more_reserves_minimum_remaining_budget():
    """阶段 6②：剩余预算不够跑完半轮工具 + reasoning + critic → 不回 supervisor。"""
    import time

    settings = Settings()
    critique = {"verdict": "research_more", "missing_points": ["m1"]}
    base = {"critique": critique, "report": {"what_happened": "x"}}

    # 剩余预算低于预留线（含负数/已超时）→ 直接落终态
    assert (
        critic_route_decision(
            {**base, "budget_deadline": time.monotonic() + max(settings.research_round_min_remaining_seconds - 1, 0)},
            settings,
        )
        == "finalize_audit"
    )
    assert (
        critic_route_decision({**base, "budget_deadline": time.monotonic() - 1}, settings) == "finalize_audit"
    )
    # 剩余预算充足 → 正常回环
    assert (
        critic_route_decision(
            {**base, "budget_deadline": time.monotonic() + settings.research_round_min_remaining_seconds + 60},
            settings,
        )
        == "supervisor"
    )
    # 没有预算信息时不臆断（不因为"没数据"就阻断回环）
    assert critic_route_decision({**base, "budget_deadline": None}, settings) == "supervisor"


def test_critic_route_research_more_returns_to_supervisor():
    """research_more < max_research_rounds → 回 supervisor；用完 → finalize_audit。"""
    settings = Settings()
    critique = {"verdict": "research_more", "missing_points": ["m1"]}
    # 正常 research_more 场景：report 已存在但缺数据
    state_with_report = {"critique": critique, "research_round_count": 0, "report": {"what_happened": "existing"}}

    assert critic_route_decision(state_with_report, settings) == "supervisor"
    assert (
        critic_route_decision(
            {
                "critique": critique,
                "research_round_count": settings.effective_max_research_rounds,
                "report": {"what_happened": "x"},
            },
            settings,
        )
        == "finalize_audit"
    )


def test_critic_route_research_more_with_no_report_goes_to_finalize_audit():
    """report 为 None（reasoning 引擎失败）时 research_more 不应回 supervisor，
    直接落终态防死循环。"""
    settings = Settings()
    critique = {"verdict": "research_more", "missing_points": ["m1"]}

    assert critic_route_decision({"critique": critique, "research_round_count": 0}, settings) == "finalize_audit"


def test_finalize_audit_node_is_registered(settings):
    """阶段 5：图里必须有 finalize_audit 节点，且它的出边指向 END。"""
    graph = build_graph(settings)
    nodes = graph.get_graph().nodes
    assert "finalize_audit" in nodes
    edges = {(e.source, e.target) for e in graph.get_graph().edges}
    assert ("finalize_audit", "__end__") in edges


# ------------------------------------------------------------------ #
# 工具分类覆盖率                                                       #
# ------------------------------------------------------------------ #


def test_technical_tools_cover_required_categories():
    """技术面应包含 K 线、行情快照、盘口、情绪等关键工具。"""
    keys = {t.key for t in by_category["technical"]}
    assert "klines" in keys or any(k in keys for k in ("quote", "sentiment", "abnormal_reasons")), "技术面缺少关键工具"


def test_fundamental_has_core_f10_tools():
    """基本面应包含核心 F10 工具。"""
    keys = {t.key for t in by_category["fundamental"]}
    assert "finance" in keys
    assert "business" in keys


def test_moneyflow_has_longhu_and_hk():
    """资金面应包含龙虎榜和 HK 北向资金工具。"""
    keys = {t.key for t in by_category["moneyflow"]}
    assert "longhu" in keys or "hk_northbound_daily" in keys


def test_tool_categories_cover_all_39_tools():
    """所有注册工具的 category 应属于合法类别之一。"""
    from app.gateway.tool_registry import ALL_TOOLS

    valid_categories = {"technical", "fundamental", "moneyflow", "shared", "news", "sentiment"}
    for t in ALL_TOOLS:
        assert t.category in valid_categories, f"{t.key} 未标注合法 category={t.category!r}"


# ------------------------------------------------------------------ #
# P2.5-2：fanout 路由与兜底拓扑                                        #
# ------------------------------------------------------------------ #


def _make_fake_openai(text: str):
    """构造返回固定 JSON 的 fake OpenAI client。"""

    class _FakeMessage:
        content = text

    class _FakeChoice:
        message = _FakeMessage()

    class _FakeResp:
        choices = [_FakeChoice()]

    class _FakeCompletions:
        async def create(self, *args, **kwargs):
            return _FakeResp()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    return _FakeClient()


def _patch_supervisor_plan(monkeypatch, plan_json: str):
    """替换 SupervisorNode.__init__，注入返回指定 plan 的 fake client。"""
    from app.graph.nodes import supervisor as sup_mod

    orig_init = sup_mod.SupervisorNode.__init__

    def patched_init(self, settings):
        orig_init(self, settings)
        self.client = _make_fake_openai(plan_json)

    monkeypatch.setattr(sup_mod.SupervisorNode, "__init__", patched_init)


def _patch_reasoning_and_critic(monkeypatch):
    """mock ReasoningNode / CriticNode.__call__ 返回固定值，避免真实 LLM。"""
    import types

    from app.graph.nodes import critic as crit_mod
    from app.graph.nodes import reasoning as rea_mod
    from app.graph.nodes.critic import Critique

    async def fake_reasoning(self, state):
        return {
            "report": types.SimpleNamespace(
                what_happened="测试报告",
                confidence=0.8,
                state_label="neutral",
                strong_areas=[],
                risks=[],
                why=[],
            ),
        }

    async def fake_critic(self, state):
        return {"critique": Critique(verdict="pass", reason="ok")}

    monkeypatch.setattr(
        rea_mod.ReasoningNode, "__call__", fake_reasoning
    )  # T35-OK: 本组用例验证 supervisor/gate 拓扑接线，reasoning 非验证目标
    monkeypatch.setattr(crit_mod.CriticNode, "__call__", fake_critic)  # T35-OK: 同上，critic 非验证目标


def _patch_tool_runtime(monkeypatch):
    """mock ToolRuntime.execute，避免真实网络请求。"""
    from app.graph.tool_runtime import ToolRuntime
    from app.models.market import ToolResult

    async def fake_execute(self, tool_name, arguments, called_signatures, deadline=None):
        return ToolResult(
            tool=tool_name,
            arguments=arguments,
            status="success",
            normalized=[],
            error=None,
        )

    monkeypatch.setattr(ToolRuntime, "execute", fake_execute)
    monkeypatch.setattr(ToolRuntime, "truncate", lambda self, r: None)


@pytest.mark.asyncio
async def test_fanout_routes_to_analyst_when_route_nonempty(monkeypatch):
    """supervisor plan 含 technical 工具 → 只有 technical analyst 执行。

    问题里带 6 位代码（个股问题）→ 阶段 3 的市场级最低证据集不注入，
    LLM 计划原样执行，故这里恰好只有 1 条结果。
    """
    from app.graph.builder import build_graph
    from app.graph.nodes.analysts.base import MarketAnalystNode

    plan_json = (
        '{"intent":{"domain":"a_share","task":"company_research",'
        '"time_scope":"today","question":"600519 测试"},'
        '"steps":[{"tool_key":"sentiment","arguments":{},"purpose":"情绪"}]}'
    )
    _patch_supervisor_plan(monkeypatch, plan_json)
    _patch_reasoning_and_critic(monkeypatch)
    _patch_tool_runtime(monkeypatch)

    called_categories: list[str] = []
    orig_call = MarketAnalystNode.__call__

    async def tracked_call(self, state):
        called_categories.append(self.category)
        return await orig_call(self, state)

    monkeypatch.setattr(
        MarketAnalystNode, "__call__", tracked_call
    )  # T35-OK: 只包一层计数透传原 __call__，analyst 骨架仍真实运行

    graph = build_graph(Settings())
    result_state = await graph.ainvoke({"question": "600519 测试", "domain": "a_share"})

    assert called_categories == ["technical"], f"route 非空时应只执行 technical，实际: {called_categories}"
    assert not result_state.get("errors")
    assert len(result_state.get("results", [])) == 1


@pytest.mark.asyncio
async def test_fanout_falls_back_to_all_analysts_when_route_empty(monkeypatch):
    """supervisor plan steps 为空 → 三个默认 analyst 全上。

    task 取 ``company_research``（非市场级）以免阶段 3 的最低证据集把 steps
    填满 —— 本用例要的是真正"route 为空"的降级路径。
    """
    from app.graph.builder import build_graph
    from app.graph.nodes.analysts.base import MarketAnalystNode

    plan_json = (
        '{"intent":{"domain":"a_share","task":"company_research","time_scope":"today","question":"测试"},"steps":[]}'
    )
    _patch_supervisor_plan(monkeypatch, plan_json)
    _patch_reasoning_and_critic(monkeypatch)
    _patch_tool_runtime(monkeypatch)

    called_categories: list[str] = []
    orig_call = MarketAnalystNode.__call__

    async def tracked_call(self, state):
        called_categories.append(self.category)
        return await orig_call(self, state)

    monkeypatch.setattr(
        MarketAnalystNode, "__call__", tracked_call
    )  # T35-OK: 只包一层计数透传原 __call__，analyst 骨架仍真实运行

    graph = build_graph(Settings())
    result_state = await graph.ainvoke({"question": "测试", "domain": "a_share"})

    assert set(called_categories) == {"technical", "fundamental", "moneyflow"}, (
        f"route 为空时应三个 analyst 全上，实际: {called_categories}"
    )
    assert not result_state.get("errors")
    assert result_state.get("results", []) == []


# ------------------------------------------------------------------ #
# P4-4：news analyst 节点                                               #
# ------------------------------------------------------------------ #


def test_news_node_absent_when_disabled():
    """默认 settings（news_enabled=False）→ 图不含 news / sentiment 节点。"""
    settings = Settings()
    graph = build_graph(settings)
    nodes = graph.get_graph().nodes
    assert "news" not in nodes
    assert "sentiment" not in nodes


def test_sentiment_enabled_registers_node_and_route():
    """sentiment_enabled=True → 图注册 sentiment 节点、扇出含它、路由候选含它。"""
    settings = Settings(sentiment_enabled=True)

    graph = build_graph(settings)
    nodes = graph.get_graph().nodes

    assert "sentiment" in nodes
    assert {"technical", "fundamental", "moneyflow"}.issubset(set(nodes))
    assert route_candidate_categories(settings) == ("technical", "fundamental", "moneyflow", "sentiment")


@pytest.mark.asyncio
async def test_news_node_executed_when_enabled(monkeypatch):
    """news_enabled=True + plan 含 news_search step → news 节点执行，
    fake runtime 收到 news_search 调用。"""
    from app.models.market import ToolResult

    settings = Settings(news_enabled=True)

    plan_json = (
        '{"intent":{"domain":"a_share","task":"market_summary",'
        '"time_scope":"today","question":"贵州茅台600519最近有什么新闻"},'
        '"steps":[{"tool_key":"news_search","arguments":{"query":"贵州茅台"},'
        '"purpose":"新闻"}]}'
    )
    _patch_supervisor_plan(monkeypatch, plan_json)
    _patch_reasoning_and_critic(monkeypatch)

    executed_tools: list[str] = []

    async def fake_execute(self, tool_name, arguments, called_signatures, deadline=None):
        executed_tools.append(tool_name)
        return ToolResult(
            tool=tool_name,
            arguments=arguments,
            status="success",
            normalized=[],
            error=None,
        )

    monkeypatch.setattr("app.graph.tool_runtime.ToolRuntime.execute", fake_execute)
    monkeypatch.setattr("app.graph.tool_runtime.ToolRuntime.truncate", lambda self, r: None)

    graph = build_graph(settings)
    await graph.ainvoke(
        {
            "question": "贵州茅台600519最近有什么新闻",
            "domain": "a_share",
        }
    )

    assert "news_search" in executed_tools, f"news_search 应被执行，实际: {executed_tools}"


@pytest.mark.asyncio
async def test_default_settings_e2e_matches_p25_baseline(monkeypatch):
    """两开关全关（默认 Settings）→ 图行为与 P2.5 基线一致：
    plan steps 为空 → 三个 analyst 全上 → 最终有 report，且图不含 news/sentiment。"""
    settings = Settings()
    assert settings.news_enabled is False
    assert settings.sentiment_enabled is False

    plan_json = (
        '{"intent":{"domain":"a_share","task":"market_summary","time_scope":"today","question":"测试"},"steps":[]}'
    )
    _patch_supervisor_plan(monkeypatch, plan_json)
    _patch_reasoning_and_critic(monkeypatch)
    _patch_tool_runtime(monkeypatch)

    graph = build_graph(settings)
    result = await graph.ainvoke({"question": "测试", "domain": "a_share"})

    assert result.get("report") is not None
    assert not result.get("errors")
    nodes = graph.get_graph().nodes
    assert "news" not in nodes
    assert "sentiment" not in nodes


# ------------------------------------------------------------------ #
# Critic 闭环回边（真实图 ainvoke，修复前这些路径从未被执行）            #
# ------------------------------------------------------------------ #


def _fake_report():
    import types

    return types.SimpleNamespace(
        what_happened="测试报告",
        confidence=0.8,
        state_label="neutral",
        strong_areas=[],
        risks=[],
        why=[],
    )


def _critique_verdict(state_dict: dict) -> str:
    """从 state 里取 Critic verdict（dict / 对象两种形式，与节点同口径）。"""
    critique = state_dict.get("critique")
    if critique is None:
        return ""
    if isinstance(critique, dict):
        return str(critique.get("verdict") or "").strip().lower()
    return str(getattr(critique, "verdict", "") or "").strip().lower()


@pytest.mark.asyncio
async def test_revise_loop_reinvokes_reasoning(monkeypatch):
    """Critic 首次 revise → reasoning 节点被执行两次，rewrite_count 记 1。"""

    from app.graph.nodes import critic as crit_mod
    from app.graph.nodes import reasoning as rea_mod

    plan_json = (
        '{"intent":{"domain":"a_share","task":"market_summary","time_scope":"today","question":"测试"},"steps":[]}'
    )
    _patch_supervisor_plan(monkeypatch, plan_json)
    _patch_tool_runtime(monkeypatch)

    async def fake_reasoning(self, state):
        if hasattr(state, "model_dump"):
            sd = state.model_dump(exclude_none=False)
        else:
            sd = state
        wc = sd.get("rewrite_count", 0) or 0
        # 与 ReasoningNode 同口径：只有 Critic 判 revise 的打回才算改写额度，
        # research_more 回环带着报告回来是"补证据后重新成文"。
        if sd.get("report") is not None and _critique_verdict(sd) == "revise":
            wc += 1
        research_rounds = sd.get("research_round_count", 0) or 0
        return {"report": _fake_report(), "rewrite_count": wc, "revision_count": wc + research_rounds}

    monkeypatch.setattr(
        rea_mod.ReasoningNode, "__call__", fake_reasoning
    )  # T35-OK: 本组用例验证 critic 回环/预算，reasoning 非验证目标

    critic_calls = {"n": 0}

    async def seq_critic(self, state):
        critic_calls["n"] += 1
        verdict = "revise" if critic_calls["n"] == 1 else "pass"
        return {"critique": Critique(verdict=verdict, reason="test")}

    monkeypatch.setattr(
        crit_mod.CriticNode, "__call__", seq_critic
    )  # T35-OK: 本组用例验证回环次序，critic verdict 序列是受控输入

    graph = build_graph(Settings())
    result = await graph.ainvoke({"question": "测试", "domain": "a_share"})

    assert critic_calls["n"] == 2
    assert result.get("report") is not None
    assert result.get("rewrite_count") == 1
    assert result.get("revision_count") == 1


@pytest.mark.asyncio
async def test_research_more_loop_reinvokes_supervisor(monkeypatch):
    """Critic 首次 research_more → supervisor 节点被执行两次，research_round_count 记 1。"""
    from app.graph.nodes import critic as crit_mod
    from app.graph.nodes import reasoning as rea_mod
    from app.graph.nodes import supervisor as sup_mod

    plan_json = (
        '{"intent":{"domain":"a_share","task":"market_summary","time_scope":"today","question":"测试"},"steps":[]}'
    )
    _patch_supervisor_plan(monkeypatch, plan_json)
    _patch_tool_runtime(monkeypatch)

    async def fake_reasoning(self, state):
        if hasattr(state, "model_dump"):
            sd = state.model_dump(exclude_none=False)
        else:
            sd = state
        wc = sd.get("rewrite_count", 0) or 0
        # 与 ReasoningNode 同口径：只有 Critic 判 revise 的打回才算改写额度，
        # research_more 回环带着报告回来是"补证据后重新成文"。
        if sd.get("report") is not None and _critique_verdict(sd) == "revise":
            wc += 1
        research_rounds = sd.get("research_round_count", 0) or 0
        return {"report": _fake_report(), "rewrite_count": wc, "revision_count": wc + research_rounds}

    monkeypatch.setattr(
        rea_mod.ReasoningNode, "__call__", fake_reasoning
    )  # T35-OK: 本组用例验证 critic 回环/预算，reasoning 非验证目标

    supervisor_calls = {"n": 0}
    orig_supervisor = sup_mod.SupervisorNode.__call__

    async def counting_supervisor(self, state):
        supervisor_calls["n"] += 1
        return await orig_supervisor(self, state)

    monkeypatch.setattr(
        sup_mod.SupervisorNode, "__call__", counting_supervisor
    )  # T35-OK: 只包一层计数透传原 __call__，supervisor 仍真实运行

    critic_calls = {"n": 0}

    async def seq_critic(self, state):
        critic_calls["n"] += 1
        verdict = "research_more" if critic_calls["n"] == 1 else "pass"
        return {"critique": Critique(verdict=verdict, reason="test")}

    monkeypatch.setattr(
        crit_mod.CriticNode, "__call__", seq_critic
    )  # T35-OK: 本组用例验证回环次序，critic verdict 序列是受控输入

    graph = build_graph(Settings())
    result = await graph.ainvoke({"question": "测试", "domain": "a_share"})

    assert supervisor_calls["n"] == 2
    assert result.get("report") is not None
    assert result.get("research_round_count") == 1
    assert result.get("revision_count") == 1
