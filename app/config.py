from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-5-mini"

    # Market Gateway 接入模式：mcp / http
    market_gateway_mode: str = "mcp"

    mcp_command: str = "iiix"
    mcp_args: str = "mcp serve market-gateway"

    # HTTP 模式使用 X-API-Key；不要把真实 key 写入代码或提交到 Git。
    market_gateway_http_url: str = "https://api.x.iiix.dev/v1/d/market-gateway"
    market_gateway_api_key: str = ""

    max_tool_calls: int = 12
    max_research_steps: int = 8
    max_retry_per_tool: int = 1

    #: 单次 MCP/HTTP 工具调用的超时；同时用于 MCP 启动握手与 list_tools。
    research_timeout_seconds: int = 30

    #: 一次完整调查的总预算（planner LLM + N 个工具 + evidence gate + reasoning LLM）。
    #: 必须显著大于 research_timeout_seconds —— 否则 HTTP 层的 wait_for 会在
    #: 研究跑完之前掐断它，然后把同样的活从头再跑一遍（历史上就是这么雪崩的）。
    #: 经验值：≥ llm_timeout_seconds * 2 + max_tool_calls * 单次工具均时。
    research_budget_seconds: int = 300

    #: SSE 心跳间隔。调查期间没有新事件时，每隔这么久推一条 progress，
    #: 防止浏览器/代理把静默连接当成死连接掐掉。
    stream_heartbeat_seconds: int = 15

    llm_timeout_seconds: int = 90
    max_conversation_turns: int = 10

    #: Market Memory 的 SQLite 路径。留空 = 默认落在"项目根/memory/memory.db"
    #: （本地开发的历史行为）。容器/只读代码目录部署时用 `MOSAIC_MEMORY_DB`
    #: 指到可写卷上，例如 `/data/memory.db`。
    mosaic_memory_db: str = ""

    #: 运行期数据目录（定时简报 JSON 等）。留空 = 默认"项目根/memory"。
    #: 容器部署时用 `MOSAIC_DATA_DIR=/data`，否则非 root 进程写不进 site-packages 之外。
    mosaic_data_dir: str = ""

    # ── LangGraph 图配置（P2+） ────────────────────
    #: Critic 打回 Reasoning 重写的最大轮次
    critic_max_revisions: int = 2
    #: LangGraph recursion limit（防止无限循环）
    graph_recursion_limit: int = 25

    # ── P4：舆情 / 新闻 ────────────────────
    #: 舆情分析员总开关（评论 MCP 就绪后置 true）
    sentiment_enabled: bool = False
    #: 单次拉取评论上限
    sentiment_max_comments: int = 500
    #: 新闻分析员总开关
    news_enabled: bool = False
    #: DDGS 搜索结果缓存时长（秒），缓解限流
    news_search_ttl_seconds: int = 21600

    # ── SEC EDGAR（美股基本面） ────────────────────
    #: SEC 公平访问政策要求所有请求声明访问身份，格式 "YourName your@email.com"。
    #: 留空 = 美股基本面工具（us_fundamentals/us_filings_recent）直接返回 error。
    sec_edgar_contact: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
