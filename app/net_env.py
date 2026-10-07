"""代理环境变量的兼容性归一化。

背景（实测 2026-10-06，httpx 0.28.1 / Python 3.14.6）：
Windows 上很常见一份**为 Node/undici 准备**的 ``NO_PROXY``：
``localhost,127.0.0.1,::1,[::1]``。httpx 会在**构造 ``Client`` 时**
把 ``NO_PROXY`` 的每一项当作 URL pattern 解析（``httpx._client`` → ``URLPattern(key)``），
于是带方括号且不带 scheme 的 IPv6（``[::1]``）直接抛::

    httpx.InvalidURL: Invalid port: ':1]'

client 根本构造不出来 —— 表现是 HTTP 模式下**所有**网关工具、以及 SEC EDGAR /
hk_northbound / news_search 这些内部直连工具**全部在 20ms 内失败**，
而错误信息只有一句 ``Invalid port: ':1]'``，极难定位（测试套件全绿也掩盖不了它，
因为测试不依赖真实网络）。

实测边界：``::1``（不带方括号）完全正常，**只有 ``[::1]`` 这种写法会崩**。

修法：**只归一化当前进程**的环境变量（不写注册表、不改用户机器、不影响其他程序），
并把归一化的事实作为告警回传，供启动日志与 ``/health`` 暴露 ——
「静默失败绝不可接受」。
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

#: 需要检查的环境变量名（Windows 环境变量不区分大小写，两种拼写都收）
PROXY_ENV_KEYS = ("NO_PROXY", "no_proxy")

#: 形如 ``[::1]`` / ``[2001:db8::1]`` 的裸方括号主机（不带 scheme、不带端口）
_BRACKETED_HOST = re.compile(r"^\[([0-9A-Fa-f:.]+)\]$")

#: 最近一次 ``install_proxy_env_normalization()`` 的告警（供 ``/health`` 暴露）
_WARNINGS: list[str] = []


def sanitize_no_proxy(value: str) -> tuple[str, list[str]]:
    """把 ``NO_PROXY`` 取值里的方括号 IPv6 归一化成裸写法。

    顺带丢掉空条目与重复条目（仅当调用方决定采纳返回值时才会生效）。

    Returns:
        ``(归一化后的取值, 被改写的原始条目)``。
    """
    rewritten: list[str] = []
    normalized: list[str] = []
    for raw in value.split(","):
        entry = raw.strip()
        if not entry:
            continue
        match = _BRACKETED_HOST.match(entry)
        if match:
            rewritten.append(entry)
            entry = match.group(1)
        if entry not in normalized:
            normalized.append(entry)
    return ",".join(normalized), rewritten


def normalize_proxy_environment() -> list[str]:
    """就地归一化本进程的 ``NO_PROXY`` / ``no_proxy``。

    Returns:
        人类可读的告警列表；空列表 = 无需处理（没设置、没有方括号条目）。
    """
    warnings: list[str] = []
    for key in PROXY_ENV_KEYS:
        raw = os.environ.get(key)
        if not raw or "[" not in raw:
            continue
        fixed, rewritten = sanitize_no_proxy(raw)
        if not rewritten or fixed == raw:
            continue
        os.environ[key] = fixed
        warnings.append(f"{key} 含 httpx 无法解析的方括号 IPv6 {rewritten}，已在本进程内归一化为 {fixed!r}")
    return warnings


def install_proxy_env_normalization() -> list[str]:
    """归一化并把告警写进日志 + 模块级缓存（幂等，可重复调用）。"""
    global _WARNINGS
    warnings = normalize_proxy_environment()
    if warnings:
        _WARNINGS = warnings
        for item in warnings:
            logger.error(
                "Proxy environment normalized: %s — httpx 会把 NO_PROXY 的每一项当 URL 解析，"
                "方括号 IPv6 会抛 httpx.InvalidURL: Invalid port: ':1]'，"
                "导致 HTTP 模式与全部内部直连工具失败",
                item,
            )
    return warnings


def proxy_env_warnings() -> list[str]:
    """返回最近一次归一化产生的告警（通常为空列表）。"""
    return list(_WARNINGS)
