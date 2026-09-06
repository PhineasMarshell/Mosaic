"""Logging — Mosaic 全局日志配置。

所有关键路径都有结构化日志输出：
- Agent: Planner → Tool Execution → Evidence Gate → Evaluator → Reasoning
- Gateway: MCP/HTTP call details, errors, retries
- Research: MarketDetective loop iterations

默认 JSON 格式，可切换为 human-readable。

用法：

    python -m app.cli "今天A股为什么这么弱？"

日志输出到 stdout（stderr 仅用于异常）。
"""

import json
import logging
import sys
import time
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    """JSON 格式日志记录器，适合机器解析。"""

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if hasattr(record, "extra_log"):
            log_data.update(record.extra_log)  # type: ignore[union-attr]
        if record.exc_info:
            log_data["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(log_data, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    """人类可读的简洁日志格式。"""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
        level = record.levelname[:4]
        msg = record.getMessage()

        # 注入额外字段
        extra = getattr(record, "extra_log", {})
        if extra:
            parts = [f"{k}={v}" for k, v in extra.items() if isinstance(v, (str, int, float, bool))]
            if parts:
                msg += f" [{', '.join(parts)}]"

        logger_name = record.name.split(".")[-1]
        return f"[{ts}] {level} {logger_name}: {msg}"


def setup_logging(level: str = "INFO", json_format: bool | None = None) -> None:
    """
    配置 Mosaic 全局日志系统。

    Args:
        level: 日志级别 (DEBUG/INFO/WARNING/ERROR)
        json_format: True=JSON 输出, False=human-readable, None=自动检测
                      (TTY→human, pipe→json)
    """
    if json_format is None:
        json_format = not sys.stdout.isatty()

    formatter = JSONFormatter() if json_format else HumanFormatter()

    root = logging.getLogger("mosaic")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root.addHandler(handler)

    # Suppress noisy third-party logs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)
    logging.getLogger("anyio").setLevel(logging.WARNING)

    logging.info("Logging initialized: level=%s json=%s", level, json_format)


def get_logger(name: str) -> logging.Logger:
    """获取 Mosaic 命名空间的 logger。"""
    return logging.getLogger(f"mosaic.{name}")


class _timing_context:
    """异步上下文管理器，自动记录执行耗时。"""

    def __init__(self, logger: logging.Logger, label: str) -> None:
        self.logger = logger
        self.label = label
        self._start: float = 0

    async def __aenter__(self):
        self._start = time.monotonic()
        self.logger.info("Starting %s", self.label)
        return self

    async def __aexit__(self, *args):
        elapsed_ms = (time.monotonic() - self._start) * 1000
        extra = getattr(self.logger.makeRecord(
            self.logger.name, logging.DEBUG, "", 0,
            "Completed %s", [], None
        ), "extra_log", None)

        record = self.logger.makeRecord(
            self.logger.name, logging.INFO, "", 0,
            "Completed %s", [self.label], None
        )
        if not hasattr(record, "extra_log"):
            object.__setattr__(record, "extra_log", {})
        record.extra_log["elapsed_ms"] = round(elapsed_ms, 1)
        self.logger.handle(record)


# ---------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------

def log_timing(logger: logging.Logger, label: str):
    """返回一个同步计时上下文管理器。"""
    return _simple_timing_context(logger, label)


class _simple_timing_context:
    """同步计时上下文。"""

    def __init__(self, logger: logging.Logger, label: str):
        self.logger = logger
        self.label = label
        self._start = 0.0

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, *args):
        elapsed_ms = (time.monotonic() - self._start) * 1000
        self.logger.info(
            "%s completed in %.1fms",
            self.label, elapsed_ms,
        )
