from __future__ import annotations

import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    token_env: str = "TELEGRAM_BOT_TOKEN"
    chat_id_env: str = "TELEGRAM_CHAT_ID"
    timeout_seconds: float = 10.0


def format_usd(value: object, *, signed: bool = False) -> str:
    try:
        amount = float(value or 0.0)
    except (TypeError, ValueError):
        amount = 0.0
    if amount < 0:
        return f"-${abs(amount):,.2f}"
    sign = "+" if signed and amount > 0 else ""
    return f"{sign}${amount:,.2f}"


def format_pct(value: object, *, signed: bool = False) -> str:
    try:
        pct = float(value or 0.0)
    except (TypeError, ValueError):
        pct = 0.0
    sign = "+" if signed and pct > 0 else ""
    return f"{sign}{pct:.2%}"


def format_utc_time_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def format_age_ms(*, now_ms: int, then_ms: int | None) -> str:
    if not then_ms:
        return "no recent cycle"
    age_min = max(0.0, (now_ms - int(then_ms)) / 60_000.0)
    if age_min < 2:
        return "just now"
    if age_min < 90:
        return f"{age_min:.0f} min ago"
    age_h = age_min / 60.0
    if age_h < 48:
        return f"{age_h:.1f}h ago"
    return f"{age_h / 24.0:.1f}d ago"


def send_telegram_message(
    text: str,
    *,
    config: TelegramConfig | None = None,
    enabled: bool = True,
) -> bool:
    # Contract: returns True on a 2xx response, False when disabled or when the
    # token/chat_id env vars are absent. Transport errors (timeout, HTTPError,
    # URLError) propagate to the caller — every call site wraps this in
    # try/except and treats failure as cycle telemetry, not a crash. Don't add
    # exception handling here without updating those call sites first.
    if not enabled:
        return False
    cfg = config or TelegramConfig()
    token = os.environ.get(cfg.token_env)
    chat_id = os.environ.get(cfg.chat_id_env)
    if not token or not chat_id:
        return False

    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=cfg.timeout_seconds) as response:
        return 200 <= int(response.status) < 300
