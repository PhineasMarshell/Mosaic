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
from datetime import UTC, datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# T14：简报按**本地时间**（北京时间）触发。旧实现用 datetime.now(UTC) 却直接和
# 9:15 / 15:30 比较，等于在 UTC 01:15 / 07:30 触发，时区错了 8 小时。
# 北京自 1991 年起无夏令时，固定 UTC+8 即可，避免对系统 tzdata 的依赖（Windows 无内置时区库）。
LOCAL_TZ = timezone(timedelta(hours=8))
MORNING_TIME = dt_time(9, 15)
EVENING_TIME = dt_time(15, 30)


def _seconds_until(target: dt_time, now: datetime) -> float:
    """计算从 ``now``（带 tz）到下一个 ``target`` 本地时刻的正秒数。"""
    today_target = datetime.combine(now.date(), target, tzinfo=now.tzinfo)
    delta = (today_target - now).total_seconds()
    if delta <= 0:
        next_target = datetime.combine(now.date() + timedelta(days=1), target, tzinfo=now.tzinfo)
        delta = (next_target - now).total_seconds()
    return delta


def _brief_file_dir(brief_type: str) -> Path:
    """定时简报的文件目录：<项目根>/memory/<morning|evening>/。"""
    project_root = Path(__file__).resolve().parents[2]
    d = project_root / "memory" / brief_type
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_brief_to_file(brief: dict[str, Any], brief_type: str) -> Path:
    """把定时简报按日期存为 JSON 文件（morning / evening 分开存放）。

    Returns:
        写入的文件路径
    """
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    path = _brief_file_dir(brief_type) / f"{date_str}.json"
    path.write_text(
        json.dumps(brief, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    logger.info("Saved %s brief to %s", brief_type, path)
    return path


async def _run_scheduler(morning_fn, evening_fn, *, now_fn=None, sleep_fn=None):
    """后台调度循环：精确睡到下一个触发时刻，而不是靠 2 秒窗口每 30 秒轮询。

    - 触发时刻按 LOCAL_TZ（Asia/Shanghai）解释，修复 UTC 时区错误。
    - 每个类型每天最多触发一次（``fired`` 记录），跨日自动重置。
    - ``now_fn`` / ``sleep_fn`` 可注入，便于用可控时钟测试。
    """
    now_fn = now_fn or (lambda: datetime.now(LOCAL_TZ))
    sleep_fn = sleep_fn or asyncio.sleep
    logger.info("Brief scheduler started")

    fired: set[str] = set()

    async def _fire(brief_type: str, fn) -> None:
        try:
            logger.info("Triggering %s brief", brief_type)
            brief = await fn()
            _save_brief_to_file(brief, brief_type)
        except Exception as exc:
            logger.error("%s brief failed: %s", brief_type, exc)

    while True:
        now = now_fn()
        today = now.date().isoformat()

        # 到达/越过当天触发时刻即到期（catch-up：重启后也能补触发，保证每天一次）。
        morning_due = now >= datetime.combine(now.date(), MORNING_TIME, tzinfo=LOCAL_TZ)
        evening_due = now >= datetime.combine(now.date(), EVENING_TIME, tzinfo=LOCAL_TZ)

        morning_key = f"{today}:morning"
        evening_key = f"{today}:evening"
        if morning_due and morning_key not in fired:
            await _fire("morning", morning_fn)
            fired.add(morning_key)
        if evening_due and evening_key not in fired:
            await _fire("evening", evening_fn)
            fired.add(evening_key)

        # 只保留今天的触发记录（跨日重置）。
        fired = {k for k in fired if k.startswith(today)}

        # 精确计算到下一个最近触发时刻的秒数并睡到点，避免窗口轮询漏触发。
        now = now_fn()
        wait = min(_seconds_until(MORNING_TIME, now), _seconds_until(EVENING_TIME, now))
        try:
            await sleep_fn(max(wait, 1.0))
        except asyncio.CancelledError:
            logger.info("Brief scheduler cancelled")
            break


# ── Brief Generators ──────────────────────


def _legacy_items_from_memory(memory, brief_type: str = "morning") -> list[str]:
    """图失败时的 memory 模板兜底（P5 前的旧逻辑，按 brief_type 分支）。"""
    if brief_type == "evening":
        return _legacy_evening_items(memory)
    return _legacy_morning_items(memory)


def _legacy_morning_items(memory) -> list[str]:
    """晨报的纯 memory 模板（旧逻辑）。"""
    recent_states = memory.get_recent_states(days=7)
    items = []
    latest = recent_states[0] if recent_states else None
    if latest:
        label = latest.get("state_label", "N/A")
        strong = latest.get("strong_areas", [])[:3]
        items.append(f"市场当前状态: {label}")
        if strong:
            items.append(f"强势方向: {', '.join(strong)}")
    anomalies = memory.get_anomalies(since=None, limit=3)
    if anomalies:
        top = anomalies[0].get("description", anomalies[0].get("type", ""))
        items.append(f"近期注意的异常: {top}")
    items.append("接下来关注: 涨停数量是否恢复、新主线是否形成、高位股是否重新获得承接")
    return items[:5]


def _legacy_evening_items(memory) -> list[str]:
    """晚报的纯 memory 模板（旧逻辑）。"""
    today_state = memory.get_daily_state()
    recent_states = memory.get_recent_states(days=7)
    items = []
    if today_state:
        label = today_state.get("state_label", "N/A")
        items.append(f"今日市场状态: {label}")
        for key in ("a-share", "crypto"):
            domain_data = today_state.get(key, {})
            if isinstance(domain_data, dict):
                dl = domain_data.get("state_label", "")
                if dl:
                    items.append(f"{key}: {dl}")
    items.append("盘中判断回顾: 观察题材轮动趋势与情绪变化")
    if recent_states:
        prev = recent_states[-1] if len(recent_states) > 1 else {}
        curr = recent_states[0]
        prev_themes = set(prev.get("strong_areas", [])[:5])
        curr_themes = set(curr.get("strong_areas", [])[:5])
        rotation = curr_themes - prev_themes
        if rotation:
            items.append(f"主题轮换: {' → '.join(list(rotation)[:3])}")
    anomalies = memory.get_anomalies(limit=2)
    if anomalies:
        items.append(f"待跟踪异常: {anomalies[0].get('description', '')}")
    return items[:5]


async def generate_morning_brief(settings=None, memory=None) -> dict[str, Any]:
    """生成 Morning Brief — 优先走图，失败时 memory 模板兜底。"""
    from app.agent.orchestrator import Orchestrator
    from app.config import get_settings as _get_settings
    from app.memory.storage import get_memory as _get_memory

    settings = settings or _get_settings()
    memory = memory or _get_memory()

    question = "请做一份今日开盘前的市场状态检查：当前市场状态、强势方向、今日需要重点跟踪的变量。"
    try:
        result = await asyncio.wait_for(
            Orchestrator(settings).run(question),
            timeout=settings.research_budget_seconds,
        )
        report = result.report
        items = [
            f"市场当前状态: {report.state_label}",
            f"发生了什么: {report.what_happened[:120]}",
        ]
        if report.strong_areas:
            items.append(f"强势方向: {', '.join(report.strong_areas[:3])}")
        if report.risks:
            items.append(f"风险: {report.risks[0][:80]}")
    except Exception as exc:
        logger.warning("Graph-based morning brief failed, fallback to memory template: %s", exc)
        items = _legacy_items_from_memory(memory, "morning")

    return {
        "title": "Good Morning. Here's what matters today.",
        "items": items[:5],
        "generated_at": datetime.now(UTC).isoformat(),
        "type": "morning",
    }


async def generate_evening_brief(settings=None, memory=None) -> dict[str, Any]:
    """生成 Evening Brief — 优先走图，失败时 memory 模板兜底。"""
    from app.agent.orchestrator import Orchestrator
    from app.config import get_settings as _get_settings
    from app.memory.storage import get_memory as _get_memory

    settings = settings or _get_settings()
    memory = memory or _get_memory()

    question = "今天实际发生了什么：状态变化、验证/证伪了什么、主题是否轮换"
    try:
        result = await asyncio.wait_for(
            Orchestrator(settings).run(question),
            timeout=settings.research_budget_seconds,
        )
        report = result.report
        items = [
            f"今日市场状态: {report.state_label}",
            f"发生了什么: {report.what_happened[:120]}",
        ]
        if report.strong_areas:
            items.append(f"强势方向: {', '.join(report.strong_areas[:3])}")
        if report.risks:
            items.append(f"风险: {report.risks[0][:80]}")
    except Exception as exc:
        logger.warning("Graph-based evening brief failed, fallback to memory template: %s", exc)
        items = _legacy_items_from_memory(memory, "evening")

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
    return _scheduler_task is not None and not _scheduler_task.done()
