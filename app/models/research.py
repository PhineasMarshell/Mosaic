"""研究意图与计划的数据模型。

新增市场域时：
1. 在 MarketDomain 的 Literal 中添加字面量
2. 在 DEFAULT_DOMAINS 中列入默认启用的市场
3. 在 tool_registry.py 中注册新市场的工具
4. 在 normalizer.py 中补充域名推断规则（可选）
5. 更新 planner / reasoning prompt 中的域选择逻辑
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


# ------------------------------------------------------------------ #
# 市场域定义 — 新增市场只需在此添加字面量并在 tool_registry 中注册   #
# ------------------------------------------------------------------ #

MarketDomain = Literal[
    "a_share",       # A 股市场
    "crypto",        # 加密货币
    "hk_stock",      # 港股市场
    "us_stock",      # 美股市场
    "commodities",   # 大宗商品（黄金、铜、原油…）
    "macro",         # 宏观经济
    "unknown",
]

#: 默认启用哪些市场域；Planner 可据此做跨域决策。
DEFAULT_DOMAINS: list[MarketDomain] = [
    "a_share", "crypto", "hk_stock", "commodities", "us_stock",
]


class ResearchIntent(BaseModel):
    """Planner 对用户问题的意图解析结果。"""

    domain: MarketDomain = "a_share"
    task: Literal[
        "market_diagnosis",
        "market_summary",
        "theme_analysis",
        "company_research",
        "anomaly_detection",
    ] = "market_summary"
    time_scope: str = "today"
    question: str = ""
    needs_comparison: bool = True
    needs_evidence: bool = True


class ToolCallPlan(BaseModel):
    tool_key: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    purpose: str
    priority: Literal["high", "medium", "low"] = "medium"


class ResearchPlan(BaseModel):
    intent: ResearchIntent = Field(default_factory=ResearchIntent)
    steps: list[ToolCallPlan] = Field(default_factory=list)
    early_stop: bool = False
