"""Critic 节点 -- 对 Report + Evidence 进行审计评估。

输出 Critique（pass / revise / research_more）：
- pass: 证据充分，报告可信 -> END
- revise: 有 unsupported claims -> 回 Reasoning 重写（<= max_revisions 轮）
- research_more: 有 missing points -> 带缺口感召 Supervisor 补充研究（最多 1 次）

P2 验收：Critic 条件边路由正确；打回重写的完整路径有测试。
"""

import logging
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.errors import LLMOutputError
from app.llm_json import parse_json_object

logger = logging.getLogger(__name__)

#: Critic 合法裁决。额外的 "error" 不允许模型返回，仅由本节点在「审计自身失败 /
#: verdict 无法识别」时内部产生，路由层据此安全终止，而不是当成 pass 或 research_more。
Verdict = Literal["pass", "revise", "research_more", "error"]


class Critique(BaseModel):
    """Critic 评估结论。"""

    verdict: Verdict
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


def _field(obj, key, default=None):
    """从 dict 或对象读取字段 —— LangGraph 可能传入任一形式。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _get_domain_rules(domain: str) -> str:
    return _DOMAIN_RULES.get(domain, "")


def _format_report_for_review(report) -> str:
    """将报告格式化为人类可读的审查文本。"""
    parts = [
        f"问题: {str(_field(report, 'what_happened', 'N/A'))[:200]}",
        f"置信度: {_field(report, 'confidence', 'N/A')}",
        f"市场状态: {_field(report, 'state_label', 'N/A')}",
    ]
    strong_areas = _field(report, "strong_areas") or []
    if strong_areas:
        parts.append(f"强势方向: {', '.join(str(x) for x in strong_areas)}")
    risks = _field(report, "risks") or []
    if risks:
        parts.append("风险:\n" + "\n".join(f"  - {r}" for r in risks))
    why = _field(report, "why") or []
    if why:
        parts.append("原因分析:\n" + "\n".join(f"  - {w}" for w in why))
    return "\n".join(parts)


def _format_evidence_for_review(results, evidence, gate) -> str:
    """格式化证据用于审查。"""
    lines = ["=== 成功工具 ==="]
    if gate:
        for t in _field(gate, "successful_tools", []) or []:
            lines.append(f"  OK {t}")
        for t in _field(gate, "partial_tools", []) or []:
            lines.append(f"  ~ partial: {t}")
        for err in _field(gate, "error_tools", []) or []:
            if isinstance(err, dict):
                lines.append(f"  FAIL {err.get('tool')}: {err.get('error', '?')}")
            else:
                lines.append(f"  FAIL {getattr(err, 'tool', '?')}: {getattr(err, 'error', '?')}")
    lines.append("")
    # Add some normalize data summary (limited length to avoid prompt overflow)
    for result in (results or [])[:20]:
        normalized = _field(result, "normalized", []) or []
        for datum in normalized[:10]:
            metric = _field(datum, "metric", "?")
            value = str(_field(datum, "value", ""))[:80]
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
            domain = _field(intent, "domain") or state.get("domain") or "a_share"

            rules = _get_domain_rules(domain)

            # Assemble prompt pieces
            prompt_pieces = [
                f"用户问题: {state.get('question', '')}\n",
                f"目标域: {domain}\n\n",
                "=== 待审查的报告 ===\n",
                _format_report_for_review(report),
                "\n\n=== 实际证据 ===\n",
                _format_evidence_for_review(results, evidence, gate),
            ]

            if rules:
                prompt_pieces.extend(["\n=== 审查规则 ===\n", rules])

            prompt_pieces.extend(
                [
                    "\n\n请逐条审查报告中的核心论断是否有对应证据支撑。"
                    "注意：不要因为缺少理想数据就判 fail -- 看已有证据够不够回答用户问题。"
                    "\n\n必须只返回合法 JSON object。",
                    '{"verdict": "pass|revise|research_more", "reason": "...", '
                    '"missing_points": [...], "unsupported_claims": [...]}',
                ]
            )

            prompt = "\n".join(prompt_pieces)

            response = await self.client.chat.completions.create(
                model=self.settings.openai_model,
                messages=[
                    {"role": "system", "content": "你是 Mosaic 的 Critic，只返回合法 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )

            content = response.choices[0].message.content or "{}"
            data = parse_json_object(content, source="Critic")

            # T11：verdict 先归一化（去空白 + 小写），再做校验。
            raw_verdict = str(data.get("verdict", "")).strip().lower()
            data["verdict"] = raw_verdict
            if raw_verdict not in ("pass", "revise", "research_more"):
                # 非法 / 无法识别的 verdict：安全终止并记 errors，
                # 绝不能像旧逻辑那样落到路由默认 end（= 静默当 pass），
                # 也不伪造 research_more 再烧一到两轮完整工具 + LLM。
                logger.error("Critic verdict 无法识别 %r，安全终止", raw_verdict)
                return {
                    "critique": Critique(
                        verdict="error",
                        reason=f"Unrecognized critic verdict: {raw_verdict!r}",
                    ),
                    "errors": [f"Critic 返回了无法识别的 verdict: {raw_verdict!r}，已安全终止"],
                }

            try:
                critique = Critique.model_validate(data)
            except ValidationError as exc:
                logger.error("Critic 输出校验失败，安全终止: %s", str(exc)[:300])
                return {
                    "critique": Critique(
                        verdict="error",
                        reason=f"Invalid critic payload: {exc}",
                    ),
                    "errors": [f"Critic 输出无法解析为合法结论: {str(exc)[:200]}"],
                }

            logger.info("Critic verdict: %s (reason: %s)", critique.verdict, critique.reason)

            return {"critique": critique}

        except LLMOutputError as exc:
            # T11：审计自身失败（LLM 超时 / JSON 坏）不再返回 research_more——
            # 那会触发一到两轮完整工具 + LLM（烧钱）且伪造 missing_points。
            # 改为内部 error verdict 安全终止，只保留 errors。
            logger.error("Critic LLM 输出不可用，安全终止: %s", exc)
            return {
                "critique": Critique(verdict="error", reason=f"Critic audit failed: {exc}"),
                "errors": [f"Critic audit failed: {exc}"],
            }
        except Exception as exc:
            logger.exception("Critic node failed，安全终止")
            return {
                "critique": Critique(verdict="error", reason=f"Critic audit failed: {exc}"),
                "errors": [f"Critic audit failed: {exc}"],
            }
