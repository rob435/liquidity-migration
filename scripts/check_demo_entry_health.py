#!/usr/bin/env python3
"""Watchdog: alert if the demo short sleeve hasn't fired an entry in N hours.

Exits 0 if healthy (entries in the last window).
Exits 1 if no entries (alert).
Exits 2 on error reading ledger.

Use as a systemd timer or cron job:
    python scripts/check_demo_entry_health.py --data-root data/bybit-demo-event --window-hours 24

Add `--telegram` to post the alert via the same Telegram channel the demo uses
(reads TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID from env).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import polars as pl


def _latest_cycle_parquet(cycles_root: Path) -> Path | None:
    if not cycles_root.exists():
        return None
    date_dirs = sorted(cycles_root.glob("date=*"))
    if not date_dirs:
        return None
    latest_dir = date_dirs[-1]
    parts = sorted(latest_dir.glob("*.parquet"))
    return parts[-1] if parts else None


def check_entries(*, data_root: Path, window_hours: int) -> tuple[int, str]:
    cycles_root = data_root / "event_demo_cycles"
    latest = _latest_cycle_parquet(cycles_root)
    if latest is None:
        return 2, f"no cycle parquet found under {cycles_root}"

    cutoff_ms = int(time.time() * 1000) - window_hours * 3600 * 1000
    # Read recent days to cover the window
    parts: list[pl.DataFrame] = []
    for d in sorted((data_root / "event_demo_cycles").glob("date=*"))[-3:]:
        for p in d.glob("*.parquet"):
            try:
                parts.append(pl.read_parquet(p))
            except Exception as exc:  # noqa: BLE001
                return 2, f"read failed: {p}: {exc}"
    if not parts:
        return 2, "no parquet rows readable"
    df = pl.concat(parts, how="diagonal_relaxed").filter(
        (pl.col("mode") == "submit") & (pl.col("ts_ms") >= cutoff_ms)
    )
    cycles = df.height
    entries = int(df.select(pl.col("entries_executed").fill_null(0).sum()).item()) if cycles else 0
    candidates = int(df.select(pl.col("entry_candidates").fill_null(0).sum()).item()) if cycles else 0
    # events pipeline rollup
    stale = int(df.select(pl.col("skipped_stale").fill_null(0).sum()).item()) if cycles else 0

    if cycles == 0:
        return 2, f"no submit-cycles in last {window_hours}h"
    if entries > 0:
        return 0, (
            f"healthy: {entries} entries executed in last {window_hours}h "
            f"({candidates} candidates seen, {cycles} cycles, {stale} stale-skips)"
        )
    return 1, (
        f"ALERT: 0 entries in last {window_hours}h "
        f"({candidates} candidates seen, {cycles} cycles, {stale} stale-skips). "
        f"If stale-skips > 0, signals are being detected but rejected as too old — "
        f"check MAX_ENTRY_LAG_MINUTES vs the feature-build cadence. "
        f"If candidates also 0, no signals are firing — check universe coverage and event filters."
    )


def maybe_telegram(message: str) -> None:
    import os
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("(telegram skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID unset)")
        return
    try:
        from urllib import request, parse
        data = parse.urlencode({"chat_id": chat, "text": message, "disable_web_page_preview": "true"}).encode()
        req = request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001
        print(f"(telegram send failed: {exc})", file=sys.stderr)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data-root", required=True, type=Path, help="Demo data root (e.g. data/bybit-demo-event)")
    p.add_argument("--window-hours", type=int, default=24, help="Window to check for entries")
    p.add_argument("--telegram", action="store_true", help="Post alert via Telegram if unhealthy")
    args = p.parse_args()

    code, msg = check_entries(data_root=args.data_root, window_hours=args.window_hours)
    print(msg)
    if code != 0 and args.telegram:
        maybe_telegram(f"[liquidity-migration demo health] {msg}")
    return code


if __name__ == "__main__":
    sys.exit(main())
