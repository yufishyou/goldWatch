from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from goldwatch.config import ConfigError, load_settings

BASE = """
[app]
timezone = "Asia/Shanghai"
state_file = "state.json"

[source]
provider = "goldapi"
fallbacks = ["sge"]

[push]
provider = "console"

[schedule]
daily_at = ["21:30", "09:30"]
interval_minutes = 0

[[rules]]
name = "国内金价"
direction = "below"
price = 870
unit = "CNY/g"
repeat_hours = 24
"""


class ConfigTest(unittest.TestCase):
    def write(self, text: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_minimal_config(self) -> None:
        settings = load_settings(self.write(BASE), load_env_file=False)
        self.assertEqual(settings.source_provider, "goldapi")
        self.assertEqual(settings.source_fallbacks, ("sge",))
        self.assertEqual(settings.daily_at, ("09:30", "21:30"))
        self.assertEqual(len(settings.rules), 1)
        self.assertEqual(settings.rules[0].direction, "below")
        self.assertTrue(settings.state_file.is_absolute())

    def test_env_expansion(self) -> None:
        os.environ["GOLDWATCH_TEST_UID"] = "UID_123"
        self.addCleanup(os.environ.pop, "GOLDWATCH_TEST_UID", None)
        text = BASE.replace(
            'provider = "console"',
            'provider = "console"\nuids = ["${GOLDWATCH_TEST_UID}"]',
        )
        settings = load_settings(self.write(text), load_env_file=False)
        self.assertEqual(settings.push_uids, ("UID_123",))

    def test_missing_env_raises(self) -> None:
        os.environ.pop("GOLDWATCH_MISSING", None)
        text = BASE.replace(
            'provider = "console"',
            'provider = "console"\nuids = ["${GOLDWATCH_MISSING}"]',
        )
        with self.assertRaises(ConfigError):
            load_settings(self.write(text), load_env_file=False)

    def test_daily_report_flag(self) -> None:
        settings = load_settings(
            self.write(BASE.replace("interval_minutes = 0", "interval_minutes = 0\ndaily_report = true")),
            load_env_file=False,
        )
        self.assertTrue(settings.daily_report)

    def test_invalid_direction(self) -> None:
        with self.assertRaises(ConfigError):
            load_settings(
                self.write(BASE.replace('direction = "below"', 'direction = "sideways"')),
                load_env_file=False,
            )

    def test_invalid_time(self) -> None:
        with self.assertRaises(ConfigError):
            load_settings(
                self.write(BASE.replace('"21:30", "09:30"', '"25:00"')),
                load_env_file=False,
            )

    def test_duplicate_rule_names(self) -> None:
        text = BASE + """
[[rules]]
name = "国内金价"
direction = "above"
price = 900
unit = "CNY/g"
"""
        with self.assertRaises(ConfigError):
            load_settings(self.write(text), load_env_file=False)

    def test_unknown_provider(self) -> None:
        with self.assertRaises(ConfigError):
            load_settings(
                self.write(BASE.replace('provider = "goldapi"', 'provider = "unknown"')),
                load_env_file=False,
            )


if __name__ == "__main__":
    unittest.main()
