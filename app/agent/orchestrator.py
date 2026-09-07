from app.config import Settings
from app.models.research import MarketDomain
from app.research.market_detective import MarketDetective
from app.models.response import ResearchResponse


class Orchestrator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_detective = MarketDetective(settings)

    async def run(
        self, question: str, domain: MarketDomain | None = None,
        conversation_id: str | None = None,
    ) -> ResearchResponse:
        """运行研究调查。

        Args:
            question: 用户问题
            domain: 可选的强制市场域；None 表示让 Planner 自动判断
            conversation_id: 可选的对话会话 ID，用于注入历史上下文
        """
        question = question.strip()
        if not question:
            raise ValueError("question cannot be empty")
        return await self.market_detective.investigate(
            question, domain=domain, conversation_id=conversation_id
        )
