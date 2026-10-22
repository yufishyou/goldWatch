from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from goldwatch.config import Rule, Settings
from goldwatch.notify import build_message, build_test_message
from goldwatch.quote import Quote
from goldwatch.rules import RuleEvaluation


class NotifyMessageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            config_path=Path("/tmp/gold-watch/config.toml"),
            timezone="Asia/Shanghai",
            push_provider="console",
            push_title_prefix="金价提醒",
        )
        self.now = datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)
        self.quote = Quote(
            price=885.42,
            unit="CNY/g",
            source="上金所 Au99.99",
            ts=self.now,
            change_pct=-0.23,
        )
        self.rule = Rule(
            name="国内金价跌破 870",
            direction="below",
            price=870,
            unit="CNY/g",
        )
        self.evaluation = RuleEvaluation(
            rule=self.rule,
            price=865.0,
            triggered=True,
            crossed=True,
            repeat_due=False,
            cooldown_blocked=False,
            should_notify=True,
            difference_pct=(865.0 - 870) / 870 * 100,
        )

    def test_message_contains_key_fields(self) -> None:
        title, body = build_message(
            self.settings, self.quote, [self.evaluation], 6.676, self.now
        )
        self.assertIn("885.420 CNY/g", title)
        self.assertIn("**当前价格**：885.420 CNY/g", body)
        self.assertIn("上金所 Au99.99", body)
        self.assertIn("国内金价跌破 870", body)
        self.assertIn("870.000 CNY/g", body)
        self.assertIn("不构成投资建议", body)

    def test_secondary_currency_reference(self) -> None:
        title, body = build_message(
            self.settings, self.quote, [self.evaluation], 6.676, self.now
        )
        self.assertIn("/oz", body)
        self.assertIn("折算", body)


    def test_daily_report_when_not_triggered(self) -> None:
        evaluation = RuleEvaluation(
            rule=self.rule,
            price=885.0,
            triggered=False,
            crossed=False,
            repeat_due=False,
            cooldown_blocked=False,
            should_notify=False,
            difference_pct=(885.0 - 870) / 870 * 100,
        )
        title, body = build_message(
            self.settings, self.quote, [evaluation], 6.676, self.now
        )
        self.assertIn("金价日报", title)
        self.assertIn("未触发", body)
        self.assertIn("+1.72%", body)

    def test_test_message_without_quote(self) -> None:
        title, body = build_test_message(self.settings, None)
        self.assertIn("通道测试", title)
        self.assertIn("推送通道", body)


if __name__ == "__main__":
    unittest.main()
