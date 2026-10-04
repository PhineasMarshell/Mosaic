"""T14 回归：简报按本地（UTC+8）时间精确触发、每类型每天最多一次、跨日重置。"""

import asyncio
from datetime import datetime, timedelta

from app.scheduler import briefs


class SchedulerHarness:
    """可控时钟 + 门控 sleep：测试手动设定时间并逐次放行调度循环。"""

    def __init__(self, start: datetime):
        self.clock = start
        self.events: list[str] = []
        self.waits: list[float] = []
        self._gate = asyncio.Event()

    def now_fn(self):
        return self.clock

    async def sleep_fn(self, sec):
        self.waits.append(sec)
        await self._gate.wait()
        self._gate.clear()

    async def release(self):
        self._gate.set()
        await asyncio.sleep(0)

    def at(self, hour: int, minute: int, day: int = 4):
        self.clock = datetime(2026, 10, day, hour, minute, tzinfo=briefs.LOCAL_TZ)


async def _run(monkeypatch, harness: SchedulerHarness):
    # 不真正写文件。
    monkeypatch.setattr(briefs, "_save_brief_to_file", lambda b, t: None)

    async def mfn():
        harness.events.append("morning")

    async def efn():
        harness.events.append("evening")

    task = asyncio.create_task(briefs._run_scheduler(mfn, efn, now_fn=harness.now_fn, sleep_fn=harness.sleep_fn))
    await asyncio.sleep(0)  # 进入首次 sleep
    return task


async def test_trigger_sequence_and_once_per_day(monkeypatch):
    h = SchedulerHarness(datetime(2026, 10, 4, 8, 0, tzinfo=briefs.LOCAL_TZ))
    task = await _run(monkeypatch, h)
    try:
        # 08:00：未到触发点，wait ≈ 75 分钟（4500s）
        assert h.events == []
        assert 4400 <= h.waits[-1] <= 4500

        # 推进到 09:15 并放行 → morning 触发
        h.at(9, 15)
        await h.release()
        assert h.events == ["morning"]

        # 不再推进、再次放行：morning 当天已触发，不得重复
        await h.release()
        assert h.events == ["morning"]

        # 推进到 15:30 → evening
        h.at(15, 30)
        await h.release()
        assert h.events == ["morning", "evening"]

        # 当天再次放行：evening 已触发，等待次日 morning
        await h.release()
        assert h.events == ["morning", "evening"]

        # 次日 09:15 → morning 再次触发（跨日重置）
        h.at(9, 15, day=5)
        await h.release()
        assert h.events == ["morning", "evening", "morning"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_seconds_until_uses_fixed_offset():
    # 15:30 本地 == 07:30 UTC；到次日 09:15 的等待应为 17h45m
    now = datetime(2026, 10, 4, 15, 30, tzinfo=briefs.LOCAL_TZ)
    wait_morning = briefs._seconds_until(briefs.MORNING_TIME, now)
    assert wait_morning == timedelta(hours=17, minutes=45).total_seconds()

    # 08:00 本地到 09:15
    now2 = datetime(2026, 10, 4, 8, 0, tzinfo=briefs.LOCAL_TZ)
    assert briefs._seconds_until(briefs.MORNING_TIME, now2) == 75 * 60
