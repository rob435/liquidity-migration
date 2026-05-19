from __future__ import annotations

import os
import urllib.parse
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    token_env: str = "TELEGRAM_BOT_TOKEN"
    chat_id_env: str = "TELEGRAM_CHAT_ID"
    timeout_seconds: float = 10.0


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
