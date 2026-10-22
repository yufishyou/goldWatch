"""消息格式化与微信推送通道。

支持：
- Server酱（个人微信，最省事）
- PushPlus（个人微信）
- WxPusher（个人微信）
- 企业微信群机器人（企业微信）
- console（只打印，用于 dry-run）
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .config import Settings
from .quote import Quote, format_price, reference_line, unit_decimals
from .rules import RuleEvaluation, format_difference

MAX_CHARS = 3500
MAX_WECOM_BYTES = 3800


class NotifyError(RuntimeError):
    """推送失败。"""


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise NotifyError(
            f"缺少环境变量 {name}。请在 .env 中填写，或 export {name}=..."
        )
    return value


def _truncate(text: str, limit: int = MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n\n…（内容已截断）"


def _truncate_bytes(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    suffix = "\n\n…（内容已截断）"
    budget = max(0, limit - len(suffix.encode("utf-8")))
    return encoded[:budget].decode("utf-8", errors="ignore").rstrip() + suffix


def build_message(
    settings: Settings,
    quote: Quote,
    evaluations: list[RuleEvaluation],
    usdcny: float | None,
    now: datetime,
    title_prefix: str | None = None,
) -> tuple[str, str]:
    notify_items = [item for item in evaluations if item.should_notify]
    any_triggered = any(
        item.triggered for item in evaluations if item.error is None
    )
    tz = ZoneInfo(settings.timezone)
    local_now = now.astimezone(tz)
    quote_time = quote.ts.astimezone(tz)

    prefix = title_prefix or settings.push_title_prefix
    price_text = format_price(quote.price, quote.unit)
    if any_triggered:
        title = f"{prefix}｜{price_text}"
        if len(notify_items) > 1:
            title += f"（{len(notify_items)} 条规则）"
    else:
        title = f"金价日报｜{price_text}"
    if len(title) > 60:
        title = title[:57] + "..."

    lines: list[str] = []
    lines.append(f"**当前价格**：{price_text}")
    lines.append(f"**行情来源**：{quote.source}")
    lines.append(f"**行情时间**：{quote_time:%Y-%m-%d %H:%M:%S}（{settings.timezone}）")
    lines.append(f"**检查时间**：{local_now:%Y-%m-%d %H:%M:%S}")
    reference = reference_line(quote, usdcny)
    if reference:
        lines.append(f"**参考换算**：{reference}")
    if quote.change_pct is not None:
        lines.append(f"**日涨跌**：{quote.change_pct:+.2f}%")

    lines.append("")
    lines.append("**规则状态**")
    if not evaluations:
        lines.append("- 暂无规则")
    for evaluation in evaluations:
        rule = evaluation.rule
        decimals = unit_decimals(rule.unit)
        target = f"{rule.price:,.{decimals}f} {rule.unit}"
        if evaluation.error is not None:
            lines.append(f"- **{rule.name}**：无法换算（{evaluation.error}）")
            continue
        status = "已触发" if evaluation.triggered else "未触发"
        current = (
            f"{evaluation.price:,.{decimals}f} {rule.unit}"
            if evaluation.price is not None
            else "未知"
        )
        difference = format_difference(evaluation.difference_pct)
        suffix = f"，{difference}" if difference else ""
        lines.append(
            f"- **{rule.name}**：{status}（{rule.describe_direction()} {target}，"
            f"现价 {current}{suffix}）"
        )

    lines.append("")
    lines.append("> 价格数据仅供参考，不构成投资建议。")
    body = "\n".join(lines)
    return _truncate(title, 80), _truncate(body)


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    from .quote import _http_post  # 复用带重试的 HTTP 实现

    raw = _http_post(
        url,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        "application/json; charset=utf-8",
    )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NotifyError(f"推送接口返回了非 JSON 内容：{raw[:200]!r}") from exc
    if not isinstance(data, dict):
        raise NotifyError(f"推送接口返回格式异常：{data!r}")
    return data


def _post_form(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    from .quote import _http_post

    raw = _http_post(
        url,
        urlencode(payload).encode("utf-8"),
        "application/x-www-form-urlencoded; charset=utf-8",
    )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NotifyError(f"推送接口返回了非 JSON 内容：{raw[:200]!r}") from exc
    if not isinstance(data, dict):
        raise NotifyError(f"推送接口返回格式异常：{data!r}")
    return data


def _send_serverchan(title: str, body: str) -> None:
    key = _require_env("SERVERCHAN_SENDKEY")
    try:
        data = _post_form(
            f"https://sctapi.ftqq.com/{key}.send",
            {"title": title, "desp": body},
        )
    except Exception as exc:  # noqa: BLE001 - 错误信息里可能带 SendKey
        raise NotifyError(str(exc).replace(key, "***")) from exc
    if int(data.get("code", -1)) != 0:
        raise NotifyError(f"Server酱返回错误：{data.get('message') or data}")


def _send_pushplus(title: str, body: str) -> None:
    token = _require_env("PUSHPLUS_TOKEN")
    data = _post_json(
        "https://www.pushplus.plus/send",
        {"token": token, "title": title, "content": body, "template": "markdown"},
    )
    if int(data.get("code", -1)) != 200:
        raise NotifyError(f"PushPlus 返回错误：{data.get('msg') or data}")


def _send_wxpusher(settings: Settings, title: str, body: str) -> None:
    app_token = _require_env("WXPUSHER_APP_TOKEN")
    uids = list(settings.push_uids)
    if not uids:
        raw = os.environ.get("WXPUSHER_UIDS") or os.environ.get("WXPUSHER_UID") or ""
        uids = [item.strip() for item in raw.split(",") if item.strip()]
    if not uids:
        raise NotifyError("WxPusher 需要 push.uids 或环境变量 WXPUSHER_UID / WXPUSHER_UIDS")
    summary = (title[:18] + "…") if len(title) > 19 else title
    data = _post_json(
        "https://wxpusher.zjiecode.com/api/send/message",
        {
            "appToken": app_token,
            "content": body,
            "summary": summary,
            "contentType": 3,
            "uids": uids,
        },
    )
    if int(data.get("code", -1)) != 1000:
        raise NotifyError(f"WxPusher 返回错误：{data.get('msg') or data}")


def _send_wecom(title: str, body: str) -> None:
    webhook = _require_env("WECOM_WEBHOOK_URL")
    content = f"**{title}**\n\n{_truncate_bytes(body, MAX_WECOM_BYTES)}"
    try:
        data = _post_json(webhook, {"msgtype": "markdown", "markdown": {"content": content}})
    except Exception as exc:  # noqa: BLE001 - 错误信息里可能带 webhook key
        safe_webhook = re.sub(r"key=[^&]+", "key=***", webhook)
        raise NotifyError(str(exc).replace(webhook, safe_webhook)) from exc
    if int(data.get("errcode", -1)) != 0:
        raise NotifyError(f"企业微信机器人返回错误：{data.get('errmsg') or data}")


def send(
    settings: Settings,
    title: str,
    body: str,
    dry_run: bool = False,
) -> str:
    """发送消息，返回实际使用的通道名。失败会抛 NotifyError。"""
    provider = settings.push_provider
    if dry_run:
        print("\n" + "=" * 60)
        print(f"[dry-run] {title}")
        print("=" * 60)
        print(body)
        print("=" * 60 + "\n")
        return "dry-run"
    if provider == "console":
        print("\n" + "=" * 60)
        print(f"[console] {title}")
        print("=" * 60)
        print(body)
        print("=" * 60 + "\n")
        return "console"

    try:
        if provider == "serverchan":
            _send_serverchan(title, body)
        elif provider == "pushplus":
            _send_pushplus(title, body)
        elif provider == "wxpusher":
            _send_wxpusher(settings, title, body)
        elif provider == "wecom":
            _send_wecom(title, body)
        else:  # pragma: no cover - 配置阶段已校验
            raise NotifyError(f"未知推送通道：{provider}")
    except NotifyError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise NotifyError(f"{provider} 推送失败：{exc}") from exc
    return provider


def build_test_message(settings: Settings, quote: Quote | None) -> tuple[str, str]:
    now = datetime.now(ZoneInfo(settings.timezone))
    if quote is None:
        title = f"{settings.push_title_prefix}｜通道测试"
        body = (
            "**这是一条测试消息**\n\n"
            f"- 推送通道：`{settings.push_provider}`\n"
            f"- 配置：`{settings.config_path}`\n"
            f"- 时间：{now:%Y-%m-%d %H:%M:%S}\n\n"
            "> 收到这条消息说明微信推送已经打通。"
        )
        return title, body
    lines = [
        "**这是一条测试消息**",
        "",
        f"- 当前行情：{format_price(quote.price, quote.unit)}（{quote.source}）",
        f"- 推送通道：`{settings.push_provider}`",
        f"- 时间：{now:%Y-%m-%d %H:%M:%S}",
        "",
        "> 收到这条消息说明微信推送已经打通；正式提醒会带上触发的价格规则。",
    ]
    return f"{settings.push_title_prefix}｜通道测试", "\n".join(lines)
