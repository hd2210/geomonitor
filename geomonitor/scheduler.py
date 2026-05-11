from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from .models import ScheduleConfig


WEEKDAYS = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


async def run_forever(schedule: ScheduleConfig, job) -> None:
    while True:
        await job()
        next_time = next_run_time(schedule)
        sleep_seconds = max((next_time - datetime.now().astimezone()).total_seconds(), 0)
        print(f"Next run at {next_time.isoformat(timespec='seconds')}")
        await asyncio.sleep(sleep_seconds)


def next_run_time(schedule: ScheduleConfig, now: datetime | None = None) -> datetime:
    current = now or datetime.now().astimezone()
    if schedule.type == "interval":
        assert schedule.every_seconds is not None
        return current + timedelta(seconds=schedule.every_seconds)
    if schedule.type == "daily":
        target = _with_time(current, schedule.time)
        return target if target > current else target + timedelta(days=1)
    if schedule.type == "weekly":
        target_days = {WEEKDAYS[d.strip().casefold()] for d in schedule.days}
        for offset in range(8):
            candidate_day = current + timedelta(days=offset)
            if candidate_day.weekday() in target_days:
                target = _with_time(candidate_day, schedule.time)
                if target > current:
                    return target
        raise ValueError("Unable to calculate weekly next run time.")
    if schedule.type == "cron":
        try:
            from croniter import croniter
        except ImportError as exc:
            raise RuntimeError("Cron schedule requires croniter. Install requirements.txt.") from exc
        assert schedule.cron is not None
        return croniter(schedule.cron, current).get_next(datetime)
    raise ValueError(f"Unsupported schedule type: {schedule.type}")


def _with_time(value: datetime, time_text: str | None) -> datetime:
    if not time_text:
        raise ValueError("Schedule time is required.")
    hour_text, minute_text = time_text.split(":", 1)
    return value.replace(hour=int(hour_text), minute=int(minute_text), second=0, microsecond=0)
