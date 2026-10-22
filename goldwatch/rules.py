"""阈值规则判断与状态提交。

推送策略：
- 价格从“未触发”进入“触发”时推一次（crossing）；
- 如果价格持续满足条件，``repeat_hours`` 之后可再次提醒（0 表示只在穿越时提醒）；
- 任意两次提醒之间至少间隔 ``cooldown_hours``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .config import Rule
from .quote import Quote, convert


@dataclass
class RuleEvaluation:
    rule: Rule
    price: float | None
    triggered: bool
    crossed: bool
    repeat_due: bool
    cooldown_blocked: bool
    should_notify: bool
    difference_pct: float | None
    error: str | None = None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def evaluate_rules(
    rules: tuple[Rule, ...] | list[Rule],
    quote: Quote,
    usdcny: float | None,
    state: dict[str, Any],
    now: datetime,
) -> list[RuleEvaluation]:
    rule_states = state.get("rules") if isinstance(state.get("rules"), dict) else {}
    evaluations: list[RuleEvaluation] = []

    for rule in rules:
        if not rule.enabled:
            continue
        try:
            price = convert(quote.price, quote.unit, rule.unit, usdcny)
        except Exception as exc:  # noqa: BLE001
            evaluations.append(
                RuleEvaluation(
                    rule=rule,
                    price=None,
                    triggered=False,
                    crossed=False,
                    repeat_due=False,
                    cooldown_blocked=False,
                    should_notify=False,
                    difference_pct=None,
                    error=str(exc),
                )
            )
            continue

        triggered = price <= rule.price if rule.direction == "below" else price >= rule.price
        previous = rule_states.get(rule.name, {}) if isinstance(rule_states, dict) else {}
        was_triggered = bool(previous.get("triggered", False))
        last_notified = _parse_iso(previous.get("last_triggered_at"))

        crossed = triggered and not was_triggered
        repeat_due = False
        if triggered and was_triggered and rule.repeat_hours > 0:
            repeat_due = last_notified is None or (
                now - last_notified >= timedelta(hours=rule.repeat_hours)
            )
        cooldown_blocked = False
        if last_notified is not None and rule.cooldown_hours > 0:
            cooldown_blocked = now - last_notified < timedelta(hours=rule.cooldown_hours)

        should_notify = triggered and (crossed or repeat_due) and not cooldown_blocked
        difference_pct = (price - rule.price) / rule.price * 100.0
        evaluations.append(
            RuleEvaluation(
                rule=rule,
                price=price,
                triggered=triggered,
                crossed=crossed,
                repeat_due=repeat_due,
                cooldown_blocked=cooldown_blocked,
                should_notify=should_notify,
                difference_pct=difference_pct,
            )
        )

    return evaluations


def commit_state(
    state: dict[str, Any],
    evaluations: list[RuleEvaluation],
    now: datetime,
    delivered: bool,
) -> None:
    """把本次判断写回状态。

    ``delivered=False`` 时，本应推送但发送失败的规则保持原状，下一次检查
    会重新按“穿越”处理，避免漏报。
    """
    previous_rules = state.get("rules") if isinstance(state.get("rules"), dict) else {}
    updated: dict[str, Any] = {}

    for evaluation in evaluations:
        rule = evaluation.rule
        if evaluation.error is not None:
            previous = previous_rules.get(rule.name)
            if isinstance(previous, dict):
                updated[rule.name] = previous
            continue

        if evaluation.should_notify and not delivered:
            previous = previous_rules.get(rule.name)
            if isinstance(previous, dict):
                updated[rule.name] = previous
            continue

        previous = previous_rules.get(rule.name, {})
        if not isinstance(previous, dict):
            previous = {}
        entry: dict[str, Any] = {
            "triggered": evaluation.triggered,
            "last_value": round(evaluation.price, 6) if evaluation.price is not None else None,
            "last_unit": rule.unit,
            "last_checked_at": now.isoformat(),
        }
        last_triggered_at = previous.get("last_triggered_at")
        if evaluation.should_notify and delivered:
            last_triggered_at = now.isoformat()
            entry["last_triggered_value"] = round(evaluation.price, 6) if evaluation.price is not None else None
        if last_triggered_at:
            entry["last_triggered_at"] = last_triggered_at
        updated[rule.name] = entry

    # 只保留当前启用规则，避免规则改名后状态无限增长
    active_names = {evaluation.rule.name for evaluation in evaluations}
    for name in previous_rules:
        if name in active_names and name not in updated:
            updated[name] = previous_rules[name]
    state["rules"] = updated


def format_difference(difference_pct: float | None) -> str:
    if difference_pct is None:
        return ""
    return f"{difference_pct:+.2f}%"
