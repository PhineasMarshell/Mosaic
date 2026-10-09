"""阶段 3：A 股市场级最低计划前缀、ToolMeta 的 symbol 声明、市场级 Normalizer 合约。

问题（方案 §2 根因 / §4 阶段 3）：不含个股代码的"今天 A 股发生了什么"这类
市场级问题，planner 会计划必须有 symbol 的 ``overview``（get_company_overview），
执行层静默跳过 → 报告在**零市场级数据**下撰写（"假覆盖"）。本文件锁住三层防线：

1. 市场级最低证据集由**代码**注入并置于计划最前面，不由 LLM 决定；
2. "需要 symbol 却拿不到 symbol"的 step 被如实剔除并记进 run log（不再静默跳过）；
3. 市场级 datum 缺时间戳时必须带出明确 note（报告不得据此断言"今日"）。

本文件 mock 了什么
-----------------
- **registry 文本**：前缀用例直接传 ``registry_text()`` 的真实结果，或在其中
  删掉一行来模拟"该 key 不在本次域过滤后的注册表里"；前缀可见性是 prompt 的
  输入而非被测逻辑，故不 mock 成注册表对象。
- **LLM**：只有 run log 集成用例把 ``SupervisorNode.client`` 换成返回固定 JSON 的
  fake（节点 ``__call__`` 骨架真实运行、prompt 真实组装），其余用例直接调用
  ``apply_market_summary_prefix``，完全不经过 LLM。
- **上游 payload**：Normalizer 用例直接喂 dict，不发起 gateway 调用。

因此**没有覆盖**
----------------
- 真实 LLM 是否遵守"最低证据集已由系统补入、不要重复规划"的提示词约定；
- 真实 Market Gateway 的返回结构（只按当前 Normalizer 合约断言）；
- 图级执行路径（市场级问题走真实图见 ``tests/test_graph_e2e.py``）。

T35 约定：本文件不整体替换任何被测节点的 ``__call__``。
"""

from __future__ import annotations

import json
import logging
import types

import pytest

# app.main 在模块导入期加载：它的 setup_logging 会安装自己的 handler，
# 用例里再首次导入会让 caplog 抓不到 app.graph.run_log 的结构化日志。
import app.main as main  # noqa: F401
from app.config import Settings
from app.gateway.normalizer import (
    _EMPTY_PAYLOAD_NOTE,
    _MARKET_LEVEL_NO_TIMESTAMP_NOTE,
    normalize_tool_result,
)
from app.gateway.tool_registry import registry_text, resolve_tool
from app.graph.market_plan import (
    A_SHARE_BENCHMARK_SYMBOL,
    MARKET_SUMMARY_MINIMUM,
    apply_market_summary_prefix,
    is_market_level_question,
    unexecutable_steps,
)
from app.graph.nodes.supervisor import SupervisorNode
from app.models.research import ResearchIntent, ResearchPlan, ToolCallPlan

_REGISTRY = registry_text()
_MIN_KEYS = [key for key, _arguments, _purpose in MARKET_SUMMARY_MINIMUM]
_MARKET_QUESTION = "今天A股发生了什么？"


def _plan(*steps: tuple[str, dict, str], domain: str = "a_share", task: str = "market_summary") -> ResearchPlan:
    """构造 plan：``steps`` 为 (tool_key, arguments, purpose) 三元组。"""
    return ResearchPlan(
        intent=ResearchIntent(domain=domain, task=task, question=_MARKET_QUESTION),
        steps=[ToolCallPlan(tool_key=key, arguments=args, purpose=purpose) for key, args, purpose in steps],
    )


def _keys(plan: ResearchPlan) -> list[str]:
    return [step.tool_key for step in plan.steps]


def _registry_without(tool_key: str) -> str:
    return "\n".join(line for line in _REGISTRY.splitlines() if not line.startswith(f"- {tool_key}:"))


# ------------------------------------------------------------------ #
# 注入判定：什么时候加前缀、什么时候不加                                    #
# ------------------------------------------------------------------ #


def test_market_level_question_is_recognised_by_three_conditions():
    """无 6 位代码 + a_share + 市场级 task → 注入。"""
    assert is_market_level_question(ResearchIntent(task="market_summary"), _MARKET_QUESTION)
    assert is_market_level_question(ResearchIntent(task="market_diagnosis"), "大盘怎么样")
    # 任一条件不满足都不注入
    assert not is_market_level_question(ResearchIntent(task="market_summary"), "600519 今天怎么样？")
    assert not is_market_level_question(ResearchIntent(task="company_research"), _MARKET_QUESTION)
    assert not is_market_level_question(ResearchIntent(domain="crypto", task="market_summary"), _MARKET_QUESTION)


def test_market_question_injects_minimum_evidence_set_before_llm_steps():
    """最低证据集置于最前；LLM 的额外 step 排在它后面（预算耗尽前先满足最低集）。"""
    plan = _plan(("detail", {"symbol": "600519"}, "看个股"))

    prefix, dropped = apply_market_summary_prefix(
        plan, question=_MARKET_QUESTION, registry=_REGISTRY, max_steps=8
    )

    assert prefix == _MIN_KEYS
    assert dropped == []
    assert _keys(plan)[: len(_MIN_KEYS)] == _MIN_KEYS
    assert _keys(plan)[len(_MIN_KEYS) :] == ["detail"]
    # 大盘基准由代码给出 canonical 参数（沪深300），不许 LLM 猜口径
    assert plan.steps[0].arguments == {"symbol": A_SHARE_BENCHMARK_SYMBOL}
    assert plan.steps[0].priority == "high"
    # 涨停池明细刻意不在最低集里：只在需要点名个股/板块密度时才计划
    assert "limit_up_pool" not in prefix


def test_planner_quote_step_is_replaced_by_the_code_owned_benchmark_symbol():
    """planner 自己也planned quote（可能给个股代码）时，被替换为大盘基准且不重复。"""
    plan = _plan(("quote", {"symbol": "SH600519"}, "个股口径"))

    apply_market_summary_prefix(plan, question=_MARKET_QUESTION, registry=_REGISTRY, max_steps=8)

    quotes = [step for step in plan.steps if step.tool_key == "quote"]
    assert len(quotes) == 1
    assert quotes[0].arguments == {"symbol": A_SHARE_BENCHMARK_SYMBOL}


@pytest.mark.parametrize(
    ("question", "domain", "task"),
    [
        ("600519 今天怎么样？", "a_share", "market_summary"),  # 点名个股 → 不塞市场级工具抢预算
        ("大盘怎么样", "crypto", "market_summary"),  # 最低集是 A 股口径
        (_MARKET_QUESTION, "a_share", "company_research"),  # 个股研究用不上市场级聚合
    ],
)
def test_no_prefix_for_non_market_questions(question, domain, task):
    """三种不该注入的场景：计划必须原样不动。"""
    plan = _plan(("longhu", {}, "看资金"), domain=domain, task=task)

    prefix, dropped = apply_market_summary_prefix(plan, question=question, registry=_REGISTRY, max_steps=8)

    assert prefix == []
    assert dropped == []
    assert _keys(plan) == ["longhu"]


# ------------------------------------------------------------------ #
# 剔除"计划了必跳过"的 step                                                 #
# ------------------------------------------------------------------ #


def test_steps_needing_a_symbol_are_dropped_instead_of_silently_skipped():
    """需要 symbol 且拿不到 symbol 的 step 从计划里剔除，并如实返回（供 run log 记账）。"""
    plan = _plan(("overview", {}, "大盘概览（必须有 symbol）"), ("detail", {}, "个股详情"), ("longhu", {}, "看资金"))

    prefix, dropped = apply_market_summary_prefix(
        plan, question=_MARKET_QUESTION, registry=_REGISTRY, max_steps=8
    )

    assert dropped == ["overview", "detail"]
    assert "overview" not in _keys(plan)
    assert "detail" not in _keys(plan)
    # longhu 声明的就是"无 symbol 可执行"，保留；前缀 key 由代码补入
    assert _keys(plan) == _MIN_KEYS + ["longhu"]


def test_unexecutable_steps_matches_the_analyst_symbol_guard():
    """判定与 analysts/base.py 的符号守卫同源：requires_symbol 声明 + 无 symbol 参数。"""
    plan = _plan(("overview", {}, "无 symbol"), ("quote", {"symbol": "000300"}, "有 symbol"))

    assert unexecutable_steps(plan, _MARKET_QUESTION) == ["overview"]
    # 问题里带 6 位代码时，analyst 会从问题里抽出 symbol → 不再算"必然跳过"
    assert unexecutable_steps(plan, "600519 怎么样？") == []


def test_steps_are_kept_when_the_question_itself_carries_a_code():
    """问题里有 6 位代码时不注入前缀，因此也不做剔除（个股路径零影响）。"""
    plan = _plan(("overview", {}, "个股概览"))

    prefix, dropped = apply_market_summary_prefix(
        plan, question="600519 今天怎么样？", registry=_REGISTRY, max_steps=8
    )

    assert prefix == []
    assert dropped == []
    assert _keys(plan) == ["overview"]


# ------------------------------------------------------------------ #
# 可见性与预算                                                           #
# ------------------------------------------------------------------ #


def test_prefix_key_invisible_in_registry_is_skipped():
    """不在本次域过滤后的注册表文本里 = 不该计划（与 supervisor 域守卫同一约定）。"""
    plan = _plan()

    prefix, _dropped = apply_market_summary_prefix(
        plan, question=_MARKET_QUESTION, registry=_registry_without("telegraph"), max_steps=8
    )

    assert prefix == [key for key in _MIN_KEYS if key != "telegraph"]
    assert "telegraph" not in _keys(plan)


def test_prefix_survives_max_steps_truncation():
    """预算不足时挤掉的是 LLM 的额外 step，最低证据集必须留下。"""
    plan = _plan(("news_digest", {}, "消息面"), ("limit_up_pool", {}, "涨停池明细"))

    prefix, _dropped = apply_market_summary_prefix(
        plan, question=_MARKET_QUESTION, registry=_REGISTRY, max_steps=len(_MIN_KEYS) + 1
    )

    assert prefix == _MIN_KEYS
    assert _keys(plan) == _MIN_KEYS + ["news_digest"]


# ------------------------------------------------------------------ #
# ToolMeta：唯一的 symbol 声明点                                          #
# ------------------------------------------------------------------ #


@pytest.mark.parametrize("tool_key", ["sentiment", "limit_up_count", "limit_up_sectors", "telegraph"])
def test_market_level_tools_declare_requires_symbol_false_and_market_level(tool_key):
    meta = resolve_tool(tool_key)
    assert meta.requires_symbol is False
    assert meta.market_level is True


def test_symbol_metadata_separates_needs_symbol_from_market_level():
    """两件不同的事：是否需要 symbol ≠ 数据是否整市场口径。"""
    assert resolve_tool("overview").requires_symbol is True
    assert resolve_tool("overview").market_level is False

    news = resolve_tool("news_search")
    assert news.requires_symbol is False
    assert news.market_level is False  # 无需 symbol，但不是"整市场横截面"数据

    assert resolve_tool("quote").requires_symbol is True

    dumped = resolve_tool("sentiment").model_dump()
    assert dumped["requires_symbol"] is False
    assert dumped["market_level"] is True


def test_analyst_symbol_whitelist_is_derived_from_tool_meta():
    """旧的白名单是派生别名（兼容期），不再是第二份真相。"""
    from app.graph.nodes.analysts.base import MarketAnalystNode

    whitelist = MarketAnalystNode.WHITELIST_NO_SYMBOL
    assert isinstance(whitelist, frozenset)
    assert whitelist == frozenset(
        meta.tool_name for meta in __import__("app.gateway.tool_registry", fromlist=["ALL_TOOLS"]).ALL_TOOLS
        if not meta.requires_symbol
    )
    assert "get_ashare_sentiment" in whitelist
    # 需要 symbol 的大盘概览/个股详情绝不能混进来
    assert "get_company_overview" not in whitelist
    assert "get_company_detail" not in whitelist


# ------------------------------------------------------------------ #
# Normalizer：市场级 datum 的日期锚点与空返回                              #
# ------------------------------------------------------------------ #


def test_market_level_datum_without_timestamp_carries_an_explicit_note():
    result = normalize_tool_result("get_ashare_sentiment", {}, {"up_count": 3000, "down_count": 2000})

    assert result.status == "success"
    assert result.normalized
    assert all(_MARKET_LEVEL_NO_TIMESTAMP_NOTE in (datum.note or "") for datum in result.normalized)


def test_market_level_datum_with_timestamp_is_not_flagged():
    result = normalize_tool_result(
        "get_ashare_sentiment", {}, {"up_count": 3000, "timestamp": "2026-10-09 15:00:00"}
    )

    assert result.status == "success"
    assert all(not (datum.note or "") for datum in result.normalized)


def test_non_market_level_tool_is_not_flagged():
    """个股行情没有时间戳时不加市场级 note（避免把提示词稀释成噪音）。"""
    result = normalize_tool_result("get_market_quotes", {"symbol": "600519"}, {"price": 12.5, "volume": 100})

    assert result.status == "success"
    assert all(not (datum.note or "") for datum in result.normalized)


def test_empty_payload_gets_an_explicit_note():
    """空返回必须给出可引用的原因，不能只是"没有数据"。"""
    result = normalize_tool_result("get_ashare_sentiment", {}, {})

    assert result.status == "error"
    assert result.normalized == []
    assert result.note == _EMPTY_PAYLOAD_NOTE


def test_zero_extractable_datum_gets_an_explicit_note():
    """能解析但没有可提取数据（整块只含元数据键）时同样要有明确 note。"""
    result = normalize_tool_result("get_ashare_sentiment", {}, {"partial": False, "status": "ok"})

    assert result.status == "error"
    assert result.note == _EMPTY_PAYLOAD_NOTE
    assert result.error is not None


def test_upstream_note_is_not_overwritten_by_the_empty_payload_note():
    """上游已经说明原因时保留上游说法（更具体的信息优先）。"""
    result = normalize_tool_result("get_ashare_sentiment", {}, {"note": "upstream has nothing"})

    assert result.status == "error"
    assert result.note == "upstream has nothing"


# ------------------------------------------------------------------ #
# run log：计划与实际执行的差异永远可解释                                    #
# ------------------------------------------------------------------ #


class _FakeOpenAI:
    """返回预设 plan JSON 的 fake client（只为驱动 SupervisorNode 的真实骨架）。"""

    def __init__(self, return_text: str):
        self.return_text = return_text

    async def create(self, *args, **kwargs):
        message = types.SimpleNamespace(content=self.return_text)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


def _patch_client(node: SupervisorNode, fake: _FakeOpenAI) -> None:
    node.client = types.SimpleNamespace()
    node.client.chat = types.SimpleNamespace()
    node.client.chat.completions = types.SimpleNamespace()
    node.client.chat.completions.create = fake.create


@pytest.mark.asyncio
async def test_plan_event_records_prefix_keys_and_unexecutable_keys(caplog):
    """run log 的 plan 事件必须能解释"为什么计划里有这些 key、为什么少了那个 key"。"""
    plan_json = json.dumps(
        {
            "intent": {
                "domain": "a_share",
                "task": "market_summary",
                "time_scope": "today",
                "question": _MARKET_QUESTION,
            },
            "steps": [
                {"tool_key": "overview", "arguments": {}, "purpose": "大盘概览"},
                {"tool_key": "longhu", "arguments": {}, "purpose": "看资金"},
            ],
        }
    )
    node = SupervisorNode(Settings())
    _patch_client(node, _FakeOpenAI(plan_json))

    with caplog.at_level(logging.INFO, logger="app.graph.run_log"):
        result = await node({"question": _MARKET_QUESTION})

    events = [json.loads(record.getMessage()) for record in caplog.records if record.getMessage().startswith("{")]
    plan_events = [event for event in events if event.get("event") == "plan"]
    assert len(plan_events) == 1, f"应恰好一条 plan 事件，实际: {[e.get('event') for e in events]}"

    payload = plan_events[0]
    assert payload["prefix_keys"] == _MIN_KEYS
    assert payload["unexecutable_keys"] == ["overview"]
    # 路由里既没有必然跳过的 overview，也没有第二份 quote
    routed = [call["tool_key"] for assignment in result["route"] for call in assignment["tool_calls"]]
    assert "overview" not in routed
    assert routed.count("quote") == 1
    assert set(_MIN_KEYS) <= set(routed)
