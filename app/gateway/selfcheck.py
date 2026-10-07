"""Gateway 通道启动自检（B3）。

背景：2026-10-06 事故——默认 mcp 模式下全部 Gateway 工具 100% 失败，但启动
没有任何报错（575 个测试全绿、线上全挂）。本模块让通道故障在启动时就「大声」
可见：ERROR 级日志 + /health 的 ``gateway_channel`` 标记。

设计（用户裁定）：自检失败默认 ``warn`` —— 不阻塞启动，但必须大声。
理由：本地开发不该因为网关/登录挂了起不来，但「静默失败」绝不可接受。

本文件 mock 说明：单元测试（tests/test_gateway_selfcheck.py）会 monkeypatch
``MarketGatewayClient`` 为 stub —— 因此不覆盖真实 spawn/握手路径（那由
tests/test_mcp_handshake.py 的内存流真握手与 Phase C 真链路验证覆盖）。
"""

import asyncio
import logging
import time

from app.config import Settings

logger = logging.getLogger(__name__)

#: 自检超时**独立于** ``research_timeout_seconds``（30s）——启动最多等 10s，
#: 不然网关挂掉时每次启动都要白等一个完整的研究超时。
SELFCHECK_TIMEOUT_SECONDS = 10.0

#: gateway_channel 的可能取值：ok / mcp_unavailable / not_checked（自检关闭）/ not_applicable（http 模式）
_channel_state: str = "not_checked"


def channel_state() -> str:
    """当前 Gateway 通道状态（/health 的 ``gateway_channel`` 字段来源）。"""
    return _channel_state


def _set_channel_state(state: str) -> None:
    global _channel_state
    _channel_state = state


def actionable_hint(error_text: str) -> str:
    """把 MCP 自检的原始报错映射成可操作的排查提示。

    用 §1.1 失败文案的**原文**匹配，不泛化成「连接失败」——运维看到提示就能动手。
    """
    if "MCP 已停用" in error_text or "Connection closed" in error_text:
        return "iiix CLI ≥ 0.8.0 请用 `plugin serve`，并确认 MCP_ARGS（如 `plugin serve market-gateway`）"
    if "登录已失效" in error_text:
        return "执行 `iiix login`，再用 `iiix plugin verify market-gateway` 确认"
    if "timed out" in error_text:
        return "检查 RESEARCH_TIMEOUT_SECONDS 与网络（HTTP_PROXY/HTTPS_PROXY/NO_PROXY 代理变量）"
    return "用 `iiix plugin verify market-gateway` 自检（登录态 + 上游连通性一起验）"


async def run_mcp_selfcheck(settings: Settings) -> tuple[bool, str]:
    """真实自检：spawn 子进程 + initialize 握手 + list_tools。

    Returns:
        (是否通过, 失败时的错误摘要；通过时为空串)
    """
    from app.gateway.mcp_client import MarketGatewayClient

    client = MarketGatewayClient(settings)
    started = time.monotonic()
    try:
        await asyncio.wait_for(client.connect(), timeout=SELFCHECK_TIMEOUT_SECONDS)
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.info("Gateway MCP self-check passed: %d tools (%.0fms)", len(client.tools), elapsed_ms)
        return True, ""
    except Exception as exc:  # noqa: BLE001 自检必须吞掉一切失败，转为状态标记
        text = str(exc)
        logger.error(
            "Gateway MCP self-check FAILED after %.1fs (command: %s %s): %s\n→ %s",
            time.monotonic() - started,
            settings.mcp_command,
            settings.mcp_args,
            text,
            actionable_hint(text),
        )
        return False, text
    finally:
        await client.close()


async def startup_gateway_selfcheck(settings: Settings) -> None:
    """lifespan 入口：按配置执行通道自检。

    - http 模式：不做 MCP 自检（硬要求：不得让 http 模式变慢/失败）。
    - 自检关闭：只标记 not_checked。
    - warn（默认）：失败只记 ERROR 日志 + /health 标记，不阻塞启动。
    - fail：失败抛 RuntimeError 终止启动。
    """
    if settings.market_gateway_mode.lower() != "mcp":
        _set_channel_state("not_applicable")
        return
    if not settings.gateway_startup_selfcheck:
        _set_channel_state("not_checked")
        logger.info("Gateway MCP startup self-check disabled (gateway_startup_selfcheck=false)")
        return

    passed, error = await run_mcp_selfcheck(settings)
    if passed:
        _set_channel_state("ok")
        return
    _set_channel_state("mcp_unavailable")
    if settings.gateway_selfcheck_on_failure == "fail":
        raise RuntimeError(f"Gateway MCP self-check failed (gateway_selfcheck_on_failure=fail): {error}")
    # warn（默认）：不阻塞启动——「大声」已由 run_mcp_selfcheck 的 ERROR 日志
    # 与这里的 /health 标记（mcp_unavailable）完成。
