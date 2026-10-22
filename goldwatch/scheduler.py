"""每日定时 / 间隔检查的调度计算。"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from threading import Event
from zoneinfo import ZoneInfo

from .config import Settings


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _in_active(window: tuple[str, str] | None, moment: time) -> bool:
    if window is None:
        return True
    start, end = _parse_hhmm(window[0]), _parse_hhmm(window[1])
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


def _is_weekend(day: datetime) -> bool:
    return day.weekday() >= 5


def next_daily(now: datetime, times: tuple[str, ...], weekdays_only: bool) -> datetime:
    candidates: list[datetime] = []
    for offset in range(0, 8):
        day = (now + timedelta(days=offset)).date()
        if weekdays_only and day.weekday() >= 5:
            continue
        for value in times:
            candidate = datetime.combine(day, _parse_hhmm(value), tzinfo=now.tzinfo)
            if candidate > now:
                candidates.append(candidate)
    if candidates:
        return min(candidates)
    return now + timedelta(days=1)


def _next_active_start(after: datetime, window: tuple[str, str], weekdays_only: bool) -> datetime:
    start_time = _parse_hhmm(window[0])
    for offset in range(0, 8):
        day = (after + timedelta(days=offset)).date()
        if weekdays_only and day.weekday() >= 5:
            continue
        candidate = datetime.combine(day, start_time, tzinfo=after.tzinfo)
        if candidate > after:
            return candidate
    return after + timedelta(days=1)


def next_run(now: datetime, settings: Settings, last_run: datetime | None = None) -> datetime:
    if settings.interval_minutes > 0:
        base = max(now, last_run) if last_run else now
        candidate = base + timedelta(minutes=settings.interval_minutes)
        if candidate <= now:
            candidate = now + timedelta(minutes=1)
        if settings.active_hours is not None and not _in_active(
            settings.active_hours, candidate.timetz().replace(tzinfo=None)
        ):
            candidate = _next_active_start(candidate, settings.active_hours, settings.weekdays_only)
        if settings.weekdays_only and _is_weekend(candidate):
            if settings.active_hours is not None:
                candidate = _next_active_start(candidate, settings.active_hours, True)
            else:
                days = 7 - candidate.weekday()
                candidate = candidate + timedelta(days=days)
        return candidate

    return next_daily(now, settings.daily_at, settings.weekdays_only)


def describe_schedule(settings: Settings) -> str:
    if settings.interval_minutes > 0:
        window = (
            f"，仅 {settings.active_hours[0]}–{settings.active_hours[1]}"
            if settings.active_hours
            else ""
        )
        text = f"每 {settings.interval_minutes} 分钟检查一次{window}"
    else:
        text = "每天 " + "、".join(settings.daily_at) + " 检查"
    if settings.daily_report:
        text += "，每次检查都发日报"
    return text


def run_loop(
    settings: Settings,
    check,
    stop_event: Event,
    logger: logging.Logger,
) -> None:
    """按日程循环执行 check；stop_event 置位后退出。"""
    tz = ZoneInfo(settings.timezone)
    last_run: datetime | None = None
    logged_for: datetime | None = None

    while not stop_event.is_set():
        now = datetime.now(tz)
        next_at = next_run(now, settings, last_run)
        if next_at != logged_for:
            logger.info("下一次检查：%s", next_at.strftime("%Y-%m-%d %H:%M:%S %Z"))
            logged_for = next_at

        while not stop_event.is_set():
            now = datetime.now(tz)
            remaining = (next_at - now).total_seconds()
            if remaining <= 0:
                break
            # 分片等待，睡眠/唤醒后能及时补跑
            stop_event.wait(min(30.0, remaining))

        if stop_event.is_set():
            break

        try:
            check()
        except Exception:  # noqa: BLE001 - 单次失败不能结束守护进程
            logger.exception("本次检查失败，等待下一次")
        last_run = datetime.now(tz)
