from __future__ import annotations

import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    token_env: str = "TELEGRAM_BOT_TOKEN"
    chat_id_env: str = "TELEGRAM_CHAT_ID"
    timeout_seconds: float = 10.0
    # Single in-process retry after a 429, honoring Telegram's retry_after up
    # to this cap. Most send sites are deliberately fire-once (the ledger stays
    # authoritative); a brief rate-limit wait inside the transport keeps a
    # burst of alerts from silently dropping pages (round 4).
    rate_limit_retry_cap_seconds: float = 5.0


def format_usd(value: object, *, signed: bool = False) -> str:
    try:
        amount = float(value or 0.0)
    except (TypeError, ValueError):
        amount = 0.0
    # audit2: `value or 0.0` does NOT catch NaN (nan is truthy, so `nan or 0.0`
    # stays nan) and a raw nan would render as "$nan"; coerce non-finite to 0.0.
    if not math.isfinite(amount):
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
    # audit2: same NaN escape as format_usd — a non-finite pct would render
    # "nan%"; coerce to 0.0 so finite inputs format byte-identically to before.
    if not math.isfinite(pct):
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


def telegram_configured(*, config: TelegramConfig | None = None) -> bool:
    """True when the token + chat-id env vars are both present (a send can be
    attempted). Lets callers distinguish "not configured" — a False return from
    ``send_telegram_message`` that no retry will ever fix — from a transport
    failure worth retrying (audit 2026-06-12 round 3: ws_risk un-recorded its
    dedupe key and rewrote the dedupe file at heartbeat cadence when the env was
    simply absent)."""
    cfg = config or TelegramConfig()
    return bool(os.environ.get(cfg.token_env)) and bool(os.environ.get(cfg.chat_id_env))


def send_telegram_message(
    text: str,
    *,
    config: TelegramConfig | None = None,
    enabled: bool = True,
) -> bool:
    # Contract: returns True on a 2xx response, False when disabled or when the
    # token/chat_id env vars are absent. Transport errors (timeout, HTTPError,
    # URLError) PROPAGATE to the caller — this function does NOT swallow them.
    # audit2: a prior version of this comment claimed "every call site wraps
    # this in try/except"; that is FALSE — e.g. cli.py's combined-book report
    # (_cmd ... `send_telegram_message(message, enabled=True)`) calls it bare,
    # so a transport fault there crashes the command. Callers that must not crash
    # MUST wrap this in try/except themselves; do not rely on the transport to be
    # exception-free. (Changing it to swallow is blocked by the frozen
    # propagation tests in tests/test_liquidity_migration_telegram.py.) The ONE
    # internal retry is the 429 rate-limit case below — bounded by
    # rate_limit_retry_cap_seconds, after which the 429 propagates like any
    # other HTTPError.
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
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout_seconds) as response:
            return 200 <= int(response.status) < 300
    except urllib.error.HTTPError as exc:
        retry_after = _rate_limit_retry_seconds(exc, cap_seconds=cfg.rate_limit_retry_cap_seconds)
        if retry_after is None:
            raise
        # audit2: release the first error response's socket/fp before retrying —
        # HTTPError is itself a closeable file object and leaking it across the
        # sleep+retry holds the connection open. Safe even when fp is None.
        exc.close()
        time.sleep(retry_after)
        with urllib.request.urlopen(request, timeout=cfg.timeout_seconds) as response:
            return 200 <= int(response.status) < 300


def _rate_limit_retry_seconds(exc: urllib.error.HTTPError, *, cap_seconds: float) -> float | None:
    """Seconds to wait before the single 429 retry, or None when the error is
    not a retryable rate limit (any non-429, or a Retry-After beyond the cap —
    sleeping longer than the cap inside a trading-loop send is worse than
    dropping the page)."""
    if int(getattr(exc, "code", 0)) != 429:
        return None
    # HTTPError can carry headers=None (it is constructed that way across the repo's own
    # tests and by urllib for some errors), so guard the .get before touching it — else a
    # 429 with null headers raises AttributeError out of the HTTPError handler instead of
    # the intended graceful behavior (telegram-alert-3). Missing/garbage Retry-After -> 1s.
    hdrs = getattr(exc, "headers", None)
    raw = hdrs.get("Retry-After", "1") if hdrs is not None else "1"
    try:
        retry_after = float(raw or 1.0)
    except (TypeError, ValueError):
        retry_after = 1.0
    # audit2: a header like "nan"/"inf" parses to a non-finite float; nan slips
    # past the `> cap_seconds` check (nan comparisons are False) and reaches
    # time.sleep(nan), which raises ValueError out of the retry path. Treat any
    # non-finite value as a garbage header and fall back to the 1s default.
    if not math.isfinite(retry_after):
        retry_after = 1.0
    if retry_after > cap_seconds:
        return None
    return max(retry_after, 0.5)
