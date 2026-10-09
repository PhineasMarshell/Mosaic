"""阶段 1 + 阶段 2 的验收用例（方案 §4）。

阶段 1（证据缺口优先的 Critic 契约）：
- 缺指数表现 / 缺涨停个股明细 / 缺板块量化比较 → 必须 research_more；
- "措辞夸大但现有数据可改写" → revise；
- 两类问题并存 → 必须先 research_more；
- 无法解析的 Critic 输出 → error，绝不伪造 pass；
- 动作与 verdict 冲突 → 记录结构化 error 并按更保守的一侧路由。

阶段 2（工具执行过 ≠ 缺口已被满足）：
- partial / 空结果 / 参数不覆盖 / operationId 复用的兄弟 key，都不算满足；
- 相��� tool key 的不同参数，只有覆盖 requirement 才算满足；
- get_company_overview 与 get_company_detail 的缺口不会因无关调用被静默丢弃。

本文件 mock 了什么
------------------
- **LLM**：``CriticNode.client`` 换成固定 Critique JSON 的 fake，节点 ``__call__``
  骨架真实运行（prompt 真实组装）。
- **工具 key / operationId**：用注册表探测式取得，不硬编码。

因此**没有覆盖**
----------------
- 真实 LLM 的审计判断质量、真实 gateway 数据、阶段 3 / 阶段 4 的能力。
"""

from __future__ import annotations

import json
import types

import pytest

from app.config import Settings
from app.gateway.tool_registry import BY_KEY, resolve_tool
from app.graph.gap_loop import (
    ExecutionCoverage,
    coverage_records,
    judge_coverage,
    render_registry_for_critic,
    sanitize_missing_tool_keys,
    satisfied_keys,
)
from app.graph.nodes.critic import AuditIssue, CriticNode, Critique, derive_verdict_from_issues, resolve_conflicts
from app.models.market import NormalizedDatum, ToolResult

DOMAIN = "a_share"

#: 需求侧常用 key（缺失时跳过，避免注册表演进后测试无声腐烂）
_OVERVIEW = "overview" if "overview" in BY_KEY else None
_DETAIL = "detail" if "detail" in BY_KEY else None


# ------------------------------------------------------------------ #
# fake client                                                          #
# ------------------------------------------------------------------ #


class _FakeCompletions:
    def __init__(self, content: str):
        self.content = content
        self.last_prompt: str | None = None

    async def create(self, **kwargs):
        messages = kwargs.get("messages") or []
        self.last_prompt = "\n".join(
            m.get("content", "") for m in messages if isinstance(m, dict) and m.get("role") == "user"
        )
        message = types.SimpleNamespace(content=self.content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


def _make_node(content: str) -> CriticNode:
    node = CriticNode.__new__(CriticNode)
    node.settings = Settings()
    completions = _FakeCompletions(content)
    node.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
    return node


def _state(results=None) -> dict:
    return {
        "report": types.SimpleNamespace(
            what_happened="今日市场走强，商业航天领涨",
            confidence="high",
            state_label="RISK_ON",
            strong_areas=["商业航天"],
            risks=[],
            why=[],
        ),
        "results": results or [],
        "evidence": [],
        "gate": None,
        "question": "今天 A 股发生了什么？",
        "domain": DOMAIN,
    }


def _result(tool_key: str, *, status="success", partial=False, count=3, arguments=None, tool_name=None) -> ToolResult:
    meta = resolve_tool(tool_key)
    return ToolResult(
        tool=tool_name or meta.tool_name,
        tool_key=tool_key,
        arguments=arguments or {},
        status=status,
        partial=partial,
        normalized=[NormalizedDatum(metric=f"m{i}", value=i, tool=meta.tool_name) for i in range(count)],
    )


# ------------------------------------------------------------------ #
# 阶段 1：缺口优先                                                     #
# ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "kind,claim,tool_key",
    [
        ("missing_evidence", "缺指数表现，无法判断大盘涨跌", _OVERVIEW),
        ("missing_evidence", "缺涨停个股明细，无法点名个股", "limit_up_pool"),
        ("missing_evidence", "缺板块量化比较，无法说谁最密集", "limit_up_sectors"),
    ],
)
async def test_evidence_gaps_route_to_research_more(kind, claim, tool_key):
    """方案 §4 阶段 1 验收：三类缺口都必须 research_more，不能只判 revise。"""
    if tool_key is None:
        pytest.skip("注册表里没有该 key")
    node = _make_node(
        json.dumps(
            {
                "issues": [
                    {
                        "kind": kind,
                        "claim": claim,
                        "severity": "high",
                        "action": "research_more",
                        "rationale": "需要新数据才能回答",
                        "required_tool_keys": [tool_key],
                        "required_coverage": {},
                    }
                ],
                "reason": "证据不足",
            }
        )
    )
    out = await node(_state())
    assert out["critique"].verdict == "research_more"
    assert out["critique"].missing_tool_keys == [tool_key]
    assert out["critique"].missing_points == [claim]
    assert out.get("errors", []) == []


async def test_overstated_wording_is_revise_not_research_more():
    """'措辞夸大但现有数据可改写' → revise，不烧一轮工具。"""
    node = _make_node(
        json.dumps(
            {
                "issues": [
                    {
                        "kind": "unsupported_claim",
                        "claim": "市场全面走强",
                        "severity": "medium",
                        "action": "remove_or_qualify",
                        "rationale": "改为『多数板块上涨』即可",
                    }
                ],
                "reason": "措辞超出证据",
            }
        )
    )
    out = await node(_state())
    critique = out["critique"]
    assert critique.verdict == "revise"
    assert critique.unsupported_claims == ["市场全面走强"]
    assert critique.missing_tool_keys == []


async def test_mixed_issue_types_prefer_research_more():
    """两类问题并存时必须先补研究（方案 §4 阶段 1 验收第 3 条）。"""
    node = _make_node(
        json.dumps(
            {
                "issues": [
                    {
                        "kind": "unsupported_claim",
                        "claim": "星测智测领涨",
                        "severity": "medium",
                        "action": "remove_or_qualify",
                        "rationale": "无证据，可直接删",
                    },
                    {
                        "kind": "missing_evidence",
                        "claim": "商业航天为涨停较集中题材",
                        "severity": "high",
                        "action": "research_more",
                        "rationale": "需个股明细",
                        "required_tool_keys": ["limit_up_pool"],
                    },
                ],
                "reason": "既缺数据又有夸大",
            }
        )
    )
    out = await node(_state())
    critique = out["critique"]
    assert critique.verdict == "research_more"
    assert critique.missing_tool_keys == ["limit_up_pool"]
    assert critique.unsupported_claims == ["星测智测领涨"]


async def test_invalid_entity_requires_new_data_is_research_more():
    """invalid_entity 需要工具核查时也走 research_more（不是靠改写蒙混）。"""
    node = _make_node(
        json.dumps(
            {
                "issues": [
                    {
                        "kind": "invalid_entity",
                        "claim": "行云科技当日涨停",
                        "severity": "high",
                        "action": "research_more",
                        "rationale": "该标的未出现在任何 evidence 里，需核对",
                        "required_tool_keys": ["limit_up_pool"],
                    }
                ],
                "reason": "实体存疑",
            }
        )
    )
    out = await node(_state())
    assert out["critique"].verdict == "research_more"


@pytest.mark.parametrize(
    "payload",
    [
        '{"issues": [{"kind": "unknown_kind", "action": "research_more"}]}',
        '{"issues": [{"kind": "missing_evidence", "action": "make_it_up"}]}',
        '{"issues": "not-a-list"}',
        '{"issues": [{"kind": "missing_evidence", "severity": "catastrophic"}]}',
        # ── P0 补修：非对象元素绝不能被静默过滤掉 ──
        # 旧实现 `if isinstance(item, dict)` 会把字符串项丢掉，于是
        # `{"issues": ["缺数据"]}` 退化成「issues 键存在 → 派生 pass」。
        '{"issues": ["缺数据"]}',
        '{"issues": [null]}',
        '{"issues": [null, {"kind": "missing_evidence", "action": "research_more"}]}',
        '{"issues": [123]}',
        '{"issues": [{"kind": "missing_evidence", "action": "research_more"}, "还缺指数"]}',
        '{"issues": [["nested"]]}',
    ],
)
async def test_unparseable_issue_payload_is_error_not_fake_pass(payload):
    """无法解析的 Critic 输出必须是 error，绝不伪造 pass（方案 §4 阶段 1 验收第 4 条）。

    P0 回归重点：数组里的**任何**非对象元素都必须让审计安全终止 —— 模型说了有缺口，
    系统不能因为"解析不出来"就把它判成通过。
    """
    node = _make_node(payload)
    out = await node(_state())
    assert out["critique"].verdict == "error"
    assert out.get("errors"), "审计自身失败必须写入 errors"
    assert "Critic 输出无法解析为合法结论" in out["errors"][0]


async def test_non_object_issue_items_are_never_silently_dropped():
    """即使模型同时给了 verdict=pass，非对象 issue 项也不许被吞掉。"""
    node = _make_node('{"verdict": "pass", "issues": ["缺指数数据"]}')
    out = await node(_state())
    assert out["critique"].verdict == "error"
    assert out.get("errors")
    # 错误信息里要能看出是第几项出的问题，便于排查
    assert "issues[0]" in out["errors"][0]


async def test_prompt_states_the_issue_contract():
    """prompt 必须写清 issues / action 语义 / '工具执行过 ≠ 证据充分'。"""
    node = _make_node(json.dumps({"issues": [], "reason": "ok"}))
    await node(_state())
    prompt = node.client.chat.completions.last_prompt
    assert "[审计契约]" in prompt
    assert "action=research_more" in prompt
    assert "required_tool_keys" in prompt
    assert "工具执行过" in prompt and "不等于" in prompt


# ------------------------------------------------------------------ #
# verdict 派生与冲突处理                                                 #
# ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "issues,expected",
    [
        ([], None),
        ([AuditIssue(kind="unsupported_claim", action="remove_or_qualify")], "revise"),
        ([AuditIssue(kind="format", action="repair_format")], "revise"),
        ([AuditIssue(kind="missing_evidence", action="research_more")], "research_more"),
        (
            [
                AuditIssue(kind="unsupported_claim", action="remove_or_qualify"),
                AuditIssue(kind="missing_evidence", action="research_more"),
            ],
            "research_more",
        ),
    ],
)
def test_derive_verdict_from_issues(issues, expected):
    assert derive_verdict_from_issues(issues) == expected


def test_legacy_fields_are_derived_from_issues():
    """迁移期：legacy 字段由 issues 派生，模型显式给的值不会被覆盖。"""
    critique = Critique(
        verdict="pass",
        missing_points=["模型自己写的缺口"],
        issues=[
            AuditIssue(kind="unsupported_claim", claim="A", action="remove_or_qualify"),
            AuditIssue(
                kind="missing_evidence", claim="B", action="research_more", required_tool_keys=["limit_up_pool"]
            ),
        ],
    )
    assert critique.unsupported_claims == ["A"]
    assert critique.missing_points == ["模型自己写的缺口", "B"]
    assert critique.missing_tool_keys == ["limit_up_pool"]


@pytest.mark.parametrize(
    "model_verdict,action,expected",
    [
        # pass / revise 撞上"必须补证据" → 往 research_more 收紧
        ("pass", "research_more", "research_more"),
        ("revise", "research_more", "research_more"),
        # 反方向：模型说 research_more、action 只要求改写 → **不得**降级成 revise
        ("research_more", "remove_or_qualify", "research_more"),
        ("research_more", "repair_format", "research_more"),
        # 同级无冲突
        ("revise", "remove_or_qualify", "revise"),
        ("research_more", "research_more", "research_more"),
    ],
)
def test_conflict_resolution_keeps_the_more_conservative_verdict(model_verdict, action, expected):
    """P1 回归：冲突必须**取两者中更保守的**，而不是"以 action 派生覆盖模型自报值"。

    旧实现无条件 ``critique.verdict = structured``，于是模型判 research_more、
    action 只要求改写时被降级成 revise —— 缺口被悄悄放过。
    """
    issue = {
        "missing_evidence": {"kind": "missing_evidence", "action": "research_more", "claim": "缺指数"},
        "research_more": {"kind": "missing_evidence", "action": "research_more", "claim": "缺指数"},
        "remove_or_qualify": {
            "kind": "unsupported_claim",
            "action": "remove_or_qualify",
            "claim": "措辞夸大",
        },
        "repair_format": {"kind": "format", "action": "repair_format", "claim": "字段缺失"},
    }[action]
    critique = Critique(verdict=model_verdict, reason="r", issues=[AuditIssue(**issue)])

    conflicts = resolve_conflicts(critique)

    assert critique.verdict == expected
    # 只要两侧不一致就要留下冲突记录（即便最终保留的是模型那一侧）
    action_verdict = {"remove_or_qualify": "revise", "repair_format": "revise", "research_more": "research_more"}[
        action
    ]
    if action_verdict == model_verdict:
        assert conflicts == [], "两侧一致时不应记录冲突"
    else:
        assert conflicts, "两侧不一致必须留下可追溯的冲突记录"
        assert conflicts[0]["model_verdict"] == model_verdict
        assert conflicts[0]["action_verdict"] == action_verdict
        assert conflicts[0]["resolved"] == expected, "日志里的 resolved 必须反映真实路由结果"


def test_more_conservative_ordering():
    """保守顺序 research_more > revise > pass（双向都要成立）。"""
    from app.graph.nodes.critic import more_conservative

    assert more_conservative("pass", "revise") == "revise"
    assert more_conservative("revise", "pass") == "revise"
    assert more_conservative("pass", "research_more") == "research_more"
    assert more_conservative("research_more", "pass") == "research_more"
    assert more_conservative("revise", "research_more") == "research_more"
    assert more_conservative("research_more", "revise") == "research_more"
    assert more_conservative("pass", "pass") == "pass"
    # error 比三者都保守：审计器失能时不存在可信结论
    assert more_conservative("pass", "error") == "error"
    assert more_conservative("error", "research_more") == "error"


def test_legacy_only_response_upgrades_to_research_more():
    """旧格式响应（只有 missing_points）也算缺口信号 → research_more。"""
    critique = Critique(verdict="revise", reason="r", missing_points=["缺指数"])
    conflicts = resolve_conflicts(critique)
    assert critique.verdict == "research_more"
    assert conflicts


# ------------------------------------------------------------------ #
# 阶段 2：覆盖度判定                                                     #
# ------------------------------------------------------------------ #


def _record(**kwargs) -> ExecutionCoverage:
    defaults = dict(
        tool_key="limit_up_pool",
        tool_name="list_limit_up_stocks",
        arguments={},
        status="success",
        partial=False,
        datum_count=30,
    )
    defaults.update(kwargs)
    return ExecutionCoverage(**defaults)


@pytest.mark.parametrize(
    "overrides,expected_reason",
    [
        ({"status": "error"}, "status_error"),
        ({"status": "partial", "partial": True}, "status_partial"),
        ({"datum_count": 0}, "no_data"),
        ({"datum_count": 5}, "insufficient_datum_count"),
        ({"tool_key": "limit_up_sectors"}, "tool_key_mismatch"),
        ({"tool_key": None}, "tool_key_mismatch"),
    ],
)
def test_unsatisfied_reasons(overrides, expected_reason):
    record = _record(**overrides)
    judged = judge_coverage(record, {"limit_up_pool"}, {"min_datum_count": 20})
    assert judged.satisfies is False
    assert judged.reason == expected_reason


def test_satisfied_coverage_is_marked():
    judged = judge_coverage(_record(), {"limit_up_pool"}, {"min_datum_count": 20})
    assert judged.satisfies is True
    assert judged.reason == "satisfied"
    assert judged.fulfills == {"limit_up_pool"}


def test_partial_may_be_reused_only_when_issue_allows_it():
    """issue 显式 allow_partial=true 时，partial 才可复用。"""
    judged = judge_coverage(_record(status="partial", partial=True), {"limit_up_pool"}, {"allow_partial": True})
    assert judged.satisfies is True


def test_same_tool_different_arguments_must_cover_requirement():
    """同工具不同参数：只有参数覆盖 requirement 才算满足（方案 §4 阶段 2 验收第 3 条）。"""
    required = {"limit_up_pool": {"arguments": {"date": "2026-10-08"}}}
    wrong_date = judge_coverage(_record(arguments={"date": "2026-10-07"}), set(required), required["limit_up_pool"])
    right_date = judge_coverage(_record(arguments={"date": "2026-10-08"}), set(required), required["limit_up_pool"])
    assert wrong_date.satisfies is False
    assert wrong_date.reason == "arguments_not_covering"
    assert right_date.satisfies is True


@pytest.mark.parametrize("missing_key", [_OVERVIEW, _DETAIL])
def test_unrelated_call_does_not_satisfy_other_key_gap(missing_key):
    """get_company_overview 与 get_company_detail 的缺口不会互相吞掉
    （方案 §4 阶段 2 验收第 4 条）。"""
    if missing_key is None:
        pytest.skip("注册表里没有该 key")
    other = _DETAIL if missing_key == _OVERVIEW else _OVERVIEW
    executed = [_result(other, count=5)]
    assert missing_key not in satisfied_keys(executed)

    decision = sanitize_missing_tool_keys(
        [missing_key], allowed={missing_key, other}, executed=satisfied_keys(executed)
    )
    assert decision[0] == [missing_key], "被无关调用遮蔽掉的缺口必须仍然可补"


def test_sibling_registry_keys_sharing_operation_id_do_not_substitute():
    """operationId 复用的兄弟 registry key 互不替代（方案 §4 阶段 2 验收第 2 条）。"""
    from app.gateway.tool_registry import SHARED_BY_NAME

    shared = [names for names in SHARED_BY_NAME.values() if len(names) >= 2]
    if not shared:
        pytest.skip("注册表里没有复用 operationId 的兄弟条目")
    siblings = shared[0]
    a, b = siblings[0].key, siblings[1].key
    assert resolve_tool(a).tool_name == resolve_tool(b).tool_name, "前提：两兄弟共用同一 operationId"

    executed = [_result(a, count=10)]
    satisfied = satisfied_keys(executed)
    assert a in satisfied
    assert b not in satisfied, "同 operationId 的兄弟 key 不许互相冒充已满足"


def test_results_without_tool_key_are_never_satisfied():
    """老结果没有 tool_key → 一律不满足（保守：宁可多补一次，不要假装已满足）。"""
    legacy = ToolResult(tool="list_limit_up_stocks", arguments={}, status="success", normalized=[])
    legacy.normalized = [NormalizedDatum(metric="m", value=1, tool="list_limit_up_stocks")]
    assert satisfied_keys([legacy]) == set()
    record = coverage_records([legacy])[0]
    assert judge_coverage(record, {"limit_up_pool"}).reason == "tool_key_mismatch"


def test_partial_tool_stays_visible_in_registry_for_critic():
    """partial 的工具必须继续出现在「可补充的工具」里。"""
    from app.gateway.tool_registry import registry_text

    registry = registry_text(domains=[DOMAIN, "cross"])
    partial = _result("limit_up_pool", status="partial", partial=True, count=3)
    text = render_registry_for_critic(registry, [partial])
    assert "- limit_up_pool:" in text
    assert "partial" in text.split("- limit_up_pool:")[1].splitlines()[0]


def test_low_coverage_success_is_annotated_not_hidden():
    """阶段 2 补修：**低覆盖度的 success 不能被隐藏**——隐藏会让 Critic 永远点不到名。

    真正过滤发生在落库前，且按每条 issue 的 required_coverage 精确判断
    （见 test_required_coverage_keeps_under_satisfied_gap_in_research_loop）。
    """
    from app.gateway.tool_registry import registry_text

    registry = registry_text(domains=[DOMAIN, "cross"])
    thin = _result("limit_up_pool", count=3)
    text = render_registry_for_critic(registry, [thin])

    line = next(ln for ln in text.splitlines() if ln.strip().startswith("- limit_up_pool:"))
    assert "现有数据 3 条" in line
    assert "仍可点名重拉" in line


def test_unexecuted_registry_alias_keeps_low_coverage_tools_visible():
    """旧名 `render_unexecuted_registry` 也必须保留低覆盖度工具（P1 补修第 3 条的验收点）。

    这个函数名暗示"只渲染没执行过的工具"，最容易退化成"已执行过就删掉"。
    一旦删掉，Critic 就永远看不见 `limit_up_pool`、也就永远点不到名，
    `required_coverage` 要求的"重拉补足"路径直接断掉。
    """
    from app.gateway.tool_registry import registry_text
    from app.graph.gap_loop import render_unexecuted_registry

    registry = registry_text(domains=[DOMAIN, "cross"])
    thin = _result("limit_up_pool", count=3)  # success 但只有 3 条，覆盖度不足
    text = render_unexecuted_registry(registry, [thin])

    assert "- limit_up_pool:" in text, "低覆盖度的 success 不得从可选工具清单里消失"
    line = next(ln for ln in text.splitlines() if ln.strip().startswith("- limit_up_pool:"))
    assert "现有数据 3 条" in line
    assert "可点名" in line


def test_repeat_call_and_evidence_sufficiency_are_separate_judgements():
    """'别再调一遍' 与 '证据够不够' 是两件事（方案 §2.2 / §4 阶段 2）。"""
    from app.graph.gap_loop import executed_index, is_repeat_step
    from app.models.research import ToolCallPlan

    partial = _result("limit_up_pool", status="partial", partial=True, count=0)
    # 覆盖度：不满足
    assert "limit_up_pool" not in satisfied_keys([partial])
    # 重复调用：同参数已经调过 → planner 不该再排一遍
    assert "list_limit_up_stocks" in executed_index([partial])
    assert is_repeat_step(ToolCallPlan(tool_key="limit_up_pool", arguments={}, purpose="p"), [partial])


# ------------------------------------------------------------------ #
# P1 补修：required_coverage 必须真正进入缺口满足判定                      #
# ------------------------------------------------------------------ #


def _research_more_critique(required_coverage: dict, tool_key: str = "limit_up_pool") -> Critique:
    return Critique(
        verdict="research_more",
        reason="覆盖不足",
        issues=[
            AuditIssue(
                kind="missing_evidence",
                claim="涨停个股明细不足",
                severity="high",
                action="research_more",
                required_tool_keys=[tool_key],
                required_coverage=required_coverage,
            )
        ],
    )


def test_coverage_requirements_are_merged_strictest_first():
    """同一 key 被多条 issue 要求时，按**最严**合并（不允许后一条把前一条放宽）。"""
    from app.graph.gap_loop import coverage_requirements

    critique = Critique(
        verdict="research_more",
        issues=[
            AuditIssue(
                kind="missing_evidence",
                action="research_more",
                required_tool_keys=["limit_up_pool"],
                required_coverage={"min_datum_count": 50, "allow_partial": True},
            ),
            AuditIssue(
                kind="missing_evidence",
                action="research_more",
                required_tool_keys=["limit_up_pool"],
                required_coverage={"min_datum_count": 20, "allow_partial": False},
            ),
        ],
    )
    requirements = coverage_requirements(critique)
    assert requirements["limit_up_pool"]["min_datum_count"] == 50, "条数要求必须取最大值"
    assert requirements["limit_up_pool"]["allow_partial"] is False, "只要有一条不允许就不允许"


def test_same_key_success_but_too_few_data_still_needs_research():
    """同 key success 但 datum 数不足 → 仍必须走补证据（不能被判成已满足）。"""
    from app.gateway.tool_registry import registry_text
    from app.graph.gap_loop import coverage_requirements, gap_tool_keys

    thin = _result("limit_up_pool", count=3)
    critique = _research_more_critique({"min_datum_count": 20})

    requirements = coverage_requirements(critique)
    assert requirements == {
        "limit_up_pool": {
            "min_datum_count": 20,
            "argument_variants": [{"arguments": {}, "min_datum_count": 20, "allow_partial": False}],
        }
    }
    assert "limit_up_pool" not in satisfied_keys([thin], requirements)

    kept, dropped = gap_tool_keys(critique, registry_text(domains=[DOMAIN, "cross"]), [thin])
    assert kept == ["limit_up_pool"], "覆盖不足的缺口必须保留下来继续补研究"
    assert dropped == []


def test_same_key_success_with_wrong_arguments_still_needs_research():
    """同 key success 但参数不覆盖要求 → 仍必须补研究。"""
    from app.gateway.tool_registry import registry_text
    from app.graph.gap_loop import coverage_requirements, gap_tool_keys

    wrong_day = _result("limit_up_pool", count=99, arguments={"date": "2026-10-07"})
    critique = _research_more_critique({"arguments": {"date": "2026-10-08"}, "min_datum_count": 5})

    requirements = coverage_requirements(critique)
    assert "limit_up_pool" not in satisfied_keys([wrong_day], requirements)

    kept, _dropped = gap_tool_keys(critique, registry_text(domains=[DOMAIN, "cross"]), [wrong_day])
    assert kept == ["limit_up_pool"]


def test_same_key_meeting_quantity_and_arguments_is_satisfied():
    """对照：条数与参数都满足时，才可以视为已满足（不再浪费一轮）。"""
    from app.gateway.tool_registry import registry_text
    from app.graph.gap_loop import coverage_requirements, gap_tool_keys

    good = _result("limit_up_pool", count=74, arguments={"date": "2026-10-08"})
    critique = _research_more_critique({"arguments": {"date": "2026-10-08"}, "min_datum_count": 20})

    requirements = coverage_requirements(critique)
    assert "limit_up_pool" in satisfied_keys([good], requirements)

    kept, dropped = gap_tool_keys(critique, registry_text(domains=[DOMAIN, "cross"]), [good])
    assert kept == []
    assert dropped == ["limit_up_pool"]


def test_keys_without_declared_requirement_use_generic_rule():
    """没有声明覆盖要求的 key（legacy missing_tool_keys）走通用规则，不被误伤。"""
    from app.gateway.tool_registry import registry_text
    from app.graph.gap_loop import coverage_requirements, gap_tool_keys

    ok = _result("limit_up_pool", count=3)
    thin = _result("limit_up_sectors", count=0)
    critique = Critique(verdict="research_more", missing_tool_keys=["limit_up_pool", "limit_up_sectors"])

    assert coverage_requirements(critique) == {}
    kept, _ = gap_tool_keys(critique, registry_text(domains=[DOMAIN, "cross"]), [ok, thin])
    assert kept == ["limit_up_sectors"], "有数据的 key 视为满足，空数据的仍需补"


@pytest.mark.asyncio
async def test_critic_keeps_under_satisfied_key_for_research_loop():
    """节点级：Critic 点名同 key 的低覆盖缺口 → 必须保留，并进入回环轮的强制补齐。"""
    from app.gateway.tool_registry import registry_text
    from app.graph.gap_loop import append_gap_steps, gap_tool_keys

    thin = _result("limit_up_pool", count=3)
    node = _make_node(
        json.dumps(
            {
                "issues": [
                    {
                        "kind": "missing_evidence",
                        "claim": "涨停个股明细不足，无法比较题材密度",
                        "severity": "high",
                        "action": "research_more",
                        "required_tool_keys": ["limit_up_pool"],
                        "required_coverage": {"min_datum_count": 20},
                    }
                ],
                "reason": "覆盖不足",
            }
        )
    )
    out = await node(_state([thin]))
    critique = out["critique"]

    assert critique.verdict == "research_more"
    assert critique.missing_tool_keys == ["limit_up_pool"]
    assert out.get("errors", []) == []

    # 回环轮：Supervisor 必须能把该 key 强制补进 plan 头部
    registry = registry_text(domains=[DOMAIN, "cross"])
    gap_keys, dropped = gap_tool_keys(critique, registry, [thin])
    assert gap_keys == ["limit_up_pool"]
    assert dropped == []

    merged, forced = append_gap_steps([], gap_keys, registry, max_steps=8, max_gap_steps=3)
    assert forced == ["limit_up_pool"]
    assert merged[0].tool_key == "limit_up_pool"


@pytest.mark.asyncio
async def test_critic_drops_key_that_meets_its_own_requirement():
    """对照：覆盖满足时落库过滤生效（不浪费一轮工具），且记录 already_satisfied。"""
    rich = _result("limit_up_pool", count=74, arguments={"date": "2026-10-08"})
    node = _make_node(
        json.dumps(
            {
                "issues": [
                    {
                        "kind": "missing_evidence",
                        "claim": "缺涨停明细",
                        "action": "research_more",
                        "required_tool_keys": ["limit_up_pool"],
                        "required_coverage": {"min_datum_count": 20},
                    }
                ],
                "reason": "覆盖不足",
            }
        )
    )
    out = await node(_state([rich]))
    critique = out["critique"]

    assert critique.missing_tool_keys == []
    assert critique.gap_key_decisions == [{"key": "limit_up_pool", "reason": "already_satisfied"}]
    # 依旧判 research_more（这份 critique 说的是"缺"，即使代码发现已满足也不伪造 pass）
    assert critique.verdict == "research_more"
    assert out.get("errors", []) == []


# ------------------------------------------------------------------ #
# 安全边界 ①：显式 issues:null 不得退化成 pass                            #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_explicit_null_issues_is_illegal_not_pass():
    """``{"issues": null}`` 且无 verdict：必须安全终止（error + errors），绝不能派生 pass。

    旧实现 ``parse_issues(None) → []`` 之后，``"issues" in data`` 为真 → 派生 pass。
    模型把 answers 写成 null，系统却判它"逐条审查后没问题"，这就是缺陷 A 的本体。
    """
    node = _make_node(json.dumps({"issues": None, "reason": "看起来没问题"}))
    out = await node(_state([_result("limit_up_pool", count=30)]))

    assert out["critique"].verdict == "error", "显式 issues:null 必须判 error"
    errors = out.get("errors", [])
    assert errors, "安全终止必须写 errors（不能静默）"
    assert "issues=NoneType" in errors[0]


@pytest.mark.asyncio
async def test_explicit_null_issues_cannot_be_bypassed_by_pass_verdict():
    """``{"verdict": "pass", "issues": null}``：自报 pass 也不能绕过 payload 校验。"""
    node = _make_node(json.dumps({"verdict": "pass", "issues": None}))
    out = await node(_state([_result("limit_up_pool", count=30)]))

    assert out["critique"].verdict == "error", "非法 payload 优先于模型自报的 pass"
    assert out.get("errors", [])


@pytest.mark.asyncio
async def test_missing_issues_field_stays_legal():
    """对照：**缺少** ``issues`` 字段是合法的，不能被 null 的处理方式误伤。

    哨兵 ``_MISSING_FIELD`` 存在的唯一理由就是把这两者分开。
    """
    node = _make_node(json.dumps({"verdict": "pass", "reason": "证据充分"}))
    out = await node(_state([_result("limit_up_pool", count=30)]))

    assert out["critique"].verdict == "pass"
    assert out.get("errors", []) == []


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"issues": None}, "error"),
        ({"issues": None, "verdict": "pass"}, "error"),
        ({"issues": []}, "pass"),
        ({}, "error"),
    ],
)
def test_verdict_derivation_never_turns_explicit_null_into_pass(payload, expected):
    """纵深防御：``_verdict_from_payload`` 本身也不认 ``issues: null``。

    （它只在模型没给 verdict 时被调用；payload 自带 verdict 时由节点另行校验，
    所以这里不含 ``{"verdict": "pass"}`` 这类输入。）
    """
    from app.graph.nodes.critic import _verdict_from_payload

    assert _verdict_from_payload(dict(payload), []) == expected


def test_parse_issues_distinguishes_missing_field_from_explicit_null():
    """哨兵语义的单元级锚点：缺字段 → []，显式 null → 抛错。

    这是缺陷 A 的根因所在 —— ``data.get("issues")`` 会把两者都变成 ``None``。
    """
    from pydantic import ValidationError

    from app.graph.nodes.critic import _MISSING_FIELD, parse_issues

    assert parse_issues(_MISSING_FIELD) == []
    assert parse_issues([]) == []
    with pytest.raises(ValidationError) as excinfo:
        parse_issues(None)
    assert "NoneType" in str(excinfo.value), "报错必须点明是显式 null（NoneType），而不是当成空数组"


# ------------------------------------------------------------------ #
# 安全边界 ②：同 key 的冲突 arguments 不得被后者覆盖                       #
# ------------------------------------------------------------------ #


def _critique_with(*coverage_specs: dict, tool_key: str = "limit_up_pool") -> Critique:
    """按顺序构造多条 ``action=research_more`` 的 issue，每条一个 required_coverage。"""
    return Critique(
        verdict="research_more",
        reason="覆盖不足",
        issues=[
            AuditIssue(
                kind="missing_evidence",
                claim=f"缺口 {index + 1}",
                severity="high",
                action="research_more",
                required_tool_keys=[tool_key],
                required_coverage=spec,
            )
            for index, spec in enumerate(coverage_specs)
        ],
    )


def test_same_key_identical_argument_requirements_merge_into_one_variant():
    """同 key 且参数完全相同的多条要求 → 合并成一个变体，条数取最大，一次调用即可满足。"""
    from app.graph.gap_loop import coverage_requirements, satisfied_keys

    critique = _critique_with(
        {"arguments": {"date": "2026-10-08"}, "min_datum_count": 20},
        {"arguments": {"date": "2026-10-08"}, "min_datum_count": 10},
    )
    requirements = coverage_requirements(critique)
    assert requirements["limit_up_pool"]["min_datum_count"] == 20
    assert requirements["limit_up_pool"]["argument_variants"] == [
        {"arguments": {"date": "2026-10-08"}, "min_datum_count": 20, "allow_partial": False}
    ]

    good = [_result("limit_up_pool", count=30, arguments={"date": "2026-10-08"})]
    assert "limit_up_pool" in satisfied_keys(good, requirements)


def test_conflicting_argument_requirements_are_kept_apart_not_merged():
    """冲突参数必须各自保留：后出现的取值不能把前一条缺口"覆盖"掉。

    旧实现 ``merged.update(args)`` 下，只看得到 2026-10-08，于是 10-07 的缺口凭空消失。
    """
    from app.graph.gap_loop import coverage_requirements, satisfied_keys

    requirements = coverage_requirements(
        _critique_with(
            {"arguments": {"date": "2026-10-07"}, "min_datum_count": 5},
            {"arguments": {"date": "2026-10-08"}, "min_datum_count": 5},
        )
    )
    variants = requirements["limit_up_pool"]["argument_variants"]
    assert {v["arguments"]["date"] for v in variants} == {"2026-10-07", "2026-10-08"}

    # 只有后出现的那一天的数据到位 → 依旧不满足
    assert "limit_up_pool" not in satisfied_keys(
        [_result("limit_up_pool", count=30, arguments={"date": "2026-10-08"})],
        requirements,
    )
    # 只有先出现的那一天 → 同样不满足
    assert "limit_up_pool" not in satisfied_keys(
        [_result("limit_up_pool", count=30, arguments={"date": "2026-10-07"})],
        requirements,
    )


def test_conflicting_argument_gap_is_never_marked_already_satisfied():
    """冲突参数下，缺口 key 必须留在 ``gap_tool_keys``，不能被标成 already_satisfied。"""
    from app.gateway.tool_registry import registry_text
    from app.graph.gap_loop import gap_tool_keys

    critique = _critique_with(
        {"arguments": {"date": "2026-10-07"}, "min_datum_count": 5},
        {"arguments": {"date": "2026-10-08"}, "min_datum_count": 5},
    )
    registry = registry_text(domains=[DOMAIN, "cross"])
    only_second = [_result("limit_up_pool", count=30, arguments={"date": "2026-10-08"})]

    kept, dropped = gap_tool_keys(critique, registry, only_second)
    assert kept == ["limit_up_pool"], "冲突参数缺口必须保留给补研究"
    assert dropped == [], "绝不能因为只满足后一个参数就被判 already_satisfied"


def test_conflicting_arguments_are_satisfied_by_multiple_calls():
    """多调用策略：两个日期的数据分别到位 → 整条缺口判为满足（不再浪费轮次）。"""
    from app.gateway.tool_registry import registry_text
    from app.graph.gap_loop import coverage_requirements, gap_tool_keys, satisfied_keys

    critique = _critique_with(
        {"arguments": {"date": "2026-10-07"}, "min_datum_count": 5},
        {"arguments": {"date": "2026-10-08"}, "min_datum_count": 5},
    )
    requirements = coverage_requirements(critique)
    both = [
        _result("limit_up_pool", count=30, arguments={"date": "2026-10-07"}),
        _result("limit_up_pool", count=30, arguments={"date": "2026-10-08"}),
    ]
    assert "limit_up_pool" in satisfied_keys(both, requirements)

    kept, dropped = gap_tool_keys(critique, registry_text(domains=[DOMAIN, "cross"]), both)
    assert kept == []
    assert dropped == ["limit_up_pool"]


def test_gap_backfill_plans_one_call_per_unsatisfied_argument():
    """代码级补齐必须为每个**尚未满足**的参数变体各排一次调用（多调用语义）。"""
    from app.gateway.tool_registry import registry_text
    from app.graph.gap_loop import append_gap_steps, coverage_requirements, gap_tool_keys

    critique = _critique_with(
        {"arguments": {"date": "2026-10-07"}},
        {"arguments": {"date": "2026-10-08"}},
    )
    registry = registry_text(domains=[DOMAIN, "cross"])
    requirements = coverage_requirements(critique)

    # 已经拿到 10-07：只补 10-08 这一条
    executed_first = [_result("limit_up_pool", count=30, arguments={"date": "2026-10-07"})]
    gap_keys, _ = gap_tool_keys(critique, registry, executed_first)
    assert gap_keys == ["limit_up_pool"]
    merged, forced = append_gap_steps(
        [],
        gap_keys,
        registry,
        max_steps=8,
        max_gap_steps=3,
        requirements=requirements,
        results=executed_first,
    )
    assert forced == ["limit_up_pool"]
    assert [s.arguments for s in merged] == [{"date": "2026-10-08"}], "已满足的变体不重复规划"

    # 两天都没有：必须各排一次（同一工具、不同参数）
    merged_all, forced_all = append_gap_steps(
        [],
        gap_keys,
        registry,
        max_steps=8,
        max_gap_steps=3,
        requirements=requirements,
        results=[],
    )
    assert [s.tool_key for s in merged_all] == ["limit_up_pool", "limit_up_pool"]
    assert [s.arguments for s in merged_all] == [{"date": "2026-10-07"}, {"date": "2026-10-08"}]
    assert forced_all == ["limit_up_pool", "limit_up_pool"]


def test_gap_context_tells_the_planner_to_plan_one_call_per_argument():
    """回环轮上下文必须把冲突参数摊开，并明确要求"每个参数各规划一次调用"。"""
    from app.graph.gap_loop import render_gap_context

    critique = _critique_with(
        {"arguments": {"date": "2026-10-07"}, "min_datum_count": 5},
        {"arguments": {"date": "2026-10-08"}, "min_datum_count": 5},
    )
    text = render_gap_context(critique, [], max_steps=8)

    assert "2026-10-07" in text
    assert "2026-10-08" in text
    assert "各规划一次调用" in text


@pytest.mark.asyncio
async def test_critic_keeps_conflicting_argument_gap_for_research_loop():
    """节点级集成：冲突参数缺口 → verdict/research_more、key 保留、只补未满足的那个参数。"""
    from app.gateway.tool_registry import registry_text
    from app.graph.gap_loop import append_gap_steps, coverage_requirements, gap_tool_keys

    first_day = _result("limit_up_pool", count=30, arguments={"date": "2026-10-07"})
    node = _make_node(
        json.dumps(
            {
                "issues": [
                    {
                        "kind": "missing_evidence",
                        "claim": "缺 10-07 涨停明细",
                        "severity": "high",
                        "action": "research_more",
                        "required_tool_keys": ["limit_up_pool"],
                        "required_coverage": {"arguments": {"date": "2026-10-07"}},
                    },
                    {
                        "kind": "missing_evidence",
                        "claim": "缺 10-08 涨停明细",
                        "severity": "high",
                        "action": "research_more",
                        "required_tool_keys": ["limit_up_pool"],
                        "required_coverage": {"arguments": {"date": "2026-10-08"}},
                    },
                ],
                "reason": "两天的明细都要",
            }
        )
    )
    out = await node(_state([first_day]))
    critique = out["critique"]

    assert critique.verdict == "research_more"
    assert critique.missing_tool_keys == ["limit_up_pool"], "10-08 未满足，key 必须保留"
    assert out.get("errors", []) == []

    registry = registry_text(domains=[DOMAIN, "cross"])
    gap_keys, dropped = gap_tool_keys(critique, registry, [first_day])
    assert gap_keys == ["limit_up_pool"]
    assert dropped == []

    merged, forced = append_gap_steps(
        [],
        gap_keys,
        registry,
        max_steps=8,
        max_gap_steps=3,
        requirements=coverage_requirements(critique),
        results=[first_day],
    )
    assert forced == ["limit_up_pool"]
    assert [s.arguments for s in merged] == [{"date": "2026-10-08"}]


# ------------------------------------------------------------------ #
# 安全边界 ③：allow_partial 是 key 级"一票否决"，不得被参数变体绕过          #
# ------------------------------------------------------------------ #


def _mixed_partial_policy_critique() -> Critique:
    """issue A：date=07 且**禁止** partial；issue B：date=08 且**允许** partial。

    这正是绕过路径的复现条件：key 级被一票否决为 ``allow_partial=False``，
    但 ``date=08`` 变体自己写着 ``allow_partial=True``。
    """
    return _critique_with(
        {"arguments": {"date": "2026-10-07"}, "allow_partial": False},
        {"arguments": {"date": "2026-10-08"}, "allow_partial": True},
    )


def test_key_level_allow_partial_veto_cannot_be_bypassed_by_a_variant():
    """date=07 success + date=08 partial → 该 key **必须**仍未满足。

    旧实现只看变体自己的 ``allow_partial``，于是 date=08 的变体拿 partial 结果顶包，
    issue A 的"禁止 partial"被 issue B 的参数变体整个绕过。
    """
    from app.gateway.tool_registry import registry_text
    from app.graph.gap_loop import coverage_requirements, gap_tool_keys, satisfied_keys

    critique = _mixed_partial_policy_critique()
    requirements = coverage_requirements(critique)
    assert requirements["limit_up_pool"]["allow_partial"] is False, "key 级一票否决必须在映射里可见"

    results = [
        _result("limit_up_pool", count=30, arguments={"date": "2026-10-07"}),
        _result("limit_up_pool", status="partial", partial=True, count=30, arguments={"date": "2026-10-08"}),
    ]
    assert "limit_up_pool" not in satisfied_keys(results, requirements), "partial 不得满足任何该 key 的变体"

    kept, dropped = gap_tool_keys(critique, registry_text(domains=[DOMAIN, "cross"]), results)
    assert kept == ["limit_up_pool"], "被 key 级禁令挡下的变体必须继续补研究"
    assert dropped == [], "绝不能被标成 already_satisfied"


def test_key_level_allow_partial_veto_still_backfills_the_blocked_variant():
    """被禁令挡下的变体必须生成正确 arguments 的补研究调用。"""
    from app.gateway.tool_registry import registry_text
    from app.graph.gap_loop import append_gap_steps, coverage_requirements, gap_tool_keys

    critique = _mixed_partial_policy_critique()
    registry = registry_text(domains=[DOMAIN, "cross"])
    results = [
        _result("limit_up_pool", count=30, arguments={"date": "2026-10-07"}),
        _result("limit_up_pool", status="partial", partial=True, count=30, arguments={"date": "2026-10-08"}),
    ]
    gap_keys, _ = gap_tool_keys(critique, registry, results)
    assert gap_keys == ["limit_up_pool"]

    merged, forced = append_gap_steps(
        [],
        gap_keys,
        registry,
        max_steps=8,
        max_gap_steps=3,
        requirements=coverage_requirements(critique),
        results=results,
    )
    assert forced == ["limit_up_pool"]
    assert [s.arguments for s in merged] == [{"date": "2026-10-08"}], "只补被禁令挡下的那个参数"


def test_key_level_allow_partial_veto_does_not_block_success():
    """对照：两个日期都 success → 该 key 可以满足（禁令只针对 partial）。"""
    from app.gateway.tool_registry import registry_text
    from app.graph.gap_loop import coverage_requirements, gap_tool_keys, satisfied_keys

    critique = _mixed_partial_policy_critique()
    requirements = coverage_requirements(critique)
    results = [
        _result("limit_up_pool", count=30, arguments={"date": "2026-10-07"}),
        _result("limit_up_pool", count=30, arguments={"date": "2026-10-08"}),
    ]
    assert "limit_up_pool" in satisfied_keys(results, requirements)

    kept, dropped = gap_tool_keys(critique, registry_text(domains=[DOMAIN, "cross"]), results)
    assert kept == []
    assert dropped == ["limit_up_pool"]


def test_partial_is_reusable_when_every_issue_allows_it():
    """对照：没有任何 issue 禁止 partial 时，`allow_partial=true` 的变体仍可复用 partial 数据。"""
    from app.graph.gap_loop import coverage_requirements, satisfied_keys

    critique = _critique_with(
        {"arguments": {"date": "2026-10-07"}, "allow_partial": True},
        {"arguments": {"date": "2026-10-08"}, "allow_partial": True},
    )
    requirements = coverage_requirements(critique)
    assert requirements["limit_up_pool"]["allow_partial"] is True

    partials = [
        _result("limit_up_pool", status="partial", partial=True, count=30, arguments={"date": "2026-10-07"}),
        _result("limit_up_pool", status="partial", partial=True, count=30, arguments={"date": "2026-10-08"}),
    ]
    assert "limit_up_pool" in satisfied_keys(partials, requirements)


def test_partial_still_unsatisfying_when_undeclared():
    """对照：**未声明** allow_partial 时默认禁止复用 partial（不因变体而放宽）。"""
    from app.graph.gap_loop import coverage_requirements, satisfied_keys

    critique = _critique_with(
        {"arguments": {"date": "2026-10-07"}},
        {"arguments": {"date": "2026-10-08"}},
    )
    requirements = coverage_requirements(critique)
    assert "allow_partial" not in requirements["limit_up_pool"]

    partials = [
        _result("limit_up_pool", status="partial", partial=True, count=30, arguments={"date": "2026-10-07"}),
        _result("limit_up_pool", status="partial", partial=True, count=30, arguments={"date": "2026-10-08"}),
    ]
    assert "limit_up_pool" not in satisfied_keys(partials, requirements)


def test_key_level_veto_applies_to_the_forbidding_issues_own_variant():
    """禁令同样作用于**禁止 partial 那条 issue 自己的变体**（不只作用在跨变体场景）。"""
    from app.graph.gap_loop import coverage_requirements, satisfied_keys

    critique = _critique_with(
        {"arguments": {"date": "2026-10-07"}, "allow_partial": False},
        {"arguments": {"date": "2026-10-08"}, "allow_partial": True},
    )
    requirements = coverage_requirements(critique)
    results = [
        _result("limit_up_pool", status="partial", partial=True, count=30, arguments={"date": "2026-10-07"}),
        _result("limit_up_pool", count=30, arguments={"date": "2026-10-08"}),
    ]
    assert "limit_up_pool" not in satisfied_keys(results, requirements)
