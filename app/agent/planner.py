"""Planner — 用户问题 → 可执行研究计划。

多域设计：
- Planner 根据用户问题自动判断目标市场域（domain）
- 只向 Planner 暴露相关域的工具注册表
- 新增市场域只需在 tool_registry 中注册工具并更新 prompt 规则

工作流程：
    用户问题
      → LLM 判断 domain + task
      → 按域过滤 Tool Registry
      → 生成步骤序列（宽后窄）
      → 返回 ResearchPlan
"""

from openai import AsyncOpenAI
from pydantic import ValidationError

from app.agent.prompts import PLANNER_PROMPT
from app.config import Settings
from app.errors import LLMOutputError
from app.gateway.tool_registry import registry_text, tools_by_domain
from app.llm_json import parse_json_object
from app.models.research import DEFAULT_DOMAINS, MarketDomain, ResearchPlan


# 默认全部启用；后续可通过配置关闭某些域
_ENABLED_DOMAINS: list[MarketDomain] = DEFAULT_DOMAINS


def set_enabled_domains(domains: list[MarketDomain]) -> None:
    """动态设置启用的市场域（供外部覆盖）。"""
    global _ENABLED_DOMAINS
    _ENABLED_DOMAINS = domains


class Planner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=settings.llm_timeout_seconds,
        )

    async def plan(self, question: str, conversation_history: str = "") -> ResearchPlan:
        """为给定问题生成研究计划。

        Args:
            question: 用户问题
            conversation_history: 可选的对话历史文本，让 Planner 能感知之前轮次
        """
        # 向 LLM 暴露所有工具，LLM 根据问题描述自主判断哪些可用
        registry = registry_text()

        prompt = PLANNER_PROMPT.format(
            question=question,
            registry=registry,
            max_steps=self.settings.max_research_steps,
            enabled_domains=", ".join(_ENABLED_DOMAINS),
            conversation_history=conversation_history if conversation_history else "（无）",
        )

        response = await self.client.chat.completions.create(
            model=self.settings.openai_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是 Mosaic 的 Planner。"
                        "必须只返回合法 JSON object，不要包含任何 Markdown 代码块标记、"
                        "不要加开头或结尾的文字解释。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )

        content = response.choices[0].message.content

        # 以前是 `content or "{}"` + 一段内联的围栏剥离：空 completion 会被变成
        # 一个合法的空计划，而 ResearchPlan 全字段有默认值 → 校验通过 →
        # domain 静默变成 "a_share"，接着触发 9 个 A 股兜底工具。
        # qwen3.7-flash 是 thinking model，思考阶段被截断时 content 就是空的。
        data = parse_json_object(content, source="Planner")

        try:
            plan = ResearchPlan.model_validate(data)
        except ValidationError as exc:
            # 模型吐的 JSON 语法合法但不符合 schema —— 同样是上游故障，不是客户端的错
            raise LLMOutputError(f"Planner returned JSON that violates the schema: {exc}") from exc

        plan.steps = plan.steps[: self.settings.max_research_steps]
        return plan
