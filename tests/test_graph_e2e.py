"""P3 图端到端 — mock LLM + mock tools 的全链路验证。"""

import types

import pytest

from app.config import Settings

# ------------------------------------------------------------------ #
# Fixtures                                                             #
# ------------------------------------------------------------------ #


@pytest.fixture(autouse=True)
def mock_tools(monkeypatch):
    """Mock ToolRuntime.execute → avoid real MCP calls."""
    from app.models.market import ToolResult

    async def fake_execute(self, tool_name, arguments, called_signatures):
        return ToolResult(
            tool=tool_name,
            arguments=arguments,
            status="success",
            normalized=[],
            error=None,
        )

    from app.graph.tool_runtime import ToolRuntime

    monkeypatch.setattr(ToolRuntime, "execute", fake_execute)


@pytest.fixture(autouse=True)
def mock_openai(monkeypatch):
    """Mock OpenAI chat completions for Supervisor & Critic."""
    plan_json = (
        '{"intent":{"domain":"a_share","task":"market_summary","time_scope":"today","question":"测试"},"steps":[]}'
    )
    critique_json = '{"verdict":"pass","reason":"证据充足"}'

    class FakeChoice:
        def __init__(self, content):
            self.message = types.SimpleNamespace(content=content)

    class FakeResp:
        def __init__(self, content):
            self.choices = [FakeChoice(content)]

    def make_create(text):
        async def create(*args, **kwargs):
            return FakeResp(text)

        return create

    # Patch Supervisor client
    from app.graph.nodes import supervisor as sup_mod

    orig_sup_init = sup_mod.SupervisorNode.__init__

    def patched_sup_init(self, settings):
        orig_sup_init(self, settings)
        self.client.chat.completions.create = make_create(plan_json)

    sup_mod.SupervisorNode.__init__ = patched_sup_init

    # Patch Critic client
    from app.graph.nodes import critic as crit_mod

    orig_crit_init = crit_mod.CriticNode.__init__

    def patched_crit_init(self, settings):
        orig_crit_init(self, settings)
        self.client.chat.completions.create = make_create(critique_json)

    crit_mod.CriticNode.__init__ = patched_crit_init

    # Patch Reasoning node — 返回固定 report，不调真实 LLM
    from app.graph.nodes import reasoning as reason_mod
    from app.models.response import MarketIntelligence

    async def fake_reasoning_call(self, state):
        if hasattr(state, "model_dump"):
            state = state.model_dump(exclude_none=False)
        report = MarketIntelligence(
            market_state="neutral",
            state_label="测试状态",
            what_happened="测试：mock 报告",
            confidence="medium",
        )
        is_revision = state.get("report") is not None
        revision_count = state.get("revision_count") or 0
        return {
            "report": report,
            "revision_count": revision_count + (1 if is_revision else 0),
        }

    monkeypatch.setattr(reason_mod.ReasoningNode, "__call__", fake_reasoning_call)

    yield

    sup_mod.SupervisorNode.__init__ = orig_sup_init
    crit_mod.CriticNode.__init__ = orig_crit_init


@pytest.fixture()
def graph():
    """编译后的研究图（mock LLM + mock tools）。"""
    from app.graph.builder import build_graph

    return build_graph(Settings())


# ------------------------------------------------------------------ #
# E2E 流程                                                             #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
@pytest.mark.skip(reason="P3 特性 — 三 analyst Send 并行，P0 图为 supervisor→kernel")
async def test_full_flow_produces_report(graph):
    """全链路：supervisor → [analysts] → gate → reasoning → critic → END。"""
    result = await graph.ainvoke(
        {
            "question": "今天A股行情如何？",
            "domain": "a_share",
            "conversation_id": None,
        }
    )

    assert result.get("report") is not None, "最终状态不应缺少 report"

    finding_analysts = {f.get("analyst") for f in result.get("findings", [])}
    assert "technical" in finding_analysts
    assert "fundamental" in finding_analysts
    assert "moneyflow" in finding_analysts


@pytest.mark.asyncio
async def test_stream_emits_node_events(graph):
    """astream(stream_mode='updates') 应按节点产出事件（LangGraph v1.x format）。"""
    events = []
    async for item in graph.astream(
        {"question": "测试问题", "domain": "a_share"},
        stream_mode="updates",
    ):
        if isinstance(item, dict):
            events.append(item)

    names = set()
    for item in events:
        names.update(item.keys())

    assert "supervisor" in names


# ------------------------------------------------------------------ #
# Analyst 隔离                                                         #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
@pytest.mark.skip(reason="P3 特性 — MarketAnalystNode 依赖 by_category")
async def test_analysis_finding_structure():
    """finding 的结构应符合 spec：{analyst, digest, tools_used, failed}。"""
    from app.graph.nodes.analysts.base import MarketAnalystNode

    node = object.__new__(MarketAnalystNode)
    node.category = "test"
    node.settings = Settings()
    node._runtime = None
    node._execute_tools = lambda *a, **k: ([])

    result = await node({})
    f = result["findings"][0]
    assert f["analyst"] == "test"
    assert isinstance(f["digest"], str)
    assert isinstance(f["tools_used"], list)
    assert isinstance(f["failed"], bool)
