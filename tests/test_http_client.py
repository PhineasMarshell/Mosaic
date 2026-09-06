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
        assert False, "Should have raised RuntimeError"
    except RuntimeError as e:
        assert "MARKET_GATEWAY_API_KEY" in str(e)
