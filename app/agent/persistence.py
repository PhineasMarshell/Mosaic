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

import logging
from contextlib import nullcontext

from app.models.response import MarketIntelligence, ResearchResponse

logger = logging.getLogger(__name__)


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
) -> bool:
    """把一次研究结果落库：研究记录 + 对话轮次 + 当日状态。

    阶段 5 门控（方案 §4 阶段 5 第 4 条）：

    - **只有 ``delivery_status == "verified"`` 的结果才允许进入对话轮次 / 当日状态
      / 后续记忆上下文**。``errors == []`` 不构成放行理由——那只说明运行没出错。
    - ``degraded``（本版尚未自动产生）与 ``blocked`` / ``failed`` 一律不写入 memory；
      诊断信息保留在响应和结构化运行日志中。
    - ``result.report is None`` 时不写任何持久化记录，以免空值污染历史状态。

    Returns:
        是否把结论写进了可复用的研究 / 记忆（完整 verified 审计才为 True）。
    """
    if memory is None:
        try:
            from app.memory.storage import get_memory

            memory = get_memory()
        except Exception as exc:  # noqa: BLE001 — persistence must not fake success
            result.persisted = False
            logger.warning("Memory initialization failed (non-fatal): %s", exc)
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
            question[:50],
            len(result.errors),
        )
        return False

    report = result.report
    if report is None:
        result.persisted = False
        logger.warning(
            "persist_research: report 为空，跳过对话轮次与当日状态 (question=%s errors=%s)",
            question[:50],
            result.errors,
        )
        return False

    persisted = True
    transaction = getattr(memory, "transaction", None)
    tx = transaction() if callable(transaction) else nullcontext()
    try:
        with tx:
            try:
                payload = result.model_dump()
                payload["unverified"] = False
                memory.save_research(question, payload)
            except Exception as exc:  # noqa: BLE001 — keep operation-specific diagnostics
                logger.warning("Research save failed (non-fatal): %s", exc)
                raise

            if conversation_id:
                try:
                    memory.save_turn(conversation_id, question, _answer_summary(report))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Turn save failed (non-fatal): %s", exc)
                    raise

            try:
                memory.save_daily_state(data=_daily_state_data(report))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Daily state save failed (non-fatal): %s", exc)
                raise
    except Exception:
        # A transaction-capable store rolls back all preceding writes.  Stores
        # used by lightweight callers may not expose transactions; they still
        # receive the historical persisted=False signal.
        persisted = False
    result.persisted = persisted
    return persisted
