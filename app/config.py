from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: str
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
    research_timeout_seconds: int = 30
    llm_timeout_seconds: int = 90


@lru_cache
def get_settings() -> Settings:
    return Settings()
