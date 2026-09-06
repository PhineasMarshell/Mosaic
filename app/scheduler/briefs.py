"""Daily Briefs — 每日晨报/晚报定时生成。

使用轻量级 asyncio 后台任务（无需外部依赖），替代 APScheduler：
- Morning Brief: 每天 9:15 自动生成 "Good Morning. Here's what matters today."
- Evening Brief: 每天 15:30 自动生成 "What actually happened today?"

集成方式：
1. 通过 app.scheduler.briefs.start_brief_scheduler() 启动
2. 提供 API 端点 /api/brief/morning 和 /api/brief/evening 按需触发
3. 自动保存生成的简报到 Market Memory
"""

import asyncio
import json
import logging
from datetime import UTC, datetime, time as dt_time
from typing import Any

logger = logging.getLogger(__name__)


def _next_run(target_time: dt_time) -> int:
    """计算距离下次目标时间（秒）。"""
    now = datetime.now(UTC).time()
    target = dt_time(target_time.hour, target_time.minute)
    diff = (datetime.combine(datetime.today(), target) - datetime.now().replace(tzinfo=UTC)).total_seconds()
    if diff <= 0:
        diff += 86400  # 明天
    return max(int(diff), 60)  # 至少 60 秒


async def _run_scheduler(morning_fn, evening_fn):
    """后台调度循环——用 asyncio.sleep 而非 APScheduler。"""
    logger.info("Brief scheduler started")

    while True:
        try:
            now = datetime.now(UTC)
            morning_trigger = False
            evening_trigger = False

            # 只在到达分钟时检查一次（避免每分钟重复触发）
            if now.hour == 9 and now.minute == 15 and now.second < 2:
                morning_trigger = True
            if now.hour == 15 and now.minute == 30 and now.second < 2:
                evening_trigger = True

            if morning_trigger:
                logger.info("Triggering morning brief")
                try:
                    await morning_fn()
                except Exception as exc:
                    logger.error("Morning brief failed: %s", exc)

            if evening_trigger:
                logger.info("Triggering evening brief")
                try:
                    await evening_fn()
                except Exception as exc:
                    logger.error("Evening brief failed: %s", exc)

            # 每 30 秒检查一次，平衡精度和开销
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            logger.info("Brief scheduler cancelled")
            break
        except Exception as exc:
            logger.error("Scheduler error: %s", exc)
            await asyncio.sleep(60)


# ── Brief Generators ──────────────────────

async def generate_morning_brief(settings=None, memory=None) -> dict[str, Any]:
    """生成 Morning Brief。"""
    from app.config import get_settings as _get_settings
    from app.memory.storage import MarketMemory as _MarketMemory

    settings = settings or _get_settings()
    memory = memory or _MarketMemory()

    # 获取最近几天的状态
    recent_states = memory.get_recent_states(days=7)

    # 构建"今天值得关注的 5 件事"
    items = []

    # 1. 今日市场概览（基于最近的 state）
    latest = recent_states[0] if recent_states else None
    if latest:
        label = latest.get("state_label", "N/A")
        strong = latest.get("strong_areas", [])[:3]
        items.append(f"市场当前状态: {label}")
        if strong:
            items.append(f"强势方向: {', '.join(strong)}")

    # 2. 近期异常事件
    anomalies = memory.get_anomalies(since=None, limit=3)
    if anomalies:
        top = anomalies[0].get("description", anomalies[0].get("type", ""))
        items.append(f"近期注意的异常: {top}")

    # 3. 需要持续观察的变量
    items.append("接下来关注: 涨停数量是否恢复、新主线是否形成、高位股是否重新获得承接")

    return {
        "title": "Good Morning. Here's what matters today.",
        "items": items[:5],
        "generated_at": datetime.now(UTC).isoformat(),
        "type": "morning",
    }


async def generate_evening_brief(settings=None, memory=None) -> dict[str, Any]:
    """生成 Evening Brief。"""
    from app.config import get_settings as _get_settings
    from app.memory.storage import MarketMemory as _MarketMemory

    settings = settings or _get_settings()
    memory = memory or _MarketMemory()

    # 获取今天的 daily state
    today_state = memory.get_daily_state()
    recent_states = memory.get_recent_states(days=7)

    items = []

    # 1. 今日总结
    if today_state:
        label = today_state.get("state_label", "N/A")
        items.append(f"今日市场状态: {label}")

        for key in ("a-share", "crypto"):
            domain_data = today_state.get(key, {})
            if isinstance(domain_data, dict):
                dl = domain_data.get("state_label", "")
                if dl:
                    items.append(f"{key}: {dl}")

    # 2. 验证了哪些判断
    items.append("盘中判断回顾: 观察题材轮动趋势与情绪变化")

    # 3. 主题变化
    if recent_states:
        prev = recent_states[-1] if len(recent_states) > 1 else {}
        curr = recent_states[0]
        prev_themes = set(prev.get("strong_areas", [])[:5])
        curr_themes = set(curr.get("strong_areas", [])[:5])
        rotation = curr_themes - prev_themes
        if rotation:
            items.append(f"主题轮换: {' → '.join(list(rotation)[:3])}")

    # 4. 需要继续观察的异常
    anomalies = memory.get_anomalies(limit=2)
    if anomalies:
        items.append(f"待跟踪异常: {anomalies[0].get('description', '')}")

    return {
        "title": "What actually happened today?",
        "items": items[:5],
        "generated_at": datetime.now(UTC).isoformat(),
        "type": "evening",
    }


# ── Scheduler Start/Stop ──────────────────

_scheduler_task: asyncio.Task | None = None


def start_brief_scheduler(
    morning_fn=None,
    evening_fn=None,
):
    """启动简报后台调度器。

    Args:
        morning_fn: 自定义晨间简报函数
        evening_fn: 自定义晚间简报函数

    Returns:
        asyncio.Task 对象（供外部取消）
    """
    global _scheduler_task

    if _scheduler_task and not _scheduler_task.done():
        logger.warning("Brief scheduler already running")
        return _scheduler_task

    mfn = morning_fn or generate_morning_brief
    efn = evening_fn or generate_evening_brief

    async def _wrapper():
        await _run_scheduler(mfn, efn)

    _scheduler_task = asyncio.create_task(_wrapper(), name="brief-scheduler")
    logger.info("Brief scheduler task created")
    return _scheduler_task


def stop_brief_scheduler() -> bool:
    """停止简报调度器。

    Returns:
        成功取消返回 True，否则返回 False
    """
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        _scheduler_task = None
        return True
    return False


def is_running() -> bool:
    """检查调度器是否正在运行。"""
    return (_scheduler_task is not None and not _scheduler_task.done())
