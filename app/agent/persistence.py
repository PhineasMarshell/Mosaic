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
) -> None:
    """把一次研究结果落库：研究记录 + 对话轮次 + 当日状态。

    - 研究记录始终保存（即便没有报告，也便于事后排查）。
    - ``result.report is None`` 时：只保存研究记录，**不写对话轮次、不写当日
      状态**，以免空值覆盖当天已有的正确快照，并记一条 warning。
    - 各项落库失败不致命，但以 warning 级别记录（旧实现是 debug，等于静默）。

    Args:
        result: 编排器产出的完整响应。
        question: 本轮用户问题。
        conversation_id: 多轮对话 id；为空则不写对话轮次。
        memory: 可注入的 Memory 存储（测试用）；默认走全局 ``get_memory()``。
    """
    if memory is None:
        from app.memory.storage import get_memory

        memory = get_memory()

    # 研究记录：始终保存，dump 形状与两条路径历史一致。
    try:
        memory.save_research(question, result.model_dump())
    except Exception as exc:  # noqa: BLE001 — 落库失败不致命，但必须可见
        logger.warning("Research save failed (non-fatal): %s", exc)

    report = result.report
    if report is None:
        logger.warning(
            "persist_research: report 为空，跳过对话轮次与当日状态 (question=%s errors=%s)",
            question[:50],
            result.errors,
        )
        return

    if conversation_id:
        try:
            memory.save_turn(conversation_id, question, _answer_summary(report))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Turn save failed (non-fatal): %s", exc)

    try:
        memory.save_daily_state(data=_daily_state_data(report))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Daily state save failed (non-fatal): %s", exc)
