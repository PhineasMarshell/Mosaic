"""Analyst 节点 — 单元测试。

验证：
- 每个 analyst 通过 by_category 发现正确工具集
- MarketAnalystNode.__call__ 返回正确的 state 字段
- budget 守卫生效
- 异常降级为 failed finding + errors 追加
- set_enabled_domains 动态影响分类（如果后续加入）
"""

import types
from contextlib import asynccontextmanager

import pytest

from app.config import Settings
from app.gateway.tool_registry import by_category
from app.graph.nodes.analysts.base import MarketAnalystNode

# ------------------------------------------------------------------ #
# Fixtures                                                            #
# ------------------------------------------------------------------ #


class _NoGatewayRuntime:
    """T18T：只提供 analyst 骨架需要的接口 —— 会话（空实现）+ truncate。

    背景：T18 之后 MarketAnalystNode.__call__ 无条件进入
    ``self._runtime.gateway_session()``，旧 fixture 把 ``_runtime`` 设成 None
    （或用缺 ``gateway_session`` 的 FakeRuntime 整体替换），AttributeError 被
    base.py 的兜底 except 吞成 ``failed=True``，用例再取 ``result["results"]``
    变成难懂的 KeyError。本 stub 只模拟会话边界，不模拟工具执行。
    """

    def __init__(self):
        self.sessions = 0  # 当前并发深度（进出相抵）
        self.entered = 0  # 累计进入次数（只增，供事后断言）

    @asynccontextmanager
    async def gateway_session(self):
        self.sessions += 1
        self.entered += 1
        try:
            yield None
        finally:
            self.sessions -= 1

    def truncate(self, result):
        pass


@pytest.fixture()
def mock_runtime(monkeypatch):
    """用同步调用伪造 ToolRuntime.execute，避免真实网络请求。"""
    results = []

    async def fake_execute(tool_name, arguments, called_signatures, deadline=None):
        result = types.SimpleNamespace(
            tool=tool_name,
            arguments=arguments,
            status="success",
            normalized=[],
            error=None,
        )
        results.append(result)
        return result

    monkeypatch.setattr("app.graph.nodes.analysts.base.ToolRuntime.execute", fake_execute)
    return results


@pytest.fixture()
def base_node():
    """匿名分析员实例（category="unknown"，不匹配任何类别 → 无工具可执行）。"""
    settings = Settings()
    node = object.__new__(MarketAnalystNode)
    node.settings = settings
    node.category = "nonexistent"  # 不匹配任何注册类别
    node._runtime = _NoGatewayRuntime()
    return node


# ------------------------------------------------------------------ #
# 核心行为                                                             #
# ------------------------------------------------------------------ #


def test_by_category_returns_tools():
    """by_category 索引应包含技术/基本面/资金面/共享四个维度。"""
    assert len(by_category["technical"]) > 0
    assert len(by_category["fundamental"]) > 0
    assert len(by_category["moneyflow"]) > 0
    assert len(by_category["shared"]) >= 0  # shared 可能为空（目前只有 health）


def test_by_category_tools_have_required_attributes():
    """每条目至少含 key / tool_name / category 属性。"""
    for tools in by_category.values():
        for t in tools:
            assert hasattr(t, "key")
            assert hasattr(t, "tool_name")
            assert hasattr(t, "category")


@pytest.mark.asyncio
async def test_analyst_with_no_matching_tools_returns_empty(node: MarketAnalystNode, mock_runtime):
    """category 未匹配 → 不执行任何工具 → results=[]，findings 标记 failed=False。"""
    result = await node({})
    assert isinstance(result["results"], list)
    assert result["results"] == []
    assert len(result["findings"]) == 1
    assert result["findings"][0]["failed"] is False
    assert result["errors"] == []
    # 走的仍是生产 __call__ 骨架：会话确实被进入过（T18T）
    assert node._runtime.entered >= 1


# ------------------------------------------------------------------ #
# Budget 守卫                                                          #
# ------------------------------------------------------------------ #


# D4 语义裁决（写进测试，不许再含糊）：budget 计**真实执行次数**——
# unknown tool_key / 缺 symbol / T27 重复签名这些被跳过的调用**不消耗**预算；
# 显式 budget=0 = 不执行；config.max_tool_calls 是真实硬上限。
_ROUTE_TOOLS = [
    {"tool_key": "sentiment", "arguments": {}},
    {"tool_key": "limit_up_count", "arguments": {}},
    {"tool_key": "limit_up_sectors", "arguments": {}},
]

# T30/D4 专用：实例级 FakeRuntime（记录真实执行的工具名）——
# mock_runtime 打的是 ToolRuntime 类补丁，而 _execute_tools 走
# self._runtime.execute，实例属性替换后类补丁根本不会被触到
# （旧 test_budget_respected 恒真的根源之一：连执行路径都没接上）。


def _make_budget_node(max_tool_calls: int = 12):
    """构造 (node, executed_tool_names)：node 的 _runtime 是记录执行的 stub。"""
    executed: list[str] = []

    class _RecordingRuntime(_NoGatewayRuntime):
        async def execute(self, tool_name, arguments, called_signatures, deadline=None):
            executed.append(tool_name)
            return types.SimpleNamespace(
                tool=tool_name,
                arguments=arguments,
                status="success",
                normalized=[],
                error=None,
            )

    node = object.__new__(MarketAnalystNode)
    node.settings = Settings(max_tool_calls=max_tool_calls)
    node.category = "nonexistent"
    node._runtime = _RecordingRuntime()
    return node, executed


@pytest.mark.asyncio
async def test_budget_limits_real_executions():
    """budget=1、route 有 3 个不同工具 → 只真实执行 1 次。

    T30：旧实现只断言 `__code__.co_varnames` 恒真，budget 守卫删掉也不报错。"""
    node, executed = _make_budget_node()
    route = [{"analyst": "nonexistent", "budget": 1, "tool_calls": _ROUTE_TOOLS}]
    results = await node._execute_tools({"route": route}, set())
    assert len(results) == 1
    assert executed == ["public_sentiment_ashare_master_sentiment_get"]


@pytest.mark.asyncio
async def test_budget_defaults_to_all_calls():
    """route 未给 budget → 默认全部执行（D4 的 None 分支）。"""
    node, executed = _make_budget_node()
    route = [{"analyst": "nonexistent", "tool_calls": _ROUTE_TOOLS}]
    results = await node._execute_tools({"route": route}, set())
    assert len(results) == 3
    assert len(executed) == 3


@pytest.mark.asyncio
async def test_budget_zero_means_execute_nothing():
    """D4：显式 budget=0 表示"不执行"——旧实现 `int(x or len(...))` 把 0 吞成全部执行
    （探针实测：budget=0、2 个调用执行了 2 个）。"""
    node, executed = _make_budget_node()
    route = [{"analyst": "nonexistent", "budget": 0, "tool_calls": _ROUTE_TOOLS}]
    results = await node._execute_tools({"route": route}, set())
    assert results == []
    assert executed == []


@pytest.mark.asyncio
async def test_max_tool_calls_caps_budget():
    """D4：config.max_tool_calls 成为真实上限（此前只有 /health 一个消费点），
    与 route budget 取 min（探针实测：max_tool_calls=2、3 个不同工具执行了 3 个）。"""
    node, executed = _make_budget_node(max_tool_calls=2)
    route = [{"analyst": "nonexistent", "budget": 10, "tool_calls": _ROUTE_TOOLS}]
    results = await node._execute_tools({"route": route}, set())
    assert len(results) == 2
    assert len(executed) == 2


@pytest.mark.asyncio
async def test_skipped_calls_do_not_consume_budget():
    """D4 语义①：被跳过的调用（unknown tool_key）不消耗预算。

    旧实现在循环顶部 `budget -= 1`：budget=3、[未知,未知,有效,有效] 只执行 1 个；
    新语义执行 2 个。"""
    node, executed = _make_budget_node()
    route = [
        {
            "analyst": "nonexistent",
            "budget": 2,
            "tool_calls": [
                {"tool_key": "no_such_tool_a", "arguments": {}},
                {"tool_key": "no_such_tool_b", "arguments": {}},
                {"tool_key": "sentiment", "arguments": {}},
                {"tool_key": "limit_up_count", "arguments": {}},
            ],
        }
    ]
    results = await node._execute_tools({"route": route}, set())
    assert [r.tool for r in results] == [
        "public_sentiment_ashare_master_sentiment_get",
        "public_limit_up_count_ashare_master_limit_up_count_get",
    ]
    assert len(executed) == 2


@pytest.mark.asyncio
async def test_duplicate_calls_do_not_consume_budget():
    """D4 语义①：T27 的重复签名跳过同样不消耗预算。

    budget=2、[有效, 重复, 有效] → 新语义执行 2 个；
    旧实现在顶部扣减：idx0 扣到 1、idx1 重复跳过但已扣到 0、idx2 break → 只执行 1 个。"""
    node, executed = _make_budget_node()
    route = [
        {
            "analyst": "nonexistent",
            "budget": 2,
            "tool_calls": [
                {"tool_key": "sentiment", "arguments": {}},
                {"tool_key": "sentiment", "arguments": {}},  # 同签名重复
                {"tool_key": "limit_up_count", "arguments": {}},
            ],
        }
    ]
    results = await node._execute_tools({"route": route}, set())
    assert len(results) == 2
    assert len(executed) == 2


# ------------------------------------------------------------------ #
# 异常处理                                                             #
# ------------------------------------------------------------------ #


class BrokenNode(MarketAnalystNode):
    """故意抛出异常的 analyst 子类。"""

    category = "technical"

    async def _execute_tools(self, state, called_signatures):
        raise RuntimeError("intentional failure")


@pytest.mark.asyncio
async def test_analyst_exception_degrades_to_error():
    """执行异常不应炸图，应降级为 failed=True finding + errors 记录。"""
    settings = Settings()
    node = BrokenNode(settings)
    result = await node({"question": "测试"})

    assert len(result["findings"]) == 1
    assert result["findings"][0]["failed"] is True
    assert len(result["errors"]) == 1
    assert "technical analysis failed" in result["errors"][0].lower()
    assert "intentional failure" in result["errors"][0]


# ------------------------------------------------------------------ #
# Fixture 初始化                                                       #
# ------------------------------------------------------------------ #


@pytest.fixture()
def node(monkeypatch):
    """带 mock runtime 的 market analyst（使用 nonexistent category → 无工具）。"""
    monkeypatch.setattr("app.graph.nodes.analysts.base.ToolRuntime.execute", lambda *a, **k: [])
    monkeypatch.setattr("app.graph.nodes.analysts.base.ToolRuntime.truncate", lambda self, r: None)

    settings = Settings()
    n = object.__new__(MarketAnalystNode)
    n.settings = settings
    n.category = "nonexistent"
    n._runtime = _NoGatewayRuntime()
    return n


# ------------------------------------------------------------------ #
# 默认符号注入 / 白名单跳过逻辑                                        #
# ------------------------------------------------------------------ #


class _TestAnalyst(MarketAnalystNode):
    """用于测试 _build_arguments / WHITELIST_NO_SYMBOL 的轻量子类。"""

    category = "technical"


def test_build_arguments_injects_symbol_when_stocks_provided():
    """有 stock codes 时，非白名单工具应获得 symbol 参数。"""
    import types

    settings = Settings()
    node = _TestAnalyst(settings)
    meta = types.SimpleNamespace(tool_name="detail_eastmoney_detail_get", key="detail")
    args = node._build_arguments(meta, "a_share", ["600519"])
    assert args == {"symbol": "600519"}


def test_build_arguments_empty_without_stocks():
    """无 stock codes 时不应猜测 symbol —— 由 execute 层跳过。"""
    import types

    settings = Settings()
    node = _TestAnalyst(settings)
    meta = types.SimpleNamespace(tool_name="detail_eastmoney_detail_get", key="detail")
    args = node._build_arguments(meta, "a_share", None)
    assert args == {}


def test_build_arguments_no_symbol_for_whitelist_tools():
    """白名单工具即使在有 stocks 的情况下也不应携带 symbol 参数。"""
    import types

    settings = Settings()
    node = _TestAnalyst(settings)
    meta = types.SimpleNamespace(tool_name="public_limit_up_pool_ashare_master_limit_up_pool_get", key="limit_up_pool")
    args = node._build_arguments(meta, "a_share", ["600519"])
    assert args == {}


async def test_execute_skips_non_whitelist_no_stock(mock_runtime, monkeypatch):
    """白名单外的工具 + 无 stock codes → 应被跳过不执行。"""
    import types

    from app.config import Settings

    settings = Settings()
    node = _TestAnalyst(settings)
    executed_tools: list[str] = []

    # Mock at instance level — __init__ already set self._runtime = ToolRuntime(settings),
    # so we replace the instance attribute rather than trying to patch the global class.
    class FakeRuntime(_NoGatewayRuntime):
        async def execute(self, tool_name, arguments, sig, deadline=None):
            executed_tools.append(tool_name)
            return types.SimpleNamespace(
                tool=tool_name,
                arguments=arguments,
                status="success",
                normalized=[],
                error=None,
                _cache_info=None,
                raw=None,
                partial=False,
            )

        @staticmethod
        def truncate(r):
            pass

    node._runtime = FakeRuntime()

    # route 分配 technical 类工具：sentiment / limit_up_count（白名单）+ detail / quote（非白名单）
    state = {
        "question": "今天A股发生了什么？",
        "domain": "a_share",
        "route": [
            {
                "analyst": "technical",
                "tool_calls": [
                    {"tool_key": "sentiment", "arguments": {}, "purpose": "情绪"},
                    {"tool_key": "limit_up_count", "arguments": {}, "purpose": "涨停数"},
                    {"tool_key": "detail", "arguments": {}, "purpose": "详情"},
                    {"tool_key": "quote", "arguments": {}, "purpose": "行情"},
                ],
                "budget": 4,
            }
        ],
    }
    result = await node(state)

    assert isinstance(result["results"], list)
    executed_set = set(executed_tools)
    # sentiment / limit_up 系列是白名单工具 → 应被执行（不依赖 symbol）
    assert any("sentiment" in t for t in executed_tools), f"预期执行 sentiment，但执行了: {executed_tools}"
    assert any("limit_up_count" in t for t in executed_tools), f"预期执行 limit_up_count，但执行了: {executed_tools}"
    # detail / quote / overview 不是白名单，且问题中无股票代码 → 应被跳过
    assert "detail_eastmoney_detail_get" not in executed_set, f"预期跳过 detail，但执行了: {executed_tools}"
    assert "overview_eastmoney_overview_get" not in executed_set, (
        f"预期跳过 overview（数据量过大），但执行了: {executed_tools}"
    )
    assert "quote_tencent_quote_get" not in executed_set, f"预期跳过 quote，但执行了: {executed_tools}"
    # T18T：显式断言无错误 —— 将来任何"被兜底 except 吞掉"的回归都会以此变红，
    # 而不是变成难懂的 KeyError: 'results'
    assert result.get("errors") == []


def test_domain_default_symbol_per_category():
    """Stock ID 提取：问题中包含 6 位数字时应匹配。"""
    settings = Settings()
    node = _TestAnalyst(settings)
    # 问题中无股票代码
    result = node._extract_stocks({"question": "今天A股发生了什么？"})
    assert result == []
    # 问题中有代码
    result = node._extract_stocks({"question": "贵州茅台600519怎么样"})
    assert "600519" in result
    # 多个代码
    result = node._extract_stocks({"question": "查看600519和000001"})
    assert "600519" in result and "000001" in result


def test_whitelist_is_frozenset():
    """WHITELIST_NO_SYMBOL 应为 frozenset（不可变，查找 O(1)）。"""
    assert isinstance(MarketAnalystNode.WHITELIST_NO_SYMBOL, frozenset)
    # sentiment / limit_up_pool (aggregate) 应该在白名单里
    assert "public_sentiment_ashare_master_sentiment_get" in MarketAnalystNode.WHITELIST_NO_SYMBOL
    assert "public_limit_up_pool_ashare_master_limit_up_pool_get" in MarketAnalystNode.WHITELIST_NO_SYMBOL
    # detail (company-specific) 不应该在白名单里
    assert "detail_eastmoney_detail_get" not in MarketAnalystNode.WHITELIST_NO_SYMBOL
    # overview (全市场数据 ~749KB) 也不应在白名单 —— 会触发网关限流
    assert "overview_eastmoney_overview_get" not in MarketAnalystNode.WHITELIST_NO_SYMBOL


# ------------------------------------------------------------------ #
# Critic 节点 — dict state 兼容性                                       #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_critic_dict_state_returns_pass(monkeypatch):
    """Critic 接收 dict state 时不应因属性访问炸掉，应正常返回 LLM verdict。

    修复前：state.model_dump() 后 state.domain / state.question 属性访问
    抛 AttributeError → 恒走 except → 恒返回 research_more。
    """
    import json as _json

    from app.graph.nodes.critic import CriticNode

    # 最小 report，满足 _format_report_for_review 的 getattr 访问
    fake_report = types.SimpleNamespace(
        what_happened="测试报告内容",
        confidence=0.8,
        state_label="neutral",
        strong_areas=[],
        risks=[],
        why=[],
    )

    # 用替换 __init__ 的方式注入 mock client
    def mock_init(self, settings):
        self.settings = settings

        class _FakeMessage:
            content = _json.dumps({"verdict": "pass", "reason": "ok"})

        class _FakeChoice:
            message = _FakeMessage()

        class _FakeResponse:
            choices = [_FakeChoice()]

        class _FakeCompletions:
            async def create(self, **kwargs):
                return _FakeResponse()

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeClient:
            chat = _FakeChat()

        self.client = _FakeClient()

    monkeypatch.setattr(CriticNode, "__init__", mock_init)

    settings = Settings()
    critic = CriticNode(settings)

    # dict state —— 修复前会因 state.domain / state.question 抛 AttributeError
    state = {
        "report": fake_report,
        "results": [],
        "evidence": [],
        "gate": None,
        "question": "测试问题",
        "domain": "a_share",
    }

    result = await critic(state)
    critique = result["critique"]
    assert critique.verdict == "pass"
    assert critique.reason == "ok"

    # 恢复 __init__（monkeypatch 会自动恢复，这里显式确认）
    assert CriticNode.__init__ is mock_init


@pytest.mark.asyncio
async def test_critic_sdk_request_has_system_role():
    """走 AsyncOpenAI + httpx 的真实序列化路径，拦截实际 HTTP 请求体。"""
    import json

    import httpx
    from openai import AsyncOpenAI

    from app.graph.nodes.critic import CriticNode

    captured_body: dict = {}

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_body["json"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"verdict": "pass", "reason": "ok"}),
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    fake_report = types.SimpleNamespace(
        what_happened="测试报告内容",
        confidence=0.8,
        state_label="neutral",
        strong_areas=[],
        risks=[],
        why=[],
    )
    state = {
        "report": fake_report,
        "results": [],
        "evidence": [],
        "gate": None,
        "question": "测试问题",
        "domain": "a_share",
    }

    settings = Settings(openai_api_key="test-key", openai_base_url="http://mosaic.test/v1")
    critic = CriticNode(settings)

    async with httpx.AsyncClient(transport=httpx.MockTransport(mock_handler)) as http_client:
        critic.client = AsyncOpenAI(
            api_key="test-key",
            base_url="http://mosaic.test/v1",
            http_client=http_client,
        )
        result = await critic(state)

    messages = captured_body["json"]["messages"]
    assert messages[0] == {
        "role": "system",
        "content": "你是 Mosaic 的 Critic，只返回合法 JSON。",
    }
    assert messages[1]["role"] == "user"
    assert result["critique"].verdict == "pass"
    assert result["critique"].reason == "ok"


@pytest.mark.asyncio
async def test_critic_invalid_json_fallback_records_error():
    """Critic 输出无法解析时，fallback 必须显式写入 errors。"""
    import httpx
    from openai import AsyncOpenAI

    from app.graph.nodes.critic import CriticNode

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "not json"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    fake_report = types.SimpleNamespace(
        what_happened="测试报告内容",
        confidence=0.8,
        state_label="neutral",
        strong_areas=[],
        risks=[],
        why=[],
    )
    state = {
        "report": fake_report,
        "results": [],
        "evidence": [],
        "gate": None,
        "question": "测试问题",
        "domain": "a_share",
    }

    settings = Settings(openai_api_key="test-key", openai_base_url="http://mosaic.test/v1")
    critic = CriticNode(settings)

    async with httpx.AsyncClient(transport=httpx.MockTransport(mock_handler)) as http_client:
        critic.client = AsyncOpenAI(
            api_key="test-key",
            base_url="http://mosaic.test/v1",
            http_client=http_client,
        )
        result = await critic(state)

    # T11（有意反转）：审计自身失败改为内部 error 安全终止，不再 research_more 重跑。
    assert result["critique"].verdict == "error"
    assert "Critic audit failed" in result["critique"].reason
    assert result["errors"]
    assert "Critic audit failed" in result["errors"][0]


# ------------------------------------------------------------------ #
# P2.5-5：Evidence 条目对齐 §1 约定                                      #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_analyst_evidence_is_evidence_instances(monkeypatch):
    """analyst 输出的 evidence 应为 Evidence 实例，含 source_tool 语义。"""
    from app.graph.nodes.analysts.technical import TechnicalAnalystNode
    from app.models.evidence import Evidence
    from app.models.market import NormalizedDatum, ToolResult

    settings = Settings()
    node = TechnicalAnalystNode(settings)

    async def fake_execute(self, tool_name, arguments, called_signatures, deadline=None):
        return ToolResult(
            tool=tool_name,
            arguments=arguments,
            status="success",
            normalized=[
                NormalizedDatum(
                    tool=tool_name,
                    metric="price",
                    value=3800.5,
                    domain="a_share",
                    timestamp="2026-10-02T10:00:00Z",
                    source="tencent",
                ),
            ],
            error=None,
        )

    monkeypatch.setattr("app.graph.tool_runtime.ToolRuntime.execute", fake_execute)
    monkeypatch.setattr("app.graph.tool_runtime.ToolRuntime.truncate", lambda self, r: None)

    state = {
        "question": "测试",
        "domain": "a_share",
        "route": [
            {
                "analyst": "technical",
                "tool_calls": [
                    {"tool_key": "sentiment", "arguments": {}, "purpose": "情绪"},
                ],
                "budget": 1,
            }
        ],
    }

    result = await node(state)
    evidence = result.get("evidence", [])

    assert len(evidence) > 0, "evidence 不应为空"
    for e in evidence:
        assert isinstance(e, Evidence), f"evidence 元素应为 Evidence 实例，实际 {type(e)}"
        assert e.source_tool == "public_sentiment_ashare_master_sentiment_get"


# ------------------------------------------------------------------ #
# Reasoning 节点 — reducer 序列化后 evidence 为 dict 的兼容性           #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_reasoning_node_accepts_dict_evidence(monkeypatch):
    """多 analyst 并行后 evidence 经 reducer/model_dump 变成 dict，
    ReasoningNode 应转回 Evidence 对象再调用 reason()，不应炸。

    修复前：reasoning.py 对 evidence 逐条 .model_dump()，dict 没有该方法
    → AttributeError → report=None → ResearchResponse 验证失败。
    """
    import json as _json

    from app.graph.nodes.reasoning import ReasoningNode

    report_json = _json.dumps(
        {
            "title": "t",
            "market_state": "neutral",
            "state_label": "窄幅震荡",
            "what_happened": "测试",
            "why": [],
            "strong_areas": [],
            "what_changed": [],
            "what_matters": [],
            "risks": [],
            "data_caveats": [],
            "confidence": "low",
            "used_tools": [],
            "evidence": [],
        }
    )

    # 用替换 __init__ 的方式注入 mock client（绕过真实 OpenAI 连接）
    def mock_init(self, settings, **kwargs):
        self.settings = settings
        self.critic_max_revisions = 2
        from app.research.reasoning import ReasoningEngine

        self._engine = object.__new__(ReasoningEngine)
        self._engine.settings = settings

        class _FakeMessage:
            content = report_json

        class _FakeChoice:
            message = _FakeMessage()

        class _FakeResp:
            choices = [_FakeChoice()]

        class _FakeCompletions:
            async def create(self, **kw):
                return _FakeResp()

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeClient:
            chat = _FakeChat()

        self._engine.client = _FakeClient()

    monkeypatch.setattr(ReasoningNode, "__init__", mock_init)

    node = ReasoningNode(Settings())

    # dict state，evidence 是 list[dict]（模拟 reducer 序列化后的形态）
    dict_evidence = [
        {
            "id": "e1",
            "source_tool": "quote_tencent_quote_get",
            "domain": "a_share",
            "metric": "price",
            "value": 3800,
            "status": "success",
        },
        {
            "id": "e2",
            "source_tool": "public_sentiment_ashare_master_sentiment_get",
            "domain": "a_share",
            "metric": "risk_on",
            "value": 0.3,
            "status": "success",
        },
    ]
    state = {
        "question": "今天A股发生了什么？",
        "domain": "a_share",
        "results": [],
        "evidence": dict_evidence,
        "findings": [],
    }

    result = await node(state)
    assert result.get("report") is not None, "dict evidence 不应导致 report=None"
    assert not result.get("errors"), f"不应有错误: {result.get('errors')}"
