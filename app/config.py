from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.net_env import install_proxy_env_normalization

#: `NO_PROXY` 里形如 `[::1]` 的方括号 IPv6 会让 httpx **构造 client 就崩**
#: （`InvalidURL: Invalid port: ':1]'`），HTTP 模式与内部直连工具会全线失败。
#: 这里是一次「唯一必经点」安装：任何要发网络请求的进程都会先 import app.config，
#: 早于任何 httpx client 的构造。只归一化本进程环境变量，不动用户机器。
install_proxy_env_normalization()


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
    #: iiix CLI >= 0.8.0 的语法：`plugin serve <project-code>`。
    #: 旧语法 `mcp serve market-gateway` 在 0.8.0 已被删除（`iiix mcp` 不再存在）。
    mcp_args: str = "plugin serve market-gateway"

    #: 启动时是否对 Gateway 通道做一次自检（mcp 模式：spawn + 握手 + list_tools）。
    #: 背景：2026-10-06 事故——mcp 模式全部 Gateway 工具失败但启动无任何报错。
    gateway_startup_selfcheck: bool = True
    #: 自检失败时的行为：warn（默认，只记 ERROR 日志 + /health 的 gateway_channel 标记）
    #: / fail（启动即终止）。本地开发不该因网关/登录挂了起不来，但静默失败不可接受。
    gateway_selfcheck_on_failure: str = "warn"

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
    #: news_search 的 TTL 分档（A2.4）：time_limit=d 是「今天的新闻」，
    #: 缓存 6 小时是错误语义 —— d/w/m 各自设档。
    news_ttl_day_seconds: int = 1800
    news_ttl_week_seconds: int = 21600
    news_ttl_month_seconds: int = 86400
    #: internal_symbol_news（东财个股新闻 + Google 资讯聚合）
    news_symbol_ttl_seconds: int = 900
    #: internal_market_telegraph（财联社电报快讯，变化快）
    news_telegraph_ttl_seconds: int = 300
    #: internal_news_digest（Google + DDGS 主题聚合）
    news_digest_ttl_seconds: int = 900
    #: 全A code↔name 名称表（首载实测约 5-6s，长 TTL 摊销）
    news_code_name_ttl_seconds: int = 86400
    #: 单条新闻正文进 datum 的截断长度
    news_max_text_chars: int = 800
    #: 单个新闻源（Google RSS / 东财 / 电报）的抓取超时
    news_source_timeout_seconds: int = 15

    # ── SEC EDGAR（美股基本面） ────────────────────
    #: SEC 公平访问政策要求所有请求声明访问身份，格式 "YourName your@email.com"。
    #: 留空 = 美股基本面工具（us_fundamentals/us_filings_recent）直接返回 error。
    sec_edgar_contact: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
