from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from goldwatch.config import Rule
from goldwatch.quote import Quote
from goldwatch.rules import commit_state, evaluate_rules


def quote(price: float, unit: str = "CNY/g") -> Quote:
    return Quote(price=price, unit=unit, source="test", ts=datetime.now(timezone.utc))


class RuleEvaluationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)

    def rule(self, **kwargs) -> Rule:
        data = {
            "name": "跌破 870",
            "direction": "below",
            "price": 870,
            "unit": "CNY/g",
            "repeat_hours": 0,
            "cooldown_hours": 0,
        }
        data.update(kwargs)
        return Rule(**data)

    def test_first_crossing_notifies(self) -> None:
        evaluations = evaluate_rules([self.rule()], quote(860), None, {"rules": {}}, self.now)
        self.assertEqual(len(evaluations), 1)
        self.assertTrue(evaluations[0].triggered)
        self.assertTrue(evaluations[0].crossed)
        self.assertTrue(evaluations[0].should_notify)

    def test_already_triggered_does_not_repeat_by_default(self) -> None:
        state = {
            "rules": {
                "跌破 870": {
                    "triggered": True,
                    "last_triggered_at": (self.now - timedelta(hours=1)).isoformat(),
                }
            }
        }
        evaluations = evaluate_rules([self.rule()], quote(865), None, state, self.now)
        self.assertTrue(evaluations[0].triggered)
        self.assertFalse(evaluations[0].crossed)
        self.assertFalse(evaluations[0].should_notify)

    def test_repeat_after_interval(self) -> None:
        state = {
            "rules": {
                "跌破 870": {
                    "triggered": True,
                    "last_triggered_at": (self.now - timedelta(hours=25)).isoformat(),
                }
            }
        }
        evaluations = evaluate_rules(
            [self.rule(repeat_hours=24)], quote(865), None, state, self.now
        )
        self.assertTrue(evaluations[0].repeat_due)
        self.assertTrue(evaluations[0].should_notify)

    def test_cooldown_blocks_quick_crossing(self) -> None:
        state = {
            "rules": {
                "跌破 870": {
                    "triggered": False,
                    "last_triggered_at": (self.now - timedelta(minutes=30)).isoformat(),
                }
            }
        }
        evaluations = evaluate_rules(
            [self.rule(cooldown_hours=2)], quote(860), None, state, self.now
        )
        self.assertTrue(evaluations[0].crossed)
        self.assertTrue(evaluations[0].cooldown_blocked)
        self.assertFalse(evaluations[0].should_notify)

    def test_above_direction(self) -> None:
        rule = self.rule(name="突破 4200", direction="above", price=4200, unit="USD/oz")
        evaluations = evaluate_rules([rule], quote(4210, "USD/oz"), None, {"rules": {}}, self.now)
        self.assertTrue(evaluations[0].triggered)
        self.assertTrue(evaluations[0].should_notify)

    def test_unit_conversion(self) -> None:
        rule = self.rule(name="国内价", price=870, unit="CNY/g")
        evaluations = evaluate_rules(
            [rule], quote(4118.5, "USD/oz"), 6.676, {"rules": {}}, self.now
        )
        self.assertAlmostEqual(evaluations[0].price, 4118.5 * 6.676 / 31.1034768, places=6)
        self.assertFalse(evaluations[0].should_notify)

    def test_failed_delivery_keeps_state_for_retry(self) -> None:
        evaluations = evaluate_rules([self.rule()], quote(860), None, {"rules": {}}, self.now)
        state = {"rules": {}}
        commit_state(state, evaluations, self.now, delivered=False)
        self.assertNotIn("跌破 870", state["rules"])

        state = {"rules": {}}
        commit_state(state, evaluations, self.now, delivered=True)
        self.assertTrue(state["rules"]["跌破 870"]["triggered"])
        self.assertIn("last_triggered_at", state["rules"]["跌破 870"])

    def test_recovery_resets_triggered(self) -> None:
        state = {
            "rules": {
                "跌破 870": {
                    "triggered": True,
                    "last_triggered_at": (self.now - timedelta(hours=1)).isoformat(),
                }
            }
        }
        evaluations = evaluate_rules([self.rule()], quote(900), None, state, self.now)
        commit_state(state, evaluations, self.now, delivered=False)
        self.assertFalse(state["rules"]["跌破 870"]["triggered"])


if __name__ == "__main__":
    unittest.main()
