"""Supervisor 节点 — LLM 路由：解析意图 + 选 analyst + 分配工具预算。

复用 PLANNER_PROMPT + llm_json.parse_json_object，
输出 ResearchState.intent + route，被各 analyst 节点消费。
"""

from openai import AsyncOpenAI

from app.agent.prompts_graph import PLANNER_PROMPT
from app.config import Settings
from app.gateway.tool_registry import registry_text
from app.llm_json import parse_json_object
from app.models.research import DEFAULT_DOMAINS, ResearchPlan

# 默认全部启用；后续可通过配置关闭某些域
_ENABLED_DOMAINS: list = DEFAULT_DOMAINS


def set_enabled_domains(domains: list) -> None:
    """动态设置启用的市场域（供外部覆盖）。"""
    global _ENABLED_DOMAINS
    _ENABLED_DOMAINS = domains


def route_candidate_categories(settings) -> tuple[str, ...]:
    """返回当前启用的 analyst category 元组。

    supervisor._build_route 分组与 builder._supervisor_fanout 扇出共用此函数，
    避免两处写死白名单。news/sentiment 开关关闭时，对应 category 的工具
    会被 _build_route 归入 technical（维持 P2.5 行为）。
    """
    cats = ["technical", "fundamental", "moneyflow"]
    if getattr(settings, "news_enabled", False):
        cats.append("news")
    # Sentiment 节点尚未实现；在 builder.add_node("sentiment", ...) 前不能把它
    # 暴露为可路由目标，否则条件边会指向未知节点并导致建图失败。
    return tuple(cats)


class SupervisorNode:
    """LLM 路由节点：用户问题 → intent + route。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=settings.llm_timeout_seconds,
        )

    async def __call__(self, state):
        """执行路由决策，返回要写入 state 的字段字典。"""
        try:
            # Normalize state to dict (handle both ResearchState and dict)
            if hasattr(state, "model_dump"):
                s = state.model_dump(exclude_none=False)
            else:
                s = state
            plan = await self._plan(s)
            return {
                "intent": plan.intent,
                "route": self._build_route(plan),
            }
        except Exception as exc:
            # 路由失败写进 errors，kernel 仍可继续尝试（只是没有显式路由）
            return {"errors": [f"Supervisor routing failed: {exc}"]}

    async def _plan(self, state):
        """生成研究计划（包装 Planner.plan 逻辑）。"""
        conv_history = ""
        conv_id = state.get("conversation_id") if isinstance(state, dict) else getattr(state, "conversation_id", None)
        if conv_id:
            from app.memory.storage import get_memory

            memory = get_memory()
            conv_history = memory.get_conversation_history(conv_id, self.settings.max_conversation_turns)

        registry = registry_text()
        question = state.get("question", "") if isinstance(state, dict) else getattr(state, "question", "")

        prompt = PLANNER_PROMPT.format(
            question=question,
            registry=registry,
            max_steps=self.settings.max_research_steps,
            enabled_domains=", ".join(_ENABLED_DOMAINS),
            conversation_history=conv_history if conv_history else "（无）",
        )

        response = await self.client.chat.completions.create(
            model=self.settings.openai_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是 Mosaic 的 Supervisor。"
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
        data = parse_json_object(content, source="Supervisor")

        plan = ResearchPlan.model_validate(data)

        # 如果显式指定了域且 planner 自动判断不一致，以显式指定为准
        explicit_domain = state.get("domain") if isinstance(state, dict) else getattr(state, "domain", None)
        if explicit_domain is not None:
            plan.intent.domain = explicit_domain

        # 截断步骤数
        plan.steps = plan.steps[: self.settings.max_research_steps]
        return plan

    def _build_route(self, plan: ResearchPlan) -> list:
        """将 ResearchPlan.steps 按工具 category 分组为 AnalystAssignment 列表。"""
        from app.gateway.tool_registry import resolve_tool

        groups: dict[str, list] = {}
        allowed = route_candidate_categories(self.settings)
        for step in plan.steps:
            try:
                meta = resolve_tool(step.tool_key)
                cat = meta.category
            except Exception:
                cat = "technical"  # 无法解析的工具键兜底给技术面
            if cat not in allowed:
                cat = "technical"  # 未启用的 category 暂归技术面
            groups.setdefault(cat, []).append(step)
        return [
            {"analyst": cat, "tool_calls": [s.model_dump() for s in steps], "budget": len(steps)}
            for cat, steps in groups.items()
        ]
