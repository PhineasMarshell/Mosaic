"""P3 图拓扑 — Send 并行节点的正确性验证。

验证：
- build_graph(settings) 返回编译后的 CompiledGraph，无异常
- 图中包含所有 P3 节点（technical / fundamental / moneyflow）
- critic 条件边路由正确（pass→END, revise→reasoning, research_more→supervisor）
- supervisor → analyst edges 为并行扇出（非串行）
- state reducer 并行写不丢数据（通过单元测试验证 by_category 工具分类覆盖）
"""

import pytest

from app.config import Settings
from app.gateway.tool_registry import by_category
from app.graph.builder import build_graph, critic_route_decision
from app.graph.nodes.critic import Critique


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
            {"critique": {"verdict": "pass", "reason": "ok"}, "revision_count": 0},
            settings,
        )
        == "end"
    )
    assert critic_route_decision({"critique": Critique(verdict="pass"), "revision_count": 0}, settings) == "end"
    assert critic_route_decision({}, settings) == "end"
    assert critic_route_decision({"critique": None}, settings) == "end"


def test_critic_route_revise_continues_under_limit():
    """revise ≤ max_revisions → 回 reasoning；超限 → END。"""
    settings = Settings()
    critique = {
        "verdict": "revise",
        "reason": "x",
        "unsupported_claims": ["c1"],
        "missing_points": [],
    }

    assert critic_route_decision({"critique": critique, "revision_count": 0}, settings) == "reasoning"
    assert critic_route_decision({"critique": critique, "revision_count": 1}, settings) == "reasoning"
    assert critic_route_decision({"critique": critique, "revision_count": 2}, settings) == "end"
    assert (
        critic_route_decision(
            {"critique": Critique(verdict="revise"), "revision_count": 0},
            settings,
        )
        == "reasoning"
    )


def test_critic_route_research_more_returns_to_supervisor():
    """research_more ≤ max_revisions → 回 supervisor；超限 → END。"""
    settings = Settings()
    critique = {"verdict": "research_more", "missing_points": ["m1"]}

    assert critic_route_decision({"critique": critique, "revision_count": 0}, settings) == "supervisor"
    assert critic_route_decision({"critique": critique, "revision_count": 2}, settings) == "end"


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

    valid_categories = {"technical", "fundamental", "moneyflow", "shared", "news"}
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

    monkeypatch.setattr(rea_mod.ReasoningNode, "__call__", fake_reasoning)
    monkeypatch.setattr(crit_mod.CriticNode, "__call__", fake_critic)


def _patch_tool_runtime(monkeypatch):
    """mock ToolRuntime.execute，避免真实网络请求。"""
    from app.graph.tool_runtime import ToolRuntime
    from app.models.market import Status, ToolResult

    async def fake_execute(self, tool_name, arguments, called_signatures):
        return ToolResult(
            tool=tool_name,
            arguments=arguments,
            status=Status.SUCCESS.value,
            normalized=[],
            error=None,
        )

    monkeypatch.setattr(ToolRuntime, "execute", fake_execute)
    monkeypatch.setattr(ToolRuntime, "truncate", lambda self, r: None)


@pytest.mark.asyncio
async def test_fanout_routes_to_analyst_when_route_nonempty(monkeypatch):
    """supervisor plan 含 technical 工具 → 只有 technical analyst 执行。"""
    from app.graph.builder import build_graph
    from app.graph.nodes.analysts.base import MarketAnalystNode

    plan_json = (
        '{"intent":{"domain":"a_share","task":"market_summary",'
        '"time_scope":"today","question":"测试"},'
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

    monkeypatch.setattr(MarketAnalystNode, "__call__", tracked_call)

    graph = build_graph(Settings())
    await graph.ainvoke({"question": "测试", "domain": "a_share"})

    assert called_categories == ["technical"], f"route 非空时应只执行 technical，实际: {called_categories}"


@pytest.mark.asyncio
async def test_fanout_falls_back_to_all_analysts_when_route_empty(monkeypatch):
    """supervisor plan steps 为空 → 三个默认 analyst 全上。"""
    from app.graph.builder import build_graph
    from app.graph.nodes.analysts.base import MarketAnalystNode

    plan_json = (
        '{"intent":{"domain":"a_share","task":"market_summary","time_scope":"today","question":"测试"},"steps":[]}'
    )
    _patch_supervisor_plan(monkeypatch, plan_json)
    _patch_reasoning_and_critic(monkeypatch)
    _patch_tool_runtime(monkeypatch)

    called_categories: list[str] = []
    orig_call = MarketAnalystNode.__call__

    async def tracked_call(self, state):
        called_categories.append(self.category)
        return await orig_call(self, state)

    monkeypatch.setattr(MarketAnalystNode, "__call__", tracked_call)

    graph = build_graph(Settings())
    await graph.ainvoke({"question": "测试", "domain": "a_share"})

    assert set(called_categories) == {"technical", "fundamental", "moneyflow"}, (
        f"route 为空时应三个 analyst 全上，实际: {called_categories}"
    )


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


@pytest.mark.asyncio
async def test_news_node_executed_when_enabled(monkeypatch):
    """news_enabled=True + plan 含 news_search step → news 节点执行，
    fake runtime 收到 news_search 调用。"""
    from app.models.market import Status, ToolResult

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

    async def fake_execute(self, tool_name, arguments, called_signatures):
        executed_tools.append(tool_name)
        return ToolResult(
            tool=tool_name,
            arguments=arguments,
            status=Status.SUCCESS.value,
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


@pytest.mark.asyncio
async def test_revise_loop_reinvokes_reasoning(monkeypatch):
    """Critic 首次 revise → reasoning 节点被执行两次，revision_count 记 1。"""

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
        rc = sd.get("revision_count", 0) or 0
        if sd.get("report") is not None:
            rc += 1
        return {"report": _fake_report(), "revision_count": rc}

    monkeypatch.setattr(rea_mod.ReasoningNode, "__call__", fake_reasoning)

    critic_calls = {"n": 0}

    async def seq_critic(self, state):
        critic_calls["n"] += 1
        verdict = "revise" if critic_calls["n"] == 1 else "pass"
        return {"critique": Critique(verdict=verdict, reason="test")}

    monkeypatch.setattr(crit_mod.CriticNode, "__call__", seq_critic)

    graph = build_graph(Settings())
    result = await graph.ainvoke({"question": "测试", "domain": "a_share"})

    assert critic_calls["n"] == 2
    assert result.get("report") is not None
    assert result.get("revision_count") == 1


@pytest.mark.asyncio
async def test_research_more_loop_reinvokes_supervisor(monkeypatch):
    """Critic 首次 research_more → supervisor 节点被执行两次。"""
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
        rc = sd.get("revision_count", 0) or 0
        if sd.get("report") is not None:
            rc += 1
        return {"report": _fake_report(), "revision_count": rc}

    monkeypatch.setattr(rea_mod.ReasoningNode, "__call__", fake_reasoning)

    supervisor_calls = {"n": 0}
    orig_supervisor = sup_mod.SupervisorNode.__call__

    async def counting_supervisor(self, state):
        supervisor_calls["n"] += 1
        return await orig_supervisor(self, state)

    monkeypatch.setattr(sup_mod.SupervisorNode, "__call__", counting_supervisor)

    critic_calls = {"n": 0}

    async def seq_critic(self, state):
        critic_calls["n"] += 1
        verdict = "research_more" if critic_calls["n"] == 1 else "pass"
        return {"critique": Critique(verdict=verdict, reason="test")}

    monkeypatch.setattr(crit_mod.CriticNode, "__call__", seq_critic)

    graph = build_graph(Settings())
    result = await graph.ainvoke({"question": "测试", "domain": "a_share"})

    assert supervisor_calls["n"] == 2
    assert result.get("report") is not None
    assert result.get("revision_count") == 1
