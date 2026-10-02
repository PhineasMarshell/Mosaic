"""LangGraph 图的状态定义。

这是所有节点的唯一契约 —— 每个节点只读写 state 中的字段，
不依赖外部传入参数（除了通过 ainvoke 传入的初始 state）。
"""

from typing import Annotated, Any, Literal, Optional

from operator import add
from typing import Callable


def _merge_dicts(a: dict, b: dict) -> dict:
    """LangGraph 用的 dict reducer — 把两个字典 shallow-merge。"""
    merged = dict(a)
    merged.update(b)
    return merged
from pydantic import BaseModel

from app.models.evidence import Evidence
from app.models.market import NormalizedDatum


# ------------------------------------------------------------------ #
# 分析员角色名 — P0/P1 kernel, P3+ technical/fundamental/moneyflow   #
# ------------------------------------------------------------------ #

#: P0/P1：单节点图，整包调用 MarketDetective.investigate()
#: P3+：扩展为 technical / fundamental / moneyflow 并行 Send
AnalystName = Literal["kernel", "technical", "fundamental", "moneyflow", "news", "sentiment"]


class ResearchState(BaseModel):
    """研究图的共享状态。

    所有字段都有默认值（空 list / None），langgraph StateGraph 会合并
    各节点的返回字典与当前 state —— 带 reducer 的字段做 merge，
    不带 reducer 的直接覆盖。
    """

    # ── 输入（由 orchestrator 初始化）──
    question: str
    conversation_id: Optional[str] = None
    domain: Optional[str] = None

    # ── Supervisor 产出 ──
    intent: Optional[object] = None
    #: Supervisor 产出的按 analyst 分组的工具分配（P2.5-3）
    route: list = []     # list[AnalystAssignment]，不加 reducer —— research_more 回环时整体覆盖

    # ── analyst 产出（P3：三节点 Send 并行）──
    report: Optional[object] = None
    #: ToolResult[] — gate / reasoning 读取的原始结果（reducer 合并三节点输出）
    results: Annotated[list[object], add] = []
    tool_results: Annotated[list[object], add] = []
    cache_stats: Annotated[dict[str, int], _merge_dicts] = {}

    # ── Evidence Gate ──
    gate: Optional[object] = None     # EvidenceGateResult

    # ── Critic ──
    critique: Optional[object] = None # Critique from critic.py
    revision_count: int = 0           # 修订轮次计数器

    # ── 并行写入字段（必须 reducer）──
    evidence: Annotated[list[Any], add] = []  # NormalizedDatum or Evidence
    findings: Annotated[list, add] = []     # AnalystFinding
    errors: Annotated[list[str], add] = []
