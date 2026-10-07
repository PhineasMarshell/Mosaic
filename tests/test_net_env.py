"""`NO_PROXY` 里的方括号 IPv6 兼容性（httpx 0.28.1 直接崩）。

锁的是 2026-10-06 实测到的一个真实故障：
`NO_PROXY=localhost,127.0.0.1,::1,[::1]`（Windows 上为 Node/undici 写的常见取值）
会让 `httpx.Client()` **在构造时就抛** `httpx.InvalidURL: Invalid port: ':1]'`，
于是 HTTP 模式全部网关工具 + SEC EDGAR / hk_northbound / news_search 全部在
20ms 内失败，且错误信息只有一句 `Invalid port: ':1]'`。

修法（`app/net_env.py`）：只在**本进程**内把方括号 IPv6 归一化成裸写法，
并在启动日志与 `/health` 里报出来（静默失败绝不可接受）。
"""

import os

import httpx
import pytest

from app.net_env import (
    install_proxy_env_normalization,
    normalize_proxy_environment,
    proxy_env_warnings,
    sanitize_no_proxy,
)

BRACKETED = "localhost,127.0.0.1,::1,[::1]"
FIXED = "localhost,127.0.0.1,::1"


class TestSanitizeNoProxy:
    def test_unwraps_bracketed_ipv6(self):
        fixed, rewritten = sanitize_no_proxy(BRACKETED)
        assert fixed == FIXED
        assert rewritten == ["[::1]"]

    def test_unwraps_full_ipv6(self):
        fixed, rewritten = sanitize_no_proxy("[2001:db8::1],example.com")
        assert fixed == "2001:db8::1,example.com"
        assert rewritten == ["[2001:db8::1]"]

    def test_bare_ipv6_is_untouched(self):
        fixed, rewritten = sanitize_no_proxy("::1,localhost")
        assert fixed == "::1,localhost"
        assert rewritten == []

    def test_no_brackets_is_untouched(self):
        fixed, rewritten = sanitize_no_proxy("localhost,127.0.0.1")
        assert fixed == "localhost,127.0.0.1"
        assert rewritten == []

    def test_strips_whitespace_and_drops_empty_and_duplicates(self):
        fixed, _ = sanitize_no_proxy(" a ,, a ,b ")
        assert fixed == "a,b"

    def test_duplicate_of_unwrapped_form_is_deduped(self):
        fixed, rewritten = sanitize_no_proxy("::1,[::1]")
        assert fixed == "::1"
        assert rewritten == ["[::1]"]


class TestNormalizeProxyEnvironment:
    def test_rewrites_both_spellings(self, monkeypatch):
        monkeypatch.setenv("NO_PROXY", BRACKETED)
        monkeypatch.setenv("no_proxy", BRACKETED)
        warnings = normalize_proxy_environment()
        # Windows 环境变量不区分大小写（两个 key 是同一个条目），POSIX 上是两个 ——
        # 所以数量按平台浮动，但两种拼写查出来都必须是归一化后的值。
        assert warnings != []
        assert os.environ["NO_PROXY"] == FIXED
        assert os.environ["no_proxy"] == FIXED

    def test_is_idempotent(self, monkeypatch):
        monkeypatch.setenv("NO_PROXY", BRACKETED)
        assert normalize_proxy_environment() != []
        assert normalize_proxy_environment() == []

    def test_noop_when_no_brackets(self, monkeypatch):
        monkeypatch.setenv("NO_PROXY", FIXED)
        assert normalize_proxy_environment() == []

    def test_noop_when_unset(self, monkeypatch):
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        assert normalize_proxy_environment() == []


class TestHttpxCompatibility:
    """真实回归：构造 httpx client 本身就是失败点。"""

    def test_bracketed_value_breaks_httpx_client(self, monkeypatch):
        monkeypatch.setenv("NO_PROXY", BRACKETED)
        with pytest.raises(httpx.InvalidURL):
            httpx.Client()

    def test_normalized_value_constructs_httpx_client(self, monkeypatch):
        monkeypatch.setenv("NO_PROXY", BRACKETED)
        assert normalize_proxy_environment() != []
        client = httpx.Client()
        client.close()  # 走到这里就是修好了（此前构造即抛）

    def test_mcp_child_env_gets_normalized_value(self, monkeypatch):
        """MCP 子进程的 env 也是从 os.environ 抄的，必须一并受益。"""
        monkeypatch.setenv("NO_PROXY", BRACKETED)
        normalize_proxy_environment()
        from app.gateway.mcp_client import MarketGatewayClient

        env = MarketGatewayClient._child_env()
        assert env["NO_PROXY"] == FIXED


class TestWarningSurface:
    def test_install_records_warnings_for_health(self, monkeypatch):
        monkeypatch.setenv("NO_PROXY", BRACKETED)
        monkeypatch.setattr("app.net_env._WARNINGS", [], raising=False)
        warnings = install_proxy_env_normalization()
        assert warnings != []
        assert proxy_env_warnings() == warnings

    def test_health_exposes_proxy_env_warnings(self):
        from fastapi.testclient import TestClient

        import app.main as main

        # 不用 with —— 避免触发 lifespan（会初始化 Orchestrator / MCP / 定时任务）
        body = TestClient(main.app).get("/health").json()
        assert isinstance(body["proxy_env_warnings"], list)
