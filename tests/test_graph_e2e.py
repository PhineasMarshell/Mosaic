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

    async def fake_execute(self, tool_name, arguments, called_signatures, deadline=None):
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
        critique = state.get("critique")
        verdict = ""
        if isinstance(critique, dict):
            verdict = str(critique.get("verdict") or "")
        is_rewrite = (state.get("report") is not None) and verdict.strip().lower() == "revise"
        rewrite_count = (state.get("rewrite_count") or 0) + (1 if is_rewrite else 0)
        research_rounds = state.get("research_round_count") or 0
        return {
            "report": report,
            "rewrite_count": rewrite_count,
            "revision_count": rewrite_count + research_rounds,
        }

    monkeypatch.setattr(
        reason_mod.ReasoningNode, "__call__", fake_reasoning_call
    )  # T35-OK: e2e 验证图接线与报告产出，reasoning 语义由 test_reasoning_parsing 覆盖

    yield

    sup_mod.SupervisorNode.__init__ = orig_sup_init
    crit_mod.CriticNode.__init__ = orig_crit_init


@pytest.fixture()
def graph():
    """编译后的研究图（mock LLM + mock tools）。"""
    from app.graph.builder import build_graph

    return build_graph(Settings())


# 节点兜底降级分支的统一 errors 标记（T35；完整清单见 tests/conftest.py）。
# 注意 "Evidence gate: …"（T15 零证据合法降级）不是失败标记，不在此列。
_FALLBACK_MARKERS = (
    "analysis failed",  # analyst
    "gate failed",
    "critic audit failed",
    "reasoning engine failed",
    "reasoning node failed",
    "supervisor routing failed",
)


def _fallback_leaks(errors) -> list[str]:
    """从 errors 里挑出兜底降级标记（T35：防"节点内部炸了测试却绿"）。"""
    return [e for e in (errors or []) if any(m in str(e).lower() for m in _FALLBACK_MARKERS)]


# ------------------------------------------------------------------ #
# E2E 流程                                                             #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_full_flow_produces_report(graph):
    """全链路：supervisor → [analysts] → gate → reasoning → critic → END。

    T31：旧的 skip 理由（"P3 特性 — 三 analyst Send 并行，P0 图为 supervisor→kernel"）
    已过期——app/graph/builder.py 早已注册三个 analyst，这是全文件唯一一条
    真全链路用例，不该躺在 skip 里。

    阶段 3：问题带 6 位代码（个股口径）时才不会被注入市场级最低证据集，
    route 才会为空并触发"三个 analyst 全上"的降级路径 —— 本用例要验的正是
    后者（市场级问题走 tests/test_market_level_plan.py）。
    """
    result = await graph.ainvoke(
        {
            "question": "600519 今天怎么样？",
            "domain": "a_share",
            "conversation_id": None,
        }
    )

    assert result.get("report") is not None, "最终状态不应缺少 report"

    finding_analysts = {f.get("analyst") for f in result.get("findings", [])}
    assert "technical" in finding_analysts
    assert "fundamental" in finding_analysts
    assert "moneyflow" in finding_analysts

    # T35：任何节点滑进兜底 except 降级分支都必须让本用例变红，
    # 而不是被吞成 failed finding 后照样绿（Evidence gate 合法降级除外）。
    assert _fallback_leaks(result.get("errors")) == [], (
        f"有节点滑进兜底降级分支: {_fallback_leaks(result.get('errors'))}"
    )


@pytest.mark.asyncio
async def test_market_level_question_plans_and_executes_market_tools(graph):
    """阶段 3 验收：无 6 位代码的 A 股市场级问题，计划与实际执行都必须含市场级数据。

    旧行为：planner 计划 ``overview``（必须有 symbol）→ 执行层静默跳过 →
    报告在零市场级数据下撰写（"假覆盖"）。现在最低证据集由代码注入，
    且必然跳过的 step 会被剔除并记账。
    """
    from app.gateway.tool_registry import resolve_tool
    from app.graph.market_plan import MARKET_SUMMARY_MINIMUM

    result = await graph.ainvoke(
        {
            "question": "今天A股发生了什么？",
            "domain": "a_share",
            "conversation_id": None,
        }
    )

    minimum_keys = [key for key, _arguments, _purpose in MARKET_SUMMARY_MINIMUM]
    planned = [call["tool_key"] for assignment in result.get("route", []) for call in assignment["tool_calls"]]

    assert planned[: len(minimum_keys)] == minimum_keys, f"市场级最低证据集必须在计划最前: {planned}"
    assert "overview" not in planned, f"大盘问题里必然被跳过的 step 不该留在计划里: {planned}"

    executed = {tool for finding in result.get("findings", []) for tool in finding.get("tools_used", [])}
    expected_operation_ids = {resolve_tool(key).tool_name for key in minimum_keys}
    assert expected_operation_ids <= executed, f"计划了却没执行（假覆盖）: 期望 {expected_operation_ids} ⊄ {executed}"


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

    # T35：流式路径同样不得有节点滑进兜底 except 降级分支。
    for item in events:
        for node_name, update in item.items():
            errs = update.get("errors") if isinstance(update, dict) else None
            leaks = _fallback_leaks(errs)
            assert leaks == [], f"节点 {node_name} 滑进兜底降级分支: {leaks}"


# ------------------------------------------------------------------ #
# Analyst 隔离                                                         #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_analysis_finding_structure():
    """finding 的结构应符合 spec：{analyst, digest, tools_used, failed}，且**成功分支** failed=False。

    T28：旧实现的 fake 是非 awaitable 的 lambda（`lambda *a, **k: []`）→
    `await` 抛 TypeError 被 base.py 的兜底 except 吞掉 → 走异常降级分支
    failed=True，而断言只有 isinstance(...)——失败分支也"通过"，成功分支
    从未被覆盖（假阳性，已用探针证实）。现在 fake 必须可 await，
    并断言 failed is False / errors == []（防止再次静默滑进降级分支）。
    """
    from contextlib import asynccontextmanager

    from app.graph.nodes.analysts.base import MarketAnalystNode

    # T18T：_runtime 不能是 None —— __call__ 现在无条件进入 gateway_session()
    class _NoGatewayRuntime:
        def __init__(self):
            self.sessions = 0

        @asynccontextmanager
        async def gateway_session(self):
            self.sessions += 1
            try:
                yield None
            finally:
                self.sessions -= 1

        def truncate(self, result):
            pass

    node = object.__new__(MarketAnalystNode)
    node.category = "test"
    node.settings = Settings()
    node._runtime = _NoGatewayRuntime()

    async def _fake_execute_tools(self, state, sigs):
        return []

    node._execute_tools = types.MethodType(_fake_execute_tools, node)

    result = await node({})
    f = result["findings"][0]
    assert f["analyst"] == "test"
    assert isinstance(f["digest"], str)
    assert f["tools_used"] == []
    assert f["failed"] is False, f"应走成功分支，实际走了降级分支: {result}"
    assert result["errors"] == []
