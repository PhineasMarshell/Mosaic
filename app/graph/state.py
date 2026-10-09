"""LangGraph 图的状态定义。

这是所有节点的唯一契约 —— 每个节点只读写 state 中的字段，
不依赖外部传入参数（除了通过 ainvoke 传入的初始 state）。
"""

import json
from operator import add
from typing import Annotated, Any, Literal


def _merge_dicts(a: dict, b: dict) -> dict:
    """LangGraph 用的 dict reducer — 把两个字典 shallow-merge。"""
    merged = dict(a)
    merged.update(b)
    return merged


# ------------------------------------------------------------------ #
# T16：research_more 回环的去重 reducer                                #
# ------------------------------------------------------------------ #
# 旧实现 results / evidence / findings / tool_results 全用 operator.add：
# critic 判 research_more 回 supervisor 后 analyst 再跑一轮，同名同参的
# ToolResult、同 id 的 Evidence、同 analyst 的 finding 全部**追加**，
# reasoning 的 normalized_data 里同一份行情出现两遍（见 research/reasoning.py
# 的 normalized_data 拼装）。route 明确"回环时整体覆盖"，这几个字段必须同等语义：
# 同键后到者覆盖（保留最新），不同键正常追加（保留并行 analyst 合并能力）。


def _dedup_append(a: list, b: list, key_fn) -> list:
    """按 key_fn 去重合并两个列表：b 中与 a 同键的项**覆盖**旧值，其余追加。"""
    merged = list(a)
    index = {key_fn(item): i for i, item in enumerate(merged)}
    for item in b:
        key = key_fn(item)
        if key in index:
            merged[index[key]] = item
        else:
            index[key] = len(merged)
            merged.append(item)
    return merged


def _tool_result_key(item) -> tuple[str, str]:
    """ToolResult（对象或 dict）按 (tool, arguments) 去重。"""
    if isinstance(item, dict):
        tool = str(item.get("tool", ""))
        arguments = item.get("arguments") or {}
    else:
        tool = str(getattr(item, "tool", "") or "")
        arguments = getattr(item, "arguments", None) or {}
    return tool, json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)


def _merge_results(a: list, b: list) -> list:
    """results 的去重 reducer（同签名保留最新一轮的结果）。"""
    return _dedup_append(a, b, _tool_result_key)


def _evidence_key(item) -> str:
    """Evidence（对象或 dict）按 id 去重（T4 后 id 形如 technical-001）。"""
    if isinstance(item, dict):
        evid = item.get("id")
    else:
        evid = getattr(item, "id", None)
    return str(evid) if evid else f"__no_id_{id(item)}"


def _merge_evidence(a: list, b: list) -> list:
    """evidence 的去重 reducer（同 id 保留最新一轮的证据）。"""
    return _dedup_append(a, b, _evidence_key)


def _finding_key(item) -> str:
    """AnalystFinding（对象或 dict）按 analyst 去重——每个 analyst 每轮只产一条。"""
    if isinstance(item, dict):
        analyst = item.get("analyst")
    else:
        analyst = getattr(item, "analyst", None)
    return str(analyst) if analyst else f"__no_analyst_{id(item)}"


def _merge_findings(a: list, b: list) -> list:
    """findings 的去重 reducer（同 analyst 保留最新一轮的 digest）。"""
    return _dedup_append(a, b, _finding_key)


from pydantic import BaseModel

# ------------------------------------------------------------------ #
# 分析员角色名 — technical/fundamental/moneyflow + 可选 news/sentiment #
# ------------------------------------------------------------------ #

#: P5 后：kernel 已删除，analyst 为 technical / fundamental / moneyflow 并行 Send
#: news / sentiment 为可选开关节点（P4）
AnalystName = Literal["technical", "fundamental", "moneyflow", "news", "sentiment"]


class ResearchState(BaseModel):
    """研究图的共享状态。

    所有字段都有默认值（空 list / None），langgraph StateGraph 会合并
    各节点的返回字典与当前 state —— 带 reducer 的字段做 merge，
    不带 reducer 的直接覆盖。
    """

    # ── 输入（由 orchestrator 初始化）──
    question: str
    conversation_id: str | None = None
    domain: str | None = None
    #: 阶段 0：一次调查的短 id，跨节点传递并落到每条结构化运行摘要日志上。
    #: 由 Orchestrator.run / SSE 路径生成；缺失时日志里 run_id=None（不猜、不补造）。
    run_id: str | None = None
    #: T23b：整次调查的预算截止时刻（time.monotonic() 秒）。图启动前由 orchestrator /
    #: SSE 路径写入；research_more 回环时**覆盖**（标量字段无 reducer，正是想要的语义——
    #: 第二轮 analyst 不能重新获得一整份预算）。
    budget_deadline: float | None = None

    # ── Supervisor 产出 ──
    intent: object | None = None
    #: Supervisor 产出的按 analyst 分组的工具分配（P2.5-3）
    route: list = []  # list[AnalystAssignment]，不加 reducer —— research_more 回环时整体覆盖

    # ── analyst 产出（P3：三节点 Send 并行）──
    report: object | None = None
    #: ToolResult[] — gate / reasoning 读取的原始结果（reducer 合并三节点输出；
    #: T16 起按 (tool, arguments) 去重，research_more 回环不再成倍重复）
    results: Annotated[list[object], _merge_results] = []
    #: （T16 已删除）旧字段 tool_results 与 results 内容完全相同且无任何读取方
    cache_stats: Annotated[dict[str, int], _merge_dicts] = {}

    # ── Evidence Gate ──
    gate: object | None = None  # EvidenceGateResult

    # ── Critic ──
    critique: object | None = None  # Critique from critic.py
    #: 阶段 6：两条回环路径**独立计数**，各自有独立上限（Settings.max_rewrites /
    #: max_research_rounds）。旧实现共用一个 revision_count，"改写一轮"与
    #: "补一轮证据"花掉的是同一份额度 —— 便宜的重写挤掉了必须重跑工具的研究轮。
    #: `rewrite_count` 只在 `revise → reasoning` 递增（ReasoningNode 写入）；
    #: `research_round_count` 只在 `research_more → supervisor` 递增（SupervisorNode 写入）。
    rewrite_count: int = 0
    research_round_count: int = 0
    #: 兼容字段 = rewrite_count + research_round_count（总回环轮次，只用于日志/展示）。
    #: 路由决策**不得**读它，否则计数拆分失去意义。
    revision_count: int = 0

    # ── 终态（阶段 5 保守版）──
    #: 最终审计状态（由 finalize_audit 节点写入；pass 路径不经过该节点，
    #: 由 build_response_from_state 从 critique.verdict 兜底派生）。
    final_audit_status: object | None = None
    #: 交付状态 verified / degraded / blocked / failed —— 调用方判断"能否当作
    #: 可信结论展示"的**唯一**依据（errors == [] 不再代表可信）。
    delivery_status: object | None = None

    # ── 并行写入字段（必须 reducer）──
    #: T16：evidence 按 id、findings 按 analyst 去重（回环覆盖，保留最新）
    evidence: Annotated[list[Any], _merge_evidence] = []  # Evidence（T4 后带唯一 id）
    findings: Annotated[list, _merge_findings] = []  # AnalystFinding
    errors: Annotated[list[str], add] = []  # 错误跨轮追加是期望行为
