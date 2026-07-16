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
    timeout_seconds: float = 10.0
    # Single in-process retry after a 429, honoring Telegram's retry_after up
    # to this cap. Most send sites are deliberately fire-once (the ledger stays
    # authoritative); a brief rate-limit wait inside the transport keeps a
    # burst of alerts from silently dropping pages.
    rate_limit_retry_cap_seconds: float = 5.0


def send_telegram_message(
    text: str,
    *,
    config: TelegramConfig | None = None,
    enabled: bool = True,
) -> bool:
    # Contract: returns True on a 2xx response, False when disabled or when the
    # token/chat_id env vars are absent. Transport errors (timeout, HTTPError,
    # URLError) PROPAGATE to the caller — this function does NOT swallow them.
    # Callers that must not crash MUST wrap this in try/except themselves; do not
    # rely on the transport to be exception-free. The propagation contract is
    # pinned by tests/test_liquidity_migration_telegram.py. The ONE
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
        # Release the HTTPError response before sleeping and retrying.
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
    # Reject non-finite headers before they reach ``time.sleep``.
    if not math.isfinite(retry_after):
        retry_after = 1.0
    if retry_after > cap_seconds:
        return None
    return max(retry_after, 0.5)
