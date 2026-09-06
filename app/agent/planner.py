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

import json
from openai import AsyncOpenAI

from app.agent.prompts import PLANNER_PROMPT
from app.config import Settings
from app.gateway.tool_registry import registry_text, tools_by_domain
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

    async def plan(self, question: str) -> ResearchPlan:
        """为给定问题生成研究计划。

        行为：
        1. 向 Planner 暴露全部已注册工具（让 LLM 自主选择）
        2. 将注册表与问题一起发送给 LLM
        3. 解析返回的 JSON 为 ResearchPlan
        4. 限制步骤数不超过配置上限
        """
        # 向 LLM 暴露所有工具，LLM 根据问题描述自主判断哪些可用
        registry = registry_text()

        prompt = PLANNER_PROMPT.format(
            question=question,
            registry=registry,
            max_steps=self.settings.max_research_steps,
            enabled_domains=", ".join(_ENABLED_DOMAINS),
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

        content = response.choices[0].message.content or "{}"

        # ── JSON 提取：剥离可能的 Markdown 代码块 ──
        import re
        cleaned = None
        # 尝试 ````json ... ``` 或 ```` ... ```
        match = re.search(r"(?:```(?:json)?\s*?\n)([\s\S]*?)(?:```)", content)
        if match:
            cleaned = match.group(1).strip()
        else:
            # 直接就是纯 JSON
            first_brace = content.find("{")
            last_brace = content.rfind("}")
            if first_brace != -1 and last_brace > first_brace:
                cleaned = content[first_brace:last_brace + 1]
            else:
                cleaned = content.strip()

        try:
            data = json.loads(cleaned or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Planner returned invalid JSON:\nraw={content!r}\nerror={exc}"
            ) from exc

        plan = ResearchPlan.model_validate(data)
        plan.steps = plan.steps[: self.settings.max_research_steps]
        return plan
