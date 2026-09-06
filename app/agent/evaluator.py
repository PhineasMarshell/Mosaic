"""Evidence Evaluator — 判断证据是否足够回答用户问题。

多域设计：
- accept intent.domain 以调整评估规则
- A 股侧重：情绪 + 涨停生态 + 题材结构
- Crypto 侧重：价格 + OI + Funding + Liquidation
- US Stock（待接入）：量价 + 板块轮动 + VIX 等宏观指标
"""

import json
import re
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from app.agent.evidence_gate import EvidenceGateResult
from app.config import Settings
from app.gateway.tool_registry import ALL_TOOLS, resolve_tool_by_name
from app.models.evidence import Evidence
from app.models.market import ToolResult
from app.models.research import MarketDomain, ToolCallPlan


class ResearchDecision(BaseModel):
    """Evidence Evaluator 的结构化输出。"""

    sufficient: bool = False
    reason: str = ""
    coverage: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "low"
    next_steps: list[ToolCallPlan] = Field(default_factory=list)
    evidence_quality: Literal["strong", "acceptable", "weak", "none"] = "none"
    recommended_next_action: Literal["continue_research", "finish", "stop"] = "continue_research"


def _resolve_available_tool_keys() -> set[str]:
    """获取所有已注册工具的 key 集合。"""
    return {tool.key for tool in ALL_TOOLS}


class EvidenceEvaluator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=settings.llm_timeout_seconds,
        )

    async def evaluate(
        self,
        question: str,
        results: list[ToolResult],
        evidence: list[Evidence],
        gate: EvidenceGateResult,
        called_tools: list[str] | None = None,
        domain: MarketDomain = "a_share",
    ) -> ResearchDecision:
        """评估证据充分性。

        Args:
            question: 用户原始问题
            results: 工具调用结果
            evidence: 标准化后的证据列表
            gate: 代码级证据门控结果
            called_tools: 已调用的工具名列表
            domain: 目标市场域
        """
        called_tools = called_tools or []

        # 1. 整理 Normalize 数据
        normalized_data = [
            {
                "tool": result.tool,
                "metric": datum.metric,
                "value": datum.value,
                "unit": datum.unit,
                "timestamp": datum.timestamp,
                "source": datum.source,
                "status": datum.status,
                "partial": datum.partial,
                "note": datum.note,
            }
            for result in results
            for datum in result.normalized
        ]

        evidence_data = [
            {
                "id": item.id,
                "source_tool": item.source_tool,
                "domain": item.domain,
                "metric": item.metric,
                "value": item.value,
                "timestamp": item.timestamp,
                "source": item.source,
                "status": item.status,
                "partial": item.partial,
                "note": item.note,
            }
            for item in evidence
        ]

        # 2. 该域的工具覆盖情况
        domain_tools_lines = []
        for result in results:
            try:
                meta = resolve_tool_by_name(result.tool)
                if meta.domain == domain:
                    domain_tools_lines.append(f"- {meta.key}: {meta.purpose}")
            except KeyError:
                pass

        # 3. Gate 数据
        gate_json = json.dumps({
            "has_evidence": gate.has_evidence,
            "successful_tools": gate.successful_tools,
            "partial_tools": gate.partial_tools,
            "error_tools": gate.error_tools,
            "reason": gate.reason,
        }, ensure_ascii=False, indent=2)

        # 4. 域特定的评估提示
        domain_rules = _get_domain_evaluation_rules(domain)

        prompt = (
            f"你是 Mosaic 的 Evidence Evaluator。\n\n"
            f"你的任务是判断当前获得的市场数据和 Evidence，"
            f"是否足以支持回答用户的问题。\n\n"
            f"用户问题：{question}\n"
            f"目标市场域：{domain}\n\n"
            f"====================\n"
            f"当前已经调用的 Tools\n"
            f"====================\n\n"
            f"{json.dumps(called_tools, ensure_ascii=False, indent=2)}\n\n"
            f"====================\n"
            f"该域的工具覆盖情况\n"
            f"====================\n\n"
            f"{chr(10).join(domain_tools_lines) if domain_tools_lines else '(无该域工具结果)'}\n\n"
            f"====================\n"
            f"Normalize 后的数据\n"
            f"====================\n\n"
            f"{json.dumps(normalized_data, ensure_ascii=False, indent=2, default=str)}\n\n"
            f"====================\n"
            f"Evidence\n"
            f"====================\n\n"
            f"{json.dumps(evidence_data, ensure_ascii=False, indent=2, default=str)}\n\n"
            f"====================\n"
            f"Evidence Gate\n"
            f"====================\n\n"
            f"{gate_json}\n\n"
            f"====================\n"
            f"{domain_rules}\n"
            f"====================\n\n"
            f"1. 你必须根据实际 Evidence 判断，不能凭空假设数据存在。\n"
            f"2. Evidence Gate 是代码层面的硬检查。如果 has_evidence=false，不允许直接判断 sufficient=true。\n"
            f"3. 如果当前没有有效 Evidence：sufficient 必须为 false。\n"
            f"4. partial=true 的数据不能作为唯一核心证据。\n"
            f"5. error 数据不能作为支持结论的证据。\n"
            f"6. 不要选择已经调用过的 Tool（完全相同的 key）。\n"
            f"7. 不要给出买入、卖出、做多、做空等交易指令。\n\n"
            f"如果当前 Evidence 已经足够：\n"
            f"    sufficient=true\n"
            f"    recommended_next_action=\"finish\"\n\n"
            f"如果当前 Evidence 不足：\n"
            f"    sufficient=false\n"
            f"    recommended_next_action=\"continue_research\"\n\n"
            f"如果已经没有合理的新 Tool 可以调用：\n"
            f"    recommended_next_action=\"stop\"\n\n"
            f"confidence 表示当前证据对最终回答的支持程度。\n\n"
            f"evidence_quality：\n"
            f"- strong：多个独立维度、数据质量较好\n"
            f"- acceptable：基本足够，但存在一定缺口\n"
            f"- weak：有数据，但不足以支持核心结论\n"
            f"- none：没有有效证据\n\n"
            f"只输出合法 JSON。\n\n"
            f'{{\n'
            f'    "sufficient": true,\n'
            f'    "reason": "...",\n'
            f'    "coverage": ["市场状态", "市场情绪"],\n'
            f'    "missing": [],\n'
            f'    "confidence": "medium",\n'
            f'    "evidence_quality": "acceptable",\n'
            f'    "recommended_next_action": "finish",\n'
            f'    "next_steps": []\n'
            f'}}'
        )

        # 5. 调用 LLM
        try:
            response = await self.client.chat.completions.create(
                model=self.settings.openai_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是 Mosaic Evidence Evaluator。"
                            "必须只返回合法 JSON，不要包含 Markdown 代码块标记。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )

            content = response.choices[0].message.content or "{}"
        except Exception:
            return _fallback_decision(gate, called_tools)

        # 6. JSON 解析
        cleaned = _extract_json(content)
        if not cleaned:
            return _fallback_decision(gate, called_tools)

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return _fallback_decision(gate, called_tools)

        if not isinstance(data, dict):
            return _fallback_decision(gate, called_tools)

        try:
            decision = ResearchDecision.model_validate(data)
        except Exception:
            decision = ResearchDecision(
                sufficient=False,
                reason="Evaluator 返回的结构不完整，使用降级判断",
                evidence_quality="weak" if gate.has_evidence else "none",
                recommended_next_action=(
                    "continue_research" if not gate.has_evidence else "finish"
                ),
            )

        # 7. 服务端兜底
        if not gate.has_evidence:
            decision.sufficient = False
            decision.evidence_quality = "none"
            decision.recommended_next_action = "continue_research"
            if not decision.reason:
                decision.reason = gate.reason

        # 8. 清理 next_steps — 只允许目标域的工具
        valid_tool_keys = _resolve_available_tool_keys()
        known_tool_keys: set[str] = set()

        for tool_name in (called_tools or []):
            if not tool_name:
                continue
            key = tool_name_to_key(tool_name)
            if key:
                known_tool_keys.add(key)

        filtered_steps: list[ToolCallPlan] = []
        for step in decision.next_steps:
            if step.tool_key not in valid_tool_keys:
                continue
            if step.tool_key in known_tool_keys:
                continue
            # 验证推荐的工具属于目标域 OR 是跨域通用工具
            try:
                meta = resolve_tool_by_name(step.tool_key)
            except KeyError:
                continue
            if meta.domain != domain and meta.domain != "unknown" and meta.domain != "cross":
                continue
            filtered_steps.append(step)

        decision.next_steps = filtered_steps

        if (not decision.sufficient
                and decision.recommended_next_action == "continue_research"
                and not decision.next_steps):
            decision.recommended_next_action = "stop"

        return decision


# ------------------------------------------------------------------ #
# 域特定评估规则文本                                                   #
# ------------------------------------------------------------------ #

def _get_domain_evaluation_rules(domain: MarketDomain) -> str:
    """返回针对特定域的评估规则说明。"""
    rules_map: dict[MarketDomain, str] = {
        "a_share": (
            "【A 股评估规则】\n"
            "- 对于今天发生了什么类问题，至少应覆盖：市场概览(overview) + 情绪(sentiment)\n"
            "- 对于为什么弱/强类问题，还应包括：涨停生态(limit_up_count/sectors) + 题材分析\n"
            "- 对于哪里最强类问题，应重点关注 sectors/pool\n"
        ),
        "crypto": (
            "【Crypto 评估规则】\n"
            "- 对于市场状况如何类问题，至少应覆盖：快照(snapshot) + K线(klines)\n"
            "- 对于上涨是否健康类问题，还应包括：衍生品历史(OI/funding)\n"
            "- 对于是否有风险类问题，应关注：清算数据(liquidation_today) + 大户仓位(top_position)\n"
            "- 杠杆信号需综合 OI 增速 vs 价格增速、Funding Rate 方向判断\n"
        ),
        "hk_stock": (
            "【港股评估规则】\n"
            "- 当前通过 quote_tencent_quote_get（HK 代码如 HK00700）获取实时行情\n"
            "- 对于市场状况类问题，至少应覆盖：主要指数 + 头部个股行情（腾讯行情 API）\n"
            "- 南向资金流向可作为港股情绪参考（待接入专用工具）\n"
            "- H 股与 A 股同一公司可交叉比较价差\n"
        ),
        "commodities": (
            "【大宗商品评估规则】\n"
            "- 当前可通过 market/snapshot 和 market/klines 通用接口获取部分数据\n"
            "- 黄金：避险情绪 proxy，需关注实际利率、美元指数\n"
            "- 铜：全球经济周期指标，关注中国 PMI 和制造业数据\n"
            "- 原油：地缘政治 + OPEC+ 政策 + 需求预期\n"
            "- 未来需接入独立 commodity data provider（如 Bloomberg, LME）\n"
        ),
        "macro": (
            "【Macro 评估规则】— 该域工具正在接入中。\n"
        ),
    }
    return rules_map.get(domain, "")


# ------------------------------------------------------------------ #
# 工具函数                                                             #
# ------------------------------------------------------------------ #

def tool_name_to_key(tool_name: str) -> str | None:
    """operationId → Mosaic business tool key"""
    from app.gateway.tool_registry import BY_NAME
    for tool in BY_NAME.values():
        if tool.tool_name == tool_name:
            return tool.key
    return None


def _extract_json(text: str) -> str | None:
    """从 LLM 响应中提取 JSON。处理 Markdown 代码块等情况。"""
    if not text:
        return None

    text = text.strip()

    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass

    match = re.search(r"(?:```(?:json)?\s*?\n)([\s\S]*?)(?:```)", text)
    if match:
        candidate = match.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = text[first_brace:last_brace + 1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    return None


def _fallback_decision(
    gate: EvidenceGateResult,
    called_tools: list[str],
) -> ResearchDecision:
    """LLM 不可达或返回无法解析时的降级决策。"""

    known_keys: set[str] = set()
    for name in called_tools:
        if not name:
            continue
        key = tool_name_to_key(name)
        if key:
            known_keys.add(key)
        elif name in _resolve_available_tool_keys():
            known_keys.add(name)

    remaining_tools = _resolve_available_tool_keys() - known_keys

    if not gate.has_evidence:
        next_key = next(iter(remaining_tools), "snapshot")
        return ResearchDecision(
            sufficient=False,
            reason=f"LLM Evaluator 不可用，证据门控报告: {gate.reason}",
            evidence_quality="none",
            recommended_next_action="continue_research",
            next_steps=[
                ToolCallPlan(tool_key=next_key, arguments={}, purpose="回退到默认工具重试"),
            ],
        )

    if not remaining_tools:
        return ResearchDecision(
            sufficient=True,
            reason=f"LLM Evaluator 不可用，基于证据门控保守判断: {gate.reason}",
            coverage=list(gate.successful_tools),
            evidence_quality="weak",
            recommended_next_action="finish",
        )

    next_key = next(iter(remaining_tools))
    return ResearchDecision(
        sufficient=False,
        reason=f"LLM Evaluator 不可用，还有 {len(remaining_tools)} 个工具未调用: {gate.reason}",
        coverage=list(gate.successful_tools),
        missing=list(remaining_tools),
        evidence_quality="weak",
        recommended_next_action="continue_research",
        next_steps=[
            ToolCallPlan(tool_key=next_key, arguments={}, purpose="回退策略：尝试下一个未调用工具"),
        ],
    )
