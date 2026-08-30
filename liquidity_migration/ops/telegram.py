from __future__ import annotations

import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    token_env: str = "TELEGRAM_BOT_TOKEN"
    chat_id_env: str = "TELEGRAM_CHAT_ID"
    #: Optional separate chat for operational alerts; empty env falls back to
    #: the main chat so alerts are never silently dropped.
    alert_chat_id_env: str = "TELEGRAM_ALERT_CHAT_ID"
    timeout_seconds: float = 10.0
    #: Cap on the single in-process 429 retry wait.
    rate_limit_retry_cap_seconds: float = 5.0


def as_block(text: str) -> str:
    """Wrap a whole message as Telegram's monospace block.

    Every message the fleet sends goes out this way, so escaping happens once,
    here, at the boundary: builders write plain text and never carry markup.
    An unescaped `<` in a venue symbol makes Telegram reject the message
    outright, and it would be rejected silently.
    """

    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<pre>{escaped}</pre>"


def send_telegram_message(
    text: str,
    *,
    config: TelegramConfig | None = None,
    enabled: bool = True,
    channel: str = "main",
    parse_mode: str | None = None,
) -> bool:
    # True on 2xx; False when disabled or the token/chat_id vars are absent.
    # Transport errors propagate — callers that must not crash wrap this
    # themselves. The only internal retry is the bounded 429 case below.
    #
    # channel="main" is the trading story; channel="alerts" is the watchdog /
    # needs-fixing line and goes to TELEGRAM_ALERT_CHAT_ID when that is set.
    #
    # parse_mode="HTML" is opt-in per call because it changes what a message
    # may contain: Telegram rejects a stray `<` outright, so only a caller
    # that escapes its own text may ask for it. `as_block` is that escaping,
    # and every caller in the fleet goes out through it.
    if channel not in ("main", "alerts"):
        raise ValueError(f"unknown telegram channel {channel!r}")
    if not enabled:
        return False
    cfg = config or TelegramConfig()
    token = os.environ.get(cfg.token_env)
    chat_id = os.environ.get(cfg.chat_id_env)
    if channel == "alerts":
        chat_id = os.environ.get(cfg.alert_chat_id_env, "").strip() or chat_id
    if not token or not chat_id:
        return False

    fields = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }
    if parse_mode is not None:
        fields["parse_mode"] = parse_mode
    payload = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout_seconds) as response:
            return 200 <= int(response.status) < 300
    except urllib.error.HTTPError as exc:
        retry_after = _rate_limit_retry_seconds(exc, cap_seconds=cfg.rate_limit_retry_cap_seconds)
        if retry_after is None:
            raise
        # Release the HTTPError response before sleeping and retrying.
        exc.close()
        time.sleep(retry_after)
        with urllib.request.urlopen(request, timeout=cfg.timeout_seconds) as response:
            return 200 <= int(response.status) < 300


def _rate_limit_retry_seconds(exc: urllib.error.HTTPError, *, cap_seconds: float) -> float | None:
    """Seconds to wait before the single 429 retry, or None when not retryable
    (non-429, or a Retry-After beyond the cap)."""
    if int(getattr(exc, "code", 0)) != 429:
        return None
    # HTTPError can carry headers=None. Missing or garbage Retry-After -> 1s.
    hdrs = getattr(exc, "headers", None)
    raw = hdrs.get("Retry-After", "1") if hdrs is not None else "1"
    try:
        retry_after = float(raw or 1.0)
    except (TypeError, ValueError):
        retry_after = 1.0
    # Reject non-finite headers before they reach ``time.sleep``.
    if not math.isfinite(retry_after):
        retry_after = 1.0
    if retry_after > cap_seconds:
        return None
    return max(retry_after, 0.5)
