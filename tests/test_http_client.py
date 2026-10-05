"""HTTP 网关客户端本地校验单元测试 —— 不 mock 任何东西、不发真实请求：
直接构造 MarketGatewayHttpClient 验证本地逻辑（API key 校验等）。
未覆盖：HTTP 请求/重试/预算行为由 test_http_client_budget.py 打桩覆盖。"""

import pytest

from app.config import Settings
from app.gateway.http_client import MarketGatewayHttpClient


@pytest.mark.asyncio
async def test_http_mode_requires_api_key():
    settings = Settings(
        openai_api_key="test",
        market_gateway_mode="http",
        market_gateway_api_key="",  # 强制为空，绕过 .env
    )
    client = MarketGatewayHttpClient(settings)

    try:
        await client.__aenter__()
        raise AssertionError("Should have raised RuntimeError")
    except RuntimeError as e:
        assert "MARKET_GATEWAY_API_KEY" in str(e)
