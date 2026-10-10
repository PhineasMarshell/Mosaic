"""统一的研究结果持久化——同步 ``/api/ask`` 与 SSE ``/api/ask/stream`` 共用。

T3 背景：此前两条路径各写一份持久化逻辑。SSE 侧把
``ResearchResponse.model_dump()`` 当成"扁平 report dict"读取
（``state_label`` / ``strong_areas`` / …），但这些字段实际嵌套在 ``report``
键下，顶层并不存在，于是：

- ``conversations.answer_summary`` 恒为 ``""``，多轮上下文形同虚设；
- 当日 ``daily_states`` 被写入空值，由于 ``save_daily_state`` 是合并语义，
  空值会覆盖当天先前写入的正确快照；
- SSE 路径完全没写 ``market_state``（同步路径有写）。

本模块一律从 ``result.report`` 读取字段，消除双路径漂移。
"""

import asyncio
import logging
import sqlite3
from contextlib import nullcontext

from app.graph import run_log
from app.models.response import MarketIntelligence, ResearchResponse

logger = logging.getLogger(__name__)


def _retry_options(max_retries: int | None, backoff_ms: int | None) -> tuple[int, int]:
    """Read bounded persistence retry settings without making config mandatory."""
    if max_retries is None or backoff_ms is None:
        try:
            from app.config import get_settings

            settings = get_settings()
        except Exception:  # noqa: BLE001 - persistence still has safe defaults
            settings = None
        if max_retries is None:
            max_retries = getattr(settings, "persistence_max_retries", 2)
        if backoff_ms is None:
            backoff_ms = getattr(settings, "persistence_retry_backoff_ms", 25)
    return max(0, min(5, int(max_retries))), max(0, min(1000, int(backoff_ms)))


def _is_transient_sqlite_lock(exc: BaseException) -> bool:
    """Only SQLite lock contention is safe to retry after transaction rollback."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _answer_summary(report: MarketIntelligence) -> str:
    """构造对话轮次的答案摘要（同步路径原有形态）。"""
    what = report.what_happened or ""
    if what:
        return f"{report.state_label}：{what[:80]}…"
    return report.state_label or ""


def _daily_state_data(report: MarketIntelligence) -> dict:
    """构造当日状态快照（保持 briefs 依赖的扁平字段形状不变）。"""
    state_data = {
        "market_state": report.market_state,
        "state_label": report.state_label,
        "strong_areas": report.strong_areas,
        "confidence": report.confidence,
        "anomalies": report.anomalies[:5] if report.anomalies else [],
    }
    # 延迟导入，避免 app.gateway -> app.agent 的潜在循环导入。
    from app.gateway.tool_registry import get_enabled_domains

    for domain in get_enabled_domains():
        key = domain.replace("_", "-")
        state_data[key] = {"state_label": report.state_label}
    return state_data


async def persist_research(
    result: ResearchResponse,
    question: str,
    conversation_id: str | None,
    *,
    memory=None,
    max_retries: int | None = None,
    retry_backoff_ms: int | None = None,
) -> bool:
    """把一次研究结果落库：研究记录 + 对话轮次 + 当日状态。

    阶段 5 门控（方案 §4 阶段 5 第 4 条）：

    - **只有 ``delivery_status == "verified"`` 的结果才允许进入对话轮次 / 当日状态
      / 后续记忆上下文**。``errors == []`` 不构成放行理由——那只说明运行没出错。
    - ``degraded``（本版尚未自动产生）与 ``blocked`` / ``failed`` 一律不写入 memory；
      诊断信息保留在响应和结构化运行日志中。
    - ``result.report is None`` 时不写任何持久化记录，以免空值污染历史状态。
    - 使用结果 ``run_id`` 作为幂等键；锁冲突只在事务回滚后有限重试。

    Returns:
        是否把结论写进了可复用的研究 / 记忆（完整 verified 审计才为 True）。
    """
    if memory is None:
        try:
            from app.memory.storage import get_memory

            memory = get_memory()
        except Exception as exc:  # noqa: BLE001 — persistence must not fake success
            result.persisted = False
            logger.warning("Memory initialization failed (non-fatal): %s", type(exc).__name__)
            return False

    delivery_status = result.delivery_status
    critique = result.critique or {}
    verdict = critique.get("verdict") if isinstance(critique, dict) else getattr(critique, "verdict", None)
    verified = (
        delivery_status == "verified"
        and result.final_audit_status == "pass"
        and str(verdict or "").lower() == "pass"
        and result.report is not None
    )

    if not verified:
        result.persisted = False
        logger.warning(
            "persist_research: delivery_status=%s/final_audit_status=%s/verdict=%s，跳过 memory、对话轮次与当日状态"
            "（question=%s errors=%d）",
            delivery_status,
            result.final_audit_status,
            verdict,
            run_log.redact_text(question),
            len(result.errors),
        )
        return False

    report = result.report
    if report is None:
        result.persisted = False
        logger.warning(
            "persist_research: report 为空，跳过对话轮次与当日状态 (question=%s errors=%s)",
            run_log.redact_text(question),
            len(result.errors),
        )
        return False

    max_retries, retry_backoff_ms = _retry_options(max_retries, retry_backoff_ms)
    max_attempts = max_retries + 1
    transaction = getattr(memory, "transaction", None)
    run_id = result.run_id
    for attempt in range(1, max_attempts + 1):
        try:
            tx = transaction() if callable(transaction) else nullcontext()
            idempotent = False
            with tx:
                if run_id and callable(getattr(memory, "has_persisted_run", None)) and memory.has_persisted_run(run_id):
                    idempotent = True
                else:
                    payload = result.model_dump()
                    payload["unverified"] = False
                    try:
                        memory.save_research(question, payload)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Research save failed (non-fatal): %s", type(exc).__name__)
                        raise

                    if conversation_id:
                        try:
                            memory.save_turn(conversation_id, question, _answer_summary(report))
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("Turn save failed (non-fatal): %s", type(exc).__name__)
                            raise

                    try:
                        memory.save_daily_state(data=_daily_state_data(report))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Daily state save failed (non-fatal): %s", type(exc).__name__)
                        raise
            run_log.log_persistence(
                result, attempt=attempt, max_attempts=max_attempts,
                outcome="idempotent" if idempotent else "committed", idempotent=idempotent,
            )
            result.persisted = True
            return True
        except Exception as exc:  # noqa: BLE001 — rollback and classify below
            # Without a transaction context, a previous write may already have
            # committed before a later lock error. Retrying that batch could
            # duplicate the committed row, so only transactional stores retry.
            retryable = callable(transaction) and _is_transient_sqlite_lock(exc) and attempt < max_attempts
            logger.warning(
                "Persistence attempt %d/%d failed (retryable=%s): %s",
                attempt,
                max_attempts,
                retryable,
                type(exc).__name__,
            )
            run_log.log_persistence(
                result, attempt=attempt, max_attempts=max_attempts,
                outcome="retry" if retryable else "failed",
                retryable=retryable,
                error_category=type(exc).__name__,
            )
            if not retryable:
                # A transaction-capable store has already rolled back all
                # preceding writes.  Non-transactional test doubles retain the
                # historical persisted=False signal.
                result.persisted = False
                return False
            await asyncio.sleep((retry_backoff_ms / 1000) * (2 ** (attempt - 1)))

    result.persisted = False
    return False
