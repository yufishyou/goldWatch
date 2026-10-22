"""行情获取、汇率换算与单位转换。

数据源都是公开的免密钥接口，可能延迟或限流；工具会按配置顺序回退。
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import Settings

LOGGER = logging.getLogger(__name__)

TROY_OUNCE_GRAMS = 31.1034768
USER_AGENT = "gold-watch/1.0 (personal price alert; +https://github.com/)"
HTTP_TIMEOUT = 12
HTTP_RETRIES = 2

_FX_CACHE: dict[tuple[str, float | None], tuple[float, float]] = {}
_FX_CACHE_SECONDS = 15 * 60
_SSL_CONTEXT: ssl.SSLContext | None = None


def _ssl_context() -> ssl.SSLContext:
    """macOS 的 python.org 安装包常没有系统 CA 包，优先用 certifi。"""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT
    try:
        import certifi

        _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
        return _SSL_CONTEXT
    except Exception:  # noqa: BLE001 - certifi 可能未安装
        pass
    for candidate in (
        "/etc/ssl/cert.pem",
        "/etc/ssl/certs/ca-certificates.crt",
        "/usr/local/etc/openssl/cert.pem",
    ):
        if os.path.exists(candidate):
            _SSL_CONTEXT = ssl.create_default_context(cafile=candidate)
            return _SSL_CONTEXT
    _SSL_CONTEXT = ssl.create_default_context()
    return _SSL_CONTEXT


class QuoteError(RuntimeError):
    """行情或汇率获取失败。"""


@dataclass(frozen=True)
class Quote:
    price: float
    unit: str
    source: str
    ts: datetime
    change_pct: float | None = None

    def age_seconds(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - self.ts).total_seconds())


def _http_get(url: str, headers: dict[str, str] | None = None) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        request = Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                **(headers or {}),
            },
        )
        try:
            with urlopen(request, timeout=HTTP_TIMEOUT, context=_ssl_context()) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - 网络异常种类很多
            last_error = exc
            if attempt < HTTP_RETRIES:
                time.sleep(attempt)
    raise QuoteError(f"请求 {url} 失败：{last_error}")


def _http_post(url: str, payload: bytes, content_type: str) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        request = Request(
            url,
            data=payload,
            method="POST",
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": content_type,
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=HTTP_TIMEOUT, context=_ssl_context()) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < HTTP_RETRIES:
                time.sleep(attempt)
    raise QuoteError(f"请求 {url} 失败：{last_error}")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sina_payload(symbol: str) -> str:
    raw = _http_get(
        f"https://hq.sinajs.cn/list={symbol}",
        headers={"Referer": "https://finance.sina.com.cn"},
    )
    text = raw.decode("gbk", errors="replace")
    match = re.search(r'="(.*)"\s*;', text)
    if not match or not match.group(1).strip():
        raise QuoteError(f"新浪接口没有返回 {symbol} 的数据")
    return match.group(1)


def _quote_goldapi() -> Quote:
    raw = _http_get("https://api.gold-api.com/price/XAU")
    data = json.loads(raw.decode("utf-8"))
    price = float(data["price"])
    if not (0 < price < 1_000_000):
        raise QuoteError(f"gold-api 返回了异常价格：{price}")
    return Quote(
        price=price,
        unit="USD/oz",
        source="gold-api XAU/USD",
        ts=_parse_iso(data.get("updatedAt")) or datetime.now(timezone.utc),
    )


def _quote_sge() -> Quote:
    """上海黄金交易所 Au99.99，单位 CNY/g。"""
    payload = _sina_payload("SGE_AU9999")
    parts = payload.split(",")
    if len(parts) < 17:
        raise QuoteError(f"上金所行情字段不足：{len(parts)}")
    price = float(parts[3])
    if not (0 < price < 100_000):
        raise QuoteError(f"上金所返回了异常价格：{price}")
    timestamp = datetime.strptime(parts[16].strip(), "%Y-%m-%d %H:%M:%S")
    from zoneinfo import ZoneInfo

    ts = timestamp.replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc)
    change_pct = None
    if len(parts) > 17:
        try:
            change_pct = float(parts[17].strip().rstrip("%"))
        except ValueError:
            change_pct = None
    return Quote(price=price, unit="CNY/g", source="上金所 Au99.99", ts=ts, change_pct=change_pct)


def _quote_comex() -> Quote:
    """COMEX 黄金，单位 USD/oz。"""
    payload = _sina_payload("hf_GC")
    parts = payload.split(",")
    if len(parts) < 13:
        raise QuoteError(f"COMEX 行情字段不足：{len(parts)}")
    price = float(parts[0])
    if not (0 < price < 1_000_000):
        raise QuoteError(f"COMEX 返回了异常价格：{price}")
    date_text = parts[12].strip()
    time_text = parts[6].strip()
    try:
        timestamp = datetime.strptime(f"{date_text} {time_text}", "%Y-%m-%d %H:%M:%S")
        from zoneinfo import ZoneInfo

        ts = timestamp.replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc)
    except ValueError:
        ts = datetime.now(timezone.utc)
    return Quote(price=price, unit="USD/oz", source="COMEX 黄金", ts=ts)


_QUOTE_PROVIDERS = {
    "goldapi": _quote_goldapi,
    "sge": _quote_sge,
    "comex": _quote_comex,
}


def fetch_quote(settings: Settings) -> Quote:
    providers = [settings.source_provider, *settings.source_fallbacks]
    seen: set[str] = set()
    errors: list[str] = []
    for provider in providers:
        if provider in seen:
            continue
        seen.add(provider)
        fetcher = _QUOTE_PROVIDERS.get(provider)
        if fetcher is None:
            continue
        try:
            quote = fetcher()
            LOGGER.info(
                "行情：%.2f %s（%s，%s）",
                quote.price,
                quote.unit,
                quote.source,
                quote.ts.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            )
            return quote
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("行情源 %s 不可用：%s", provider, exc)
            errors.append(f"{provider}: {exc}")
    raise QuoteError("所有行情源都失败了。" + (" " + "；".join(errors) if errors else ""))


def _fx_sina() -> float:
    payload = _sina_payload("fx_susdcny")
    parts = payload.split(",")
    if len(parts) < 3:
        raise QuoteError("新浪汇率字段不足")
    bid, ask = float(parts[1]), float(parts[2])
    rate = (bid + ask) / 2
    if not (3.0 < rate < 15.0):
        raise QuoteError(f"新浪汇率异常：{rate}")
    return rate


def _fx_frankfurter() -> float:
    raw = _http_get("https://api.frankfurter.dev/v1/latest?from=USD&to=CNY")
    data = json.loads(raw.decode("utf-8"))
    rate = float(data["rates"]["CNY"])
    if not (3.0 < rate < 15.0):
        raise QuoteError(f"Frankfurter 汇率异常：{rate}")
    return rate


def _fx_erapi() -> float:
    raw = _http_get("https://open.er-api.com/v6/latest/USD")
    data = json.loads(raw.decode("utf-8"))
    rate = float(data["rates"]["CNY"])
    if not (3.0 < rate < 15.0):
        raise QuoteError(f"open.er-api 汇率异常：{rate}")
    return rate


_FX_PROVIDERS = {
    "sina": _fx_sina,
    "frankfurter": _fx_frankfurter,
    "erapi": _fx_erapi,
}


def fetch_usdcny(settings: Settings) -> float:
    key = (settings.fx_provider, settings.fx_fixed)
    cached = _FX_CACHE.get(key)
    now = time.time()
    if cached and now - cached[0] < _FX_CACHE_SECONDS:
        return cached[1]

    if settings.fx_provider == "fixed":
        if settings.fx_fixed is None:
            raise QuoteError("fx.provider = fixed，但没有配置 fx.fixed_rate")
        rate = settings.fx_fixed
        _FX_CACHE[key] = (now, rate)
        return rate

    providers = [settings.fx_provider, *settings.fx_fallbacks]
    seen: set[str] = set()
    errors: list[str] = []
    for provider in providers:
        if provider in seen or provider == "fixed":
            continue
        seen.add(provider)
        fetcher = _FX_PROVIDERS.get(provider)
        if fetcher is None:
            continue
        try:
            rate = fetcher()
            LOGGER.info("USD/CNY 汇率：%.4f（%s）", rate, provider)
            _FX_CACHE[key] = (now, rate)
            return rate
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("汇率源 %s 不可用：%s", provider, exc)
            errors.append(f"{provider}: {exc}")
    raise QuoteError("所有汇率源都失败了。" + (" " + "；".join(errors) if errors else ""))


def needs_fx(from_unit: str, to_unit: str) -> bool:
    if from_unit == to_unit:
        return False
    cny_units = {"CNY/g", "CNY/oz"}
    usd_units = {"USD/oz", "USD/g"}
    return (from_unit in cny_units) != (to_unit in cny_units)


def convert(price: float, from_unit: str, to_unit: str, usdcny: float | None) -> float:
    if from_unit == to_unit:
        return price
    if from_unit not in ("USD/oz", "CNY/g", "CNY/oz", "USD/g"):
        raise QuoteError(f"不支持的单位：{from_unit}")
    if to_unit not in ("USD/oz", "CNY/g", "CNY/oz", "USD/g"):
        raise QuoteError(f"不支持的单位：{to_unit}")
    if needs_fx(from_unit, to_unit) and not usdcny:
        raise QuoteError(f"从 {from_unit} 换算到 {to_unit} 需要 USD/CNY 汇率")

    # 同一货币内换算不需要汇率
    if from_unit == "CNY/g" and to_unit == "CNY/oz":
        return price * TROY_OUNCE_GRAMS
    if from_unit == "CNY/oz" and to_unit == "CNY/g":
        return price / TROY_OUNCE_GRAMS
    if from_unit == "USD/oz" and to_unit == "USD/g":
        return price / TROY_OUNCE_GRAMS
    if from_unit == "USD/g" and to_unit == "USD/oz":
        return price * TROY_OUNCE_GRAMS

    # 先统一到 USD/oz
    if from_unit == "USD/oz":
        usd_per_oz = price
    elif from_unit == "USD/g":
        usd_per_oz = price * TROY_OUNCE_GRAMS
    elif from_unit == "CNY/oz":
        usd_per_oz = price / float(usdcny)  # type: ignore[arg-type]
    else:  # CNY/g
        usd_per_oz = price * TROY_OUNCE_GRAMS / float(usdcny)  # type: ignore[arg-type]

    if to_unit == "USD/oz":
        return usd_per_oz
    if to_unit == "USD/g":
        return usd_per_oz / TROY_OUNCE_GRAMS
    if to_unit == "CNY/oz":
        return usd_per_oz * float(usdcny)  # type: ignore[arg-type]
    return usd_per_oz * float(usdcny) / TROY_OUNCE_GRAMS  # type: ignore[arg-type]


def unit_decimals(unit: str) -> int:
    return 3 if unit.endswith("/g") else 2


def format_price(value: float, unit: str) -> str:
    return f"{value:,.{unit_decimals(unit)}f} {unit}"


def reference_line(quote: Quote, usdcny: float | None) -> str | None:
    """给消息加一行“另一种计价方式”的参考价。"""
    if quote.unit == "CNY/g" and usdcny:
        usd_oz = convert(quote.price, "CNY/g", "USD/oz", usdcny)
        return f"${usd_oz:,.2f}/oz（按 USD/CNY {usdcny:.4f} 折算）"
    if quote.unit == "USD/oz" and usdcny:
        cny_g = convert(quote.price, "USD/oz", "CNY/g", usdcny)
        return f"{cny_g:,.2f} CNY/g（按 USD/CNY {usdcny:.4f} 折算）"
    return None
