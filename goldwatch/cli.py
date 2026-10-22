"""命令行入口：check / run / test-push / status / install-launchd。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import plistlib
import signal
import subprocess
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Event
from typing import Any

from .config import ConfigError, Settings, load_settings
from .notify import NotifyError, build_message, build_test_message, send
from .quote import (
    Quote,
    QuoteError,
    fetch_quote,
    fetch_usdcny,
    format_price,
    needs_fx,
    unit_decimals,
)
from .rules import commit_state, evaluate_rules
from .scheduler import describe_schedule, run_loop
from .state import FileLock, LockError, StateError, StateStore

VERSION = "1.0.0"
LOGGER_NAME = "goldwatch"


def setup_logging(settings: Settings, verbose: bool) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    try:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            settings.log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:  # pragma: no cover - 权限问题
        logger.warning("无法写入日志文件 %s：%s（仅输出到终端）", settings.log_file, exc)
    return logger


def _needs_fx(settings: Settings, quote: Quote) -> bool:
    return any(
        rule.enabled and needs_fx(quote.unit, rule.unit) for rule in settings.rules
    )


def _local_time(value: Any, timezone_name: str) -> str:
    if not isinstance(value, str) or not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def _quote_to_dict(quote: Quote) -> dict[str, Any]:
    return {
        "price": quote.price,
        "unit": quote.unit,
        "source": quote.source,
        "ts": quote.ts.isoformat(),
        "change_pct": quote.change_pct,
    }


def _print_evaluations(settings: Settings, evaluations, logger: logging.Logger) -> None:
    for evaluation in evaluations:
        rule = evaluation.rule
        if evaluation.error:
            logger.warning("规则「%s」无法换算：%s", rule.name, evaluation.error)
            continue
        status = "触发" if evaluation.triggered else "未触发"
        decimals = unit_decimals(rule.unit)
        current = (
            f"{evaluation.price:,.{decimals}f} {rule.unit}"
            if evaluation.price is not None
            else "-"
        )
        logger.info(
            "规则「%s」：%s（目标 %s %.3f %s，现价 %s）",
            rule.name,
            status,
            "≤" if rule.direction == "below" else "≥",
            rule.price,
            rule.unit,
            current,
        )


def run_check(settings: Settings, dry_run: bool, logger: logging.Logger) -> int:
    store = StateStore(settings.state_file)
    lock_path = settings.state_file.with_name(settings.state_file.name + ".lock")
    try:
        with FileLock(lock_path):
            try:
                state = store.load()
            except StateError as exc:
                logger.error("%s", exc)
                return 2

            try:
                quote = fetch_quote(settings)
            except QuoteError as exc:
                logger.error("获取行情失败：%s", exc)
                if not dry_run:
                    state["last_error"] = str(exc)
                    state["last_error_at"] = datetime.now(timezone.utc).isoformat()
                    try:
                        store.save(state)
                    except OSError:
                        pass
                return 1

            usdcny: float | None = None
            if _needs_fx(settings, quote):
                try:
                    usdcny = fetch_usdcny(settings)
                except QuoteError as exc:
                    logger.warning("获取汇率失败，涉及人民币/美元换算的规则会被跳过：%s", exc)

            now = datetime.now(timezone.utc)
            evaluations = evaluate_rules(settings.rules, quote, usdcny, state, now)
            triggered = [item for item in evaluations if item.should_notify]

            should_send = bool(triggered) or settings.daily_report
            delivered = False
            if should_send:
                title, body = build_message(settings, quote, evaluations, usdcny, now)
                try:
                    provider = send(settings, title, body, dry_run=dry_run)
                    delivered = True
                    if not dry_run:
                        if triggered:
                            logger.info("已通过 %s 推送 %d 条规则", provider, len(triggered))
                        else:
                            logger.info("已通过 %s 推送金价日报", provider)
                except NotifyError as exc:
                    logger.error("推送失败：%s", exc)

            if not dry_run:
                commit_state(state, evaluations, now, delivered)
                state["last_check_at"] = now.isoformat()
                state["last_quote"] = _quote_to_dict(quote)
                state["last_error"] = None
                state["last_error_at"] = None
                try:
                    store.save(state)
                except OSError as exc:
                    logger.error("保存状态失败：%s", exc)
                    return 1

            logger.info("当前行情：%s（%s）", format_price(quote.price, quote.unit), quote.source)
            _print_evaluations(settings, evaluations, logger)
            if dry_run and should_send:
                result_text = "dry-run"
            elif delivered and triggered:
                result_text = "已推送"
            elif delivered and settings.daily_report:
                result_text = "日报已发送"
            elif should_send:
                result_text = "推送失败"
            else:
                result_text = "未触发阈值，无需推送"
            logger.info(
                "本次检查：%d 条规则，%d 条触发，%s",
                len(evaluations),
                len(triggered),
                result_text,
            )
            if should_send and not delivered and not dry_run:
                return 1
            return 0
    except LockError as exc:
        logger.warning("%s", exc)
        return 0


def cmd_check(settings: Settings, args: argparse.Namespace, logger: logging.Logger) -> int:
    return run_check(settings, dry_run=args.dry_run, logger=logger)


def cmd_run(settings: Settings, args: argparse.Namespace, logger: logging.Logger) -> int:
    stop = Event()

    def handle_signal(signum, frame):  # noqa: ARG001
        # 信号处理器里不要打日志：可能重入 logging 的锁
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    logger.info("金价观察启动：%s", describe_schedule(settings))
    logger.info("行情源：%s；推送通道：%s", settings.source_provider, settings.push_provider)
    if args.run_now:
        run_check(settings, dry_run=False, logger=logger)
    run_loop(settings, lambda: run_check(settings, False, logger), stop, logger)
    logger.info("已退出")
    return 0


def cmd_test_push(settings: Settings, args: argparse.Namespace, logger: logging.Logger) -> int:
    quote: Quote | None
    try:
        quote = fetch_quote(settings)
    except QuoteError as exc:
        logger.warning("行情获取失败，将只测试推送通道：%s", exc)
        quote = None

    title, body = build_test_message(settings, quote)
    try:
        provider = send(settings, title, body, dry_run=args.dry_run)
    except NotifyError as exc:
        print(f"推送失败：{exc}", file=sys.stderr)
        print(
            "检查 .env 中的凭据，并确认 push.provider 与之一致。"
            "可先用 --dry-run 看消息内容。",
            file=sys.stderr,
        )
        return 1
    print(f"测试消息已通过 {provider} 发送。")
    return 0


def cmd_status(settings: Settings, args: argparse.Namespace, logger: logging.Logger) -> int:
    store = StateStore(settings.state_file)
    try:
        state = store.load()
    except StateError as exc:
        print(f"状态读取失败：{exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0

    print(f"配置文件：{settings.config_path}")
    print(f"定时：{describe_schedule(settings)}（{settings.timezone}）")
    print(f"行情源：{settings.source_provider}；推送：{settings.push_provider}")
    last_check = state.get("last_check_at")
    print(f"上次检查：{_local_time(last_check, settings.timezone) if last_check else '从未'}")
    last_quote = state.get("last_quote")
    if isinstance(last_quote, dict):
        print(
            "上次行情：{}（{}，{}）".format(
                format_price(float(last_quote.get("price", 0)), str(last_quote.get("unit", ""))),
                last_quote.get("source", ""),
                _local_time(last_quote.get("ts"), settings.timezone),
            )
        )
    if state.get("last_error"):
        print(f"上次错误：{state['last_error']}")

    print("规则状态：")
    rule_states = state.get("rules") if isinstance(state.get("rules"), dict) else {}
    for rule in settings.rules:
        entry = rule_states.get(rule.name, {}) if isinstance(rule_states, dict) else {}
        triggered = "触发中" if entry.get("triggered") else "未触发"
        value = entry.get("last_value")
        unit = entry.get("last_unit", rule.unit)
        decimals = unit_decimals(unit)
        value_text = f"{value:,.{decimals}f} {unit}" if isinstance(value, (int, float)) else "-"
        last = _local_time(entry.get("last_triggered_at"), settings.timezone)
        print(
            f"  - {rule.name}：{triggered}，目标 "
            f"{rule.describe_direction()} {rule.price:,.{unit_decimals(rule.unit)}f} {rule.unit}，"
            f"最后值 {value_text}，上次提醒 {last}"
        )
    return 0


def _launch_agent_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _build_plist(settings: Settings, label: str) -> dict[str, Any]:
    script = Path(__file__).resolve().parent.parent / "gold_watch.py"
    log_dir = settings.log_file.parent
    return {
        "Label": label,
        "ProgramArguments": [
            sys.executable,
            str(script),
            "run",
            "--config",
            str(settings.config_path),
        ],
        "WorkingDirectory": str(settings.config_path.parent),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "StandardOutPath": str(log_dir / "launchd.out.log"),
        "StandardErrorPath": str(log_dir / "launchd.err.log"),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def cmd_install_launchd(
    settings: Settings, args: argparse.Namespace, logger: logging.Logger
) -> int:
    if sys.platform != "darwin":
        print("install-launchd 只支持 macOS；Linux 请用 cron 或 systemd。", file=sys.stderr)
        return 2

    label = args.label
    plist = _build_plist(settings, label)
    plist_path = _launch_agent_path(label)
    if args.print_only:
        print(f"# {plist_path}")
        sys.stdout.buffer.write(plistlib.dumps(plist, fmt=plistlib.FMT_XML))
        return 0

    plist_path.parent.mkdir(parents=True, exist_ok=True)
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        plist_path.write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML))
        plist_path.chmod(0o644)
    except OSError as exc:
        print(f"写入 {plist_path} 失败：{exc}", file=sys.stderr)
        return 1

    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{label}")
    result = _launchctl("bootstrap", domain, str(plist_path))
    if result.returncode != 0:
        print(f"launchctl bootstrap 失败：{result.stderr.strip()}", file=sys.stderr)
        return 1
    _launchctl("enable", f"{domain}/{label}")
    print(f"已安装并启动：{plist_path}")
    print(f"查看状态：launchctl print {domain}/{label}")
    print(f"日志：{settings.log_file}")
    return 0


def cmd_uninstall_launchd(
    settings: Settings, args: argparse.Namespace, logger: logging.Logger
) -> int:
    if sys.platform != "darwin":
        print("uninstall-launchd 只支持 macOS。", file=sys.stderr)
        return 2
    label = args.label
    plist_path = _launch_agent_path(label)
    _launchctl("bootout", f"gui/{os.getuid()}/{label}")
    if plist_path.exists():
        plist_path.unlink()
        print(f"已移除：{plist_path}")
    else:
        print("没有找到已安装的 LaunchAgent。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gold-watch",
        description="每天定时观察金价，达到设定价格时推送微信消息。",
    )
    parser.add_argument("--config", default="config.toml", help="配置文件路径（默认 config.toml）")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--version", action="version", version=f"gold-watch {VERSION}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config", default=argparse.SUPPRESS, help="配置文件路径（也可写在子命令后面）"
    )
    common.add_argument(
        "--verbose", action="store_true", default=argparse.SUPPRESS, help="输出调试日志"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", parents=[common], help="立即检查一次")
    check.add_argument("--dry-run", action="store_true", help="只打印消息，不推送、不写状态")
    check.set_defaults(func=cmd_check)

    run = sub.add_parser("run", parents=[common], help="常驻运行，按日程检查")
    run.add_argument("--run-now", action="store_true", help="启动时先检查一次")
    run.set_defaults(func=cmd_run)

    test_push = sub.add_parser("test-push", parents=[common], help="发送一条测试消息")
    test_push.add_argument("--dry-run", action="store_true", help="只打印消息")
    test_push.set_defaults(func=cmd_test_push)

    status = sub.add_parser("status", parents=[common], help="查看上次检查与规则状态")
    status.add_argument("--json", action="store_true", help="输出原始 JSON")
    status.set_defaults(func=cmd_status)

    install = sub.add_parser("install-launchd", parents=[common], help="安装 macOS LaunchAgent（开机自启）")
    install.add_argument("--label", default="com.goldwatch.agent", help="LaunchAgent 标签")
    install.add_argument("--print-only", action="store_true", help="只打印 plist，不安装")
    install.set_defaults(func=cmd_install_launchd)

    uninstall = sub.add_parser("uninstall-launchd", parents=[common], help="卸载 macOS LaunchAgent")
    uninstall.add_argument("--label", default="com.goldwatch.agent", help="LaunchAgent 标签")
    uninstall.set_defaults(func=cmd_uninstall_launchd)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    logger = setup_logging(settings, args.verbose)
    try:
        return int(args.func(settings, args, logger))
    except KeyboardInterrupt:
        logger.info("已中断")
        return 130
    except (StateError, QuoteError) as exc:
        logger.error("%s", exc)
        return 1
