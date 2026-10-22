from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from goldwatch.config import Settings
from goldwatch.scheduler import describe_schedule, next_daily, next_run

TZ = ZoneInfo("Asia/Shanghai")


def settings(**kwargs) -> Settings:
    data = {"config_path": Path("/tmp/gold-watch/config.toml")}
    data.update(kwargs)
    return Settings(**data)


def at(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


class SchedulerTest(unittest.TestCase):
    def test_next_daily_today(self) -> None:
        now = at(2026, 10, 22, 8, 0)
        self.assertEqual(next_daily(now, ("09:30",), False), at(2026, 10, 22, 9, 30))

    def test_next_daily_tomorrow(self) -> None:
        now = at(2026, 10, 22, 10, 0)
        self.assertEqual(next_daily(now, ("09:30",), False), at(2026, 10, 23, 9, 30))

    def test_multiple_times(self) -> None:
        now = at(2026, 10, 22, 10, 0)
        self.assertEqual(
            next_daily(now, ("09:30", "21:30"), False), at(2026, 10, 22, 21, 30)
        )

    def test_weekdays_only_skips_weekend(self) -> None:
        # 2026-10-23 是周五，18:00 之后应跳到周一 09:30
        now = at(2026, 10, 23, 18, 0)
        self.assertEqual(
            next_daily(now, ("09:30",), True), at(2026, 10, 26, 9, 30)
        )

    def test_interval_with_active_hours(self) -> None:
        config = settings(
            interval_minutes=30,
            active_hours=("09:00", "18:00"),
        )
        now = at(2026, 10, 22, 10, 0)
        self.assertEqual(next_run(now, config), at(2026, 10, 22, 10, 30))

    def test_interval_outside_active_hours_waits_for_next_window(self) -> None:
        config = settings(
            interval_minutes=30,
            active_hours=("09:00", "18:00"),
        )
        now = at(2026, 10, 22, 17, 50)
        self.assertEqual(next_run(now, config), at(2026, 10, 23, 9, 0))

    def test_interval_weekend_waits_for_monday(self) -> None:
        config = settings(interval_minutes=60, weekdays_only=True)
        now = at(2026, 10, 24, 10, 0)  # 周六
        result = next_run(now, config)
        self.assertEqual(result.weekday(), 0)

    def test_describe(self) -> None:
        self.assertIn("09:30", describe_schedule(settings(daily_at=("09:30",))))
        self.assertIn("30 分钟", describe_schedule(settings(interval_minutes=30)))


if __name__ == "__main__":
    unittest.main()
