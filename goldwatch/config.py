"""配置加载、环境变量展开与校验。

配置文件使用 TOML（Python 3.11+ 标准库 tomllib），密钥通过 ``.env``
或系统环境变量注入，避免把 webhook / token 写进配置文件。
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UNITS = ("USD/oz", "CNY/g", "CNY/oz", "USD/g")
SOURCE_PROVIDERS = ("goldapi", "sge", "comex")
FX_PROVIDERS = ("sina", "frankfurter", "erapi", "fixed")
PUSH_PROVIDERS = ("serverchan", "pushplus", "wxpusher", "wecom", "console")

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    """配置内容不合法。"""


@dataclass
class Rule:
    name: str
    direction: str
    price: float
    unit: str
    repeat_hours: float = 0.0
    cooldown_hours: float = 1.0
    enabled: bool = True

    def describe_direction(self) -> str:
        return "≤" if self.direction == "below" else "≥"


@dataclass
class Settings:
    config_path: Path
    timezone: str = "Asia/Shanghai"
    state_file: Path = Path("state.json")
    log_file: Path = Path("logs/gold-watch.log")

    source_provider: str = "goldapi"
    source_fallbacks: tuple[str, ...] = ("sge",)

    fx_provider: str = "sina"
    fx_fallbacks: tuple[str, ...] = ("frankfurter",)
    fx_fixed: float | None = None

    push_provider: str = "serverchan"
    push_title_prefix: str = "金价提醒"
    push_uids: tuple[str, ...] = ()

    daily_at: tuple[str, ...] = ("09:30",)
    interval_minutes: int = 0
    active_hours: tuple[str, str] | None = None
    weekdays_only: bool = False
    daily_report: bool = False

    rules: tuple[Rule, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def base_dir(self) -> Path:
        return self.config_path.parent


def load_dotenv(path: Path) -> None:
    """读取简单 KEY=VALUE 形式的 .env；已存在的环境变量优先。"""
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 权限问题
        raise ConfigError(f"无法读取 {path}: {exc}") from exc

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key:
            continue
        os.environ.setdefault(key, value)


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ConfigError(
                    f"配置引用了环境变量 {name}，但它没有设置。"
                    f"请写到 .env 或 export {name}=..."
                )
            return os.environ[name]

        return _ENV_RE.sub(replace, value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


def parse_hhmm(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,2}:\d{2}", value.strip()):
        raise ConfigError(f"{field_name} 必须是 HH:MM 格式，例如 09:30")
    hour_text, minute_text = value.strip().split(":")
    hour, minute = int(hour_text), int(minute_text)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError(f"{field_name} 不是有效时间：{value}")
    return f"{hour:02d}:{minute:02d}"


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] 必须是一个表")
    return value


def _string(value: Any, field_name: str, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field_name} 必须是非空字符串")
    return value.strip()


def _number(value: Any, field_name: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{field_name} 必须是数字")
    return float(value)


def _integer(value: Any, field_name: str, default: int) -> int:
    number = _number(value, field_name, float(default))
    if number != int(number):
        raise ConfigError(f"{field_name} 必须是整数")
    return int(number)


def _boolean(value: Any, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{field_name} 必须是 true 或 false")
    return value


def _string_list(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ConfigError(f"{field_name} 必须是字符串数组")
    return tuple(item.strip() for item in value)


def _resolve_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _parse_rule(raw: Any, index: int) -> Rule:
    if not isinstance(raw, dict):
        raise ConfigError(f"rules[{index}] 必须是一个表")
    name = _string(raw.get("name"), f"rules[{index}].name", f"规则 {index + 1}")
    direction = _string(raw.get("direction"), f"rules[{index}].direction", "")
    if direction not in ("above", "below"):
        raise ConfigError(f"规则「{name}」的 direction 只能是 above 或 below")
    price = _number(raw.get("price"), f"rules[{index}].price", 0.0)
    if price <= 0:
        raise ConfigError(f"规则「{name}」的 price 必须大于 0")
    unit = _string(raw.get("unit"), f"rules[{index}].unit", "CNY/g")
    if unit not in UNITS:
        raise ConfigError(f"规则「{name}」的 unit 不受支持：{unit}（可选 {', '.join(UNITS)}）")
    repeat_hours = _number(raw.get("repeat_hours"), f"rules[{index}].repeat_hours", 0.0)
    cooldown_hours = _number(raw.get("cooldown_hours"), f"rules[{index}].cooldown_hours", 1.0)
    if repeat_hours < 0 or cooldown_hours < 0:
        raise ConfigError(f"规则「{name}」的 repeat_hours / cooldown_hours 不能为负数")
    enabled = _boolean(raw.get("enabled"), f"rules[{index}].enabled", True)
    return Rule(
        name=name,
        direction=direction,
        price=price,
        unit=unit,
        repeat_hours=repeat_hours,
        cooldown_hours=cooldown_hours,
        enabled=enabled,
    )


def load_settings(config_path: str | Path, load_env_file: bool = True) -> Settings:
    path = Path(config_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not path.is_file():
        raise ConfigError(
            f"找不到配置文件 {path}。先执行：cp config.example.toml config.toml"
        )

    if load_env_file:
        load_dotenv(path.parent / ".env")

    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} 不是合法的 TOML：{exc}") from exc

    data = _expand(data)
    app = _table(data, "app")
    source = _table(data, "source")
    fx = _table(data, "fx")
    push = _table(data, "push")
    schedule = _table(data, "schedule")

    timezone_name = _string(app.get("timezone"), "app.timezone", "Asia/Shanghai")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"未知时区：{timezone_name}") from exc

    state_file = _resolve_path(
        _string(app.get("state_file"), "app.state_file", "state.json"), path.parent
    )
    log_file = _resolve_path(
        _string(app.get("log_file"), "app.log_file", "logs/gold-watch.log"), path.parent
    )

    source_provider = _string(source.get("provider"), "source.provider", "goldapi")
    if source_provider not in SOURCE_PROVIDERS:
        raise ConfigError(
            f"source.provider 不支持 {source_provider}（可选 {', '.join(SOURCE_PROVIDERS)}）"
        )
    source_fallbacks = tuple(
        item for item in _string_list(source.get("fallbacks"), "source.fallbacks")
        if item != source_provider
    )
    for item in source_fallbacks:
        if item not in SOURCE_PROVIDERS:
            raise ConfigError(f"source.fallbacks 里有未知来源：{item}")

    fx_provider = _string(fx.get("provider"), "fx.provider", "sina")
    if fx_provider not in FX_PROVIDERS:
        raise ConfigError(f"fx.provider 不支持 {fx_provider}（可选 {', '.join(FX_PROVIDERS)}）")
    fx_fallbacks = tuple(
        item for item in _string_list(fx.get("fallbacks"), "fx.fallbacks")
        if item != fx_provider
    )
    for item in fx_fallbacks:
        if item not in FX_PROVIDERS:
            raise ConfigError(f"fx.fallbacks 里有未知来源：{item}")
    fx_fixed_value = fx.get("fixed_rate")
    fx_fixed = _number(fx_fixed_value, "fx.fixed_rate", 0.0) if fx_fixed_value is not None else None
    if fx_fixed is not None and not (3.0 < fx_fixed < 15.0):
        raise ConfigError("fx.fixed_rate 看起来不是合理的 USD/CNY 汇率")

    push_provider = _string(push.get("provider"), "push.provider", "serverchan")
    if push_provider not in PUSH_PROVIDERS:
        raise ConfigError(f"push.provider 不支持 {push_provider}（可选 {', '.join(PUSH_PROVIDERS)}）")
    push_prefix = _string(push.get("title_prefix"), "push.title_prefix", "金价提醒")
    push_uids = _string_list(push.get("uids"), "push.uids")

    daily_raw = schedule.get("daily_at", ["09:30"])
    if isinstance(daily_raw, str):
        daily_raw = [daily_raw]
    if not isinstance(daily_raw, list) or not daily_raw:
        raise ConfigError("schedule.daily_at 至少要有一个时间")
    daily_at = tuple(sorted({parse_hhmm(item, "schedule.daily_at") for item in daily_raw}))

    interval_minutes = _integer(
        schedule.get("interval_minutes"), "schedule.interval_minutes", 0
    )
    if interval_minutes < 0:
        raise ConfigError("schedule.interval_minutes 不能为负数")

    active_hours: tuple[str, str] | None = None
    active_raw = schedule.get("active_hours")
    if active_raw is not None:
        if not isinstance(active_raw, list) or len(active_raw) != 2:
            raise ConfigError("schedule.active_hours 必须是 [\"08:00\", \"23:00\"] 这样的两个时间")
        start = parse_hhmm(active_raw[0], "schedule.active_hours[0]")
        end = parse_hhmm(active_raw[1], "schedule.active_hours[1]")
        if start == end:
            raise ConfigError("schedule.active_hours 的起止时间不能相同")
        active_hours = (start, end)

    weekdays_only = _boolean(schedule.get("weekdays_only"), "schedule.weekdays_only", False)
    daily_report = _boolean(schedule.get("daily_report"), "schedule.daily_report", False)

    rules_raw = data.get("rules", [])
    if rules_raw is None:
        rules_raw = []
    if not isinstance(rules_raw, list):
        raise ConfigError("rules 必须是数组（[[rules]]）")
    rules = [_parse_rule(item, index) for index, item in enumerate(rules_raw)]
    names = [rule.name for rule in rules]
    duplicates = {name for name in names if names.count(name) > 1}
    if duplicates:
        raise ConfigError(f"规则名重复：{', '.join(sorted(duplicates))}")

    return Settings(
        config_path=path,
        timezone=timezone_name,
        state_file=state_file,
        log_file=log_file,
        source_provider=source_provider,
        source_fallbacks=source_fallbacks,
        fx_provider=fx_provider,
        fx_fallbacks=fx_fallbacks,
        fx_fixed=fx_fixed,
        push_provider=push_provider,
        push_title_prefix=push_prefix,
        push_uids=push_uids,
        daily_at=daily_at,
        interval_minutes=interval_minutes,
        active_hours=active_hours,
        weekdays_only=weekdays_only,
        daily_report=daily_report,
        rules=tuple(rules),
        raw=data,
    )
