"""Trade updates for the phone: entries and exits from the target books.

Every sleeve publishes its desired book as a file, so the union of those
files IS the fleet's trading story — one watcher diffing them covers carry,
LONG, and the exodus short without touching a producer or the engine. A new
symbol with size is an entry; a zeroed or vanished symbol is an exit. Sizing
wiggles are the producers' housekeeping and stay off the phone.

Messages go to the main line (the owner's DM with the bot); the group is the
debugging line and gets nothing from here. The LLM gate's book is excluded:
its own service already sends richer messages (score, stop) for the same
events, and one event must not page twice.

First run on an empty state baselines silently. An unreadable book keeps its
previous state — a producer mid-write must not read as a mass exit.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from liquidity_migration.ops.telegram import send_telegram_message

BOOKS = {
    "CARRY": "/var/lib/liquidity-migration/targets/carry-demo.json",
    "LONG": "/var/lib/liquidity-migration/targets/long-demo.json",
    "EXODUS": "/var/lib/liquidity-migration/targets/exodus-demo.json",
}
STATE_PATH = "/var/lib/liquidity-migration/book-notify/state.json"


def read_positive_targets(path: str) -> dict[str, float] | None:
    """Symbol -> notional for rows with size, or None when unreadable."""

    try:
        with open(path) as fh:
            book = json.load(fh)
        out: dict[str, float] = {}
        for row in book.get("targets") or []:
            notional = abs(float(row.get("notional_usdt", 0.0)))
            if notional > 0.0:
                out[str(row["symbol"])] = notional
        return out
    except Exception:
        return None


def diff_messages(sleeve: str, before: dict[str, float], now: dict[str, float]) -> list[str]:
    messages = []
    for symbol in sorted(set(now) - set(before)):
        if sleeve == "EXODUS":
            messages.append(f"EXODUS short: {symbol} ${now[symbol]:.2f}")
        else:
            messages.append(f"{sleeve} entry: {symbol} ${now[symbol]:.2f}")
    for symbol in sorted(set(before) - set(now)):
        if sleeve == "EXODUS":
            messages.append(f"EXODUS covered: {symbol}")
        else:
            messages.append(f"{sleeve} exit: {symbol}")
    return messages


def main() -> None:
    state_path = Path(os.environ.get("BOOK_NOTIFY_STATE", STATE_PATH))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(state_path.read_text())
    except Exception:
        state = {}
    first_run = not state

    enabled = os.environ.get("TELEGRAM_ENABLED", "").strip() == "1"
    for sleeve, path in BOOKS.items():
        now = read_positive_targets(path)
        if now is None:
            continue
        before = state.get(sleeve)
        if isinstance(before, dict) and not first_run:
            for text in diff_messages(sleeve, before, now):
                try:
                    sent = send_telegram_message(text, enabled=enabled, channel="main")
                except Exception:
                    sent = False
                print(f"{'sent' if sent else 'unsent'}: {text}")
        state[sleeve] = now

    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(state_path)
    if first_run:
        print("baselined without notifying")


if __name__ == "__main__":
    main()
