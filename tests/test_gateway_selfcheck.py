"""B3/B4：Gateway 通道启动自检测试。

本文件 mock 掉了什么：``app.gateway.mcp_client.MarketGatewayClient`` 被 stub
成可编程假客户端（connect 成功/抛不同异常）——因此不覆盖真实 spawn/握手路径
（那由 tests/test_mcp_handshake.py 的内存流真握手覆盖）。
因此没有覆盖：真实 iiix 子进程行为、真实超时时长。

覆盖：自检通过/失败的通道状态标记、ERROR 日志与可操作提示文案、
fail 模式终止启动、http 模式/自检关闭零开销、/health 的 gateway_channel 字段。
"""

import logging

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.gateway import selfcheck
from app.gateway.mcp_client import MCPConnectionError
from app.gateway.selfcheck import actionable_hint, channel_state, startup_gateway_selfcheck


def _settings(**overrides) -> Settings:
    defaults = {
        "market_gateway_mode": "mcp",
        "gateway_startup_selfcheck": True,
        "gateway_selfcheck_on_failure": "warn",
        "mcp_command": "iiix",
        "mcp_args": "plugin serve market-gateway",
        "openai_api_key": "sk-test",
        "_env_file": None,  # 测试不读真实 .env
    }
    defaults.update(overrides)
    return Settings(**defaults)


class _FakeClient:
    """可编程 stub：connect 行为由类属性决定。"""

    behavior: str = "ok"  # ok / raise:<text> / timeout
    tools: list[str] = ["t1", "t2"]
    instantiate_count: int = 0

    def __init__(self, settings):
        _FakeClient.instantiate_count += 1
        self.settings = settings

    async def connect(self):
        if _FakeClient.behavior == "ok":
            return
        if _FakeClient.behavior == "timeout":
            raise MCPConnectionError("MCP initialize (handshake) timed out (command: iiix plugin serve market-gateway)")
        text = _FakeClient.behavior.removeprefix("raise:")
        raise MCPConnectionError(text)

    async def close(self):
        _FakeClient.closed = True


@pytest.fixture(autouse=True)
def _reset_channel_state():
    _FakeClient.behavior = "ok"
    _FakeClient.instantiate_count = 0
    selfcheck._set_channel_state("not_checked")
    yield
    selfcheck._set_channel_state("not_checked")


@pytest.mark.parametrize(
    ("error_text", "expected_fragment"),
    [
        ("iiix: MCP 已停用: 服务器目录已移除或当前账号无权使用", "plugin serve"),
        (
            "MCP initialize (handshake) failed: Connection closed (command: iiix plugin serve market-gateway)",
            "MCP_ARGS",
        ),
        ("iiix: 登录已失效，请重新执行 iiix login", "iiix login"),
        ("MCP initialize (handshake) timed out (command: iiix …)", "RESEARCH_TIMEOUT_SECONDS"),
    ],
)
def test_actionable_hint_matches_verbatim_failures(error_text, expected_fragment):
    """§1.1 失败文案原文匹配 —— 提示必须可操作，不许泛化成「连接失败」。"""
    assert expected_fragment in actionable_hint(error_text)


async def test_selfcheck_pass_marks_channel_ok(monkeypatch, caplog):
    monkeypatch.setattr("app.gateway.mcp_client.MarketGatewayClient", _FakeClient)
    with caplog.at_level(logging.INFO, logger="app.gateway.selfcheck"):
        await startup_gateway_selfcheck(_settings())
    assert channel_state() == "ok"
    assert any("self-check passed" in r.message for r in caplog.records)


async def test_selfcheck_failure_warn_logs_error_with_hint(monkeypatch, caplog):
    """默认 warn：不抛异常，但必须大声——ERROR 级 + 原始错误 + 可操作提示。"""
    _FakeClient.behavior = "raise:iiix: 登录已失效，请重新执行 iiix login"
    monkeypatch.setattr("app.gateway.mcp_client.MarketGatewayClient", _FakeClient)
    with caplog.at_level(logging.ERROR, logger="app.gateway.selfcheck"):
        await startup_gateway_selfcheck(_settings())  # 不应抛
    assert channel_state() == "mcp_unavailable"
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "自检失败必须打 ERROR 日志（静默失败正是本次事故成因）"
    combined = "\n".join(r.getMessage() for r in errors)
    assert "登录已失效" in combined  # 原始错误保留
    assert "iiix login" in combined  # 可操作提示
    assert "plugin serve market-gateway" in combined  # 带上实际 command/args


async def test_selfcheck_failure_fail_mode_raises(monkeypatch):
    """fail 模式：启动即终止（lifespan 中抛出 → uvicorn 启动失败）。"""
    _FakeClient.behavior = "raise:iiix: MCP 已停用"
    monkeypatch.setattr("app.gateway.mcp_client.MarketGatewayClient", _FakeClient)
    with pytest.raises(RuntimeError, match="self-check failed"):
        await startup_gateway_selfcheck(_settings(gateway_selfcheck_on_failure="fail"))
    assert channel_state() == "mcp_unavailable"


async def test_selfcheck_disabled_is_zero_cost(monkeypatch):
    monkeypatch.setattr("app.gateway.mcp_client.MarketGatewayClient", _FakeClient)
    await startup_gateway_selfcheck(_settings(gateway_startup_selfcheck=False))
    assert channel_state() == "not_checked"
    assert _FakeClient.instantiate_count == 0  # 不许创建客户端


async def test_http_mode_skips_selfcheck(monkeypatch):
    """硬要求：自检不得让 http 模式变慢/失败。"""
    monkeypatch.setattr("app.gateway.mcp_client.MarketGatewayClient", _FakeClient)
    await startup_gateway_selfcheck(_settings(market_gateway_mode="http"))
    assert channel_state() == "not_applicable"
    assert _FakeClient.instantiate_count == 0


async def test_selfcheck_timeout_is_independent_constant():
    """自检超时必须独立于 research_timeout_seconds（否则启动挂 30s）。"""
    assert selfcheck.SELFCHECK_TIMEOUT_SECONDS <= 10.0
    assert selfcheck.SELFCHECK_TIMEOUT_SECONDS != Settings().research_timeout_seconds


def test_health_exposes_gateway_channel():
    """/health 必须暴露 gateway_channel（B3 验收面）。

    刻意**不**用 ``with TestClient(app)``——那会触发 lifespan 的真实 MCP 自检
    （spawn 真实 iiix 子进程）；通道状态直接注入 selfcheck 模块。
    """
    from app.main import app

    selfcheck._set_channel_state("mcp_unavailable")
    try:
        client = TestClient(app)
        payload = client.get("/health").json()
    finally:
        selfcheck._set_channel_state("not_checked")
    assert payload["gateway_channel"] == "mcp_unavailable"
    assert "gateway_mode" in payload
