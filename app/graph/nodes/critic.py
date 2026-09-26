"""Critic 节点 -- 对 Report + Evidence 进行审计评估。

输出 Critique（pass / revise / research_more）：
- pass: 证据充分，报告可信 -> END
- revise: 有 unsupported claims -> 回 Reasoning 重写（<= max_revisions 轮）
- research_more: 有 missing points -> 带缺口感召 Supervisor 补充研究（最多 1 次）

P2 验收：Critic 条件边路由正确；打回重写的完整路径有测试。

对应现有代码：app/agent/evaluator.py 的 EvidenceEvaluator + ResearchDecision。
"""

import json
import logging

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import Settings
from app.errors import LLMOutputError
from app.graph.state import ResearchState
from app.llm_json import parse_json_object

logger = logging.getLogger(__name__)


class Critique(BaseModel):
    """Critic 评估结论。"""

    verdict: str  # "pass" | "revise" | "research_more"
    reason: str = ""
    missing_points: list[str] = []  # 证据缺口（research_more 时喂回 Supervisor）
    unsupported_claims: list[str] = []  # 无证据支撑的表述（revise 时喂回 Reasoning）


_CRITIC_SYSTEM_PROMPT = (
    "你是 Mosaic 的 Critic。你的任务是逐项审查报告的每个关键论断是否都有对应的"
    "证据支撑。不要凭空增加要求 -- 只看已有证据够不够回答用户问题。"
)

# Domain-specific review rules
_DOMAIN_RULES = {
    "a_share": (
        "[A股审查规则]\n"
        "- 如果报告声称市场情绪悲观/乐观，必须有 sentiment / limit_up_count 等数据支撑\n"
        "- 如果报告提到某个题材板块强势，必须有 sectors / pool 的数据支撑\n"
        "- 不能在没有涨停生态数据的情况下声称 '涨停板萎缩'"
    ),
    "crypto": (
        "[Crypto审查规则]\n"
        "- 关于杠杆风险的判断需要 OI / Funding 数据支撑\n"
        "- 不能在没有清算数据的情况下声称 '无爆仓风险'"
    ),
}


def _get_domain_rules(domain: str) -> str:
    return _DOMAIN_RULES.get(domain, "")


def _format_report_for_review(report) -> str:
    """将报告格式化为人类可读的审查文本。"""
    parts = [
        f"问题: {getattr(report, 'what_happened', 'N/A')[:200]}",
        f"置信度: {getattr(report, 'confidence', 'N/A')}",
        f"市场状态: {getattr(report, 'state_label', 'N/A')}",
    ]
    if hasattr(report, "strong_areas") and report.strong_areas:
        parts.append(f"强势方向: {', '.join(report.strong_areas)}")
    if hasattr(report, "risks") and report.risks:
        parts.append(
            "风险:\n" + "\n".join(f"  - {r}" for r in report.risks)
        )
    if hasattr(report, "why") and report.why:
        parts.append(
            "原因分析:\n" + "\n".join(f"  - {w}" for w in report.why)
        )
    return "\n".join(parts)


def _format_evidence_for_review(results, evidence, gate) -> str:
    """格式化证据用于审查。"""
    lines = ["=== 成功工具 ==="]
    if gate:
        for t in gate.successful_tools:
            lines.append(f"  OK {t}")
        for t in gate.partial_tools:
            lines.append(f"  ~ partial: {t}")
        for err in gate.error_tools:
            lines.append(f"  FAIL {err['tool']}: {err.get('error', '?')}")
    lines.append("")
    # Add some normalize data summary (limited length to avoid prompt overflow)
    for result in results[:20]:
        if hasattr(result, "normalized"):
            normalized = result.normalized[:10]
        else:
            normalized = result.get("normalized", [])[:10]
        for datum in normalized:
            if hasattr(datum, "metric"):
                metric = datum.metric
                value = str(datum.value)[:80]
            else:
                metric = datum.get("metric", "?")
                value = str(datum.get("value", ""))[:80]
            lines.append(f"  - {metric}: {value}")
    return "\n".join(lines)


class CriticNode:
    """Critique 节点：对 Report 进行证据一致性审计。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=settings.llm_timeout_seconds,
        )

    async def __call__(self, state):
        """执行 Critic 审查，返回 Critique。"""
        try:
            # Normalize state to dict (handle both ResearchState and dict)
            if hasattr(state, "model_dump"):
                state = state.model_dump(exclude_none=False)

            report = state.get("report")
            if not report:
                return {
                    "critique": Critique(
                        verdict="research_more",
                        reason="推理引擎未产出报告",
                        missing_points=["完整的市场情报报告"],
                    )
                }

            results = state.get("results", [])
            evidence = state.get("evidence", [])
            gate = state.get("gate")

            # Build domain-aware review prompt
            intent = state.get("intent")
            domain = None
            if intent:
                domain = getattr(intent, "domain", None) or state.domain or "a_share"
            elif state.domain:
                domain = state.domain
            else:
                domain = "a_share"

            rules = _get_domain_rules(domain)

            # Assemble prompt pieces
            prompt_pieces = [
                f"用户问题: {state.question}\n",
                f"目标域: {domain}\n\n",
                "=== 待审查的报告 ===\n",
                _format_report_for_review(report),
                "\n\n=== 实际证据 ===\n",
                _format_evidence_for_review(results, evidence, gate),
            ]

            if rules:
                prompt_pieces.extend(["\n=== 审查规则 ===\n", rules])

            prompt_pieces.extend([
                "\n\n请逐条审查报告中的核心论断是否有对应证据支撑。"
                "注意：不要因为缺少理想数据就判 fail -- 看已有证据够不够回答用户问题。"
                "\n\n必须只返回合法 JSON object。",
                '{"verdict": "pass|revise|research_more", "reason": "...", '
                '"missing_points": [...], "unsupported_claims": [...]}'
            ])

            prompt = "\n".join(prompt_pieces)

            response = await self.client.chat.completions.create(
                model=self.settings.openai_model,
                messages=[
                    {"role": "system", "content": _CRITIC_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )

            content = response.choices[0].message.content or "{}"
            data = parse_json_object(content, source="Critic")
            critique = Critique.model_validate(data)

            logger.info("Critic verdict: %s (reason: %s)", critique.verdict, critique.reason)

            return {"critique": critique}

        except LLMOutputError as exc:
            logger.warning("Critic LLM output error, defaulting to research_more: %s", exc)
            return {
                "critique": Critique(
                    verdict="research_more",
                    reason=f"Critic LLM error: {exc}",
                    missing_points=["经过 Critic 审计的报告"],
                )
            }
        except Exception as exc:
            logger.exception("Critic node failed")
            return {
                "critique": Critique(
                    verdict="research_more",
                    reason=f"Critic node error: {exc}",
                    missing_points=["经过 Critic 审计的报告"],
                ),
                "errors": [f"Critic failed: {exc}"],
            }
