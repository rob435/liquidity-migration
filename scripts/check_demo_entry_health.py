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


def _last_coverage_gap(df: pl.DataFrame) -> int | None:
    """Return the universe_coverage_gap from the most recent submit cycle.

    The cycle telemetry serialises universe_coverage as a struct/dict per row;
    we only need the latest reading because gaps are persistent until the
    next bootstrap completes. A non-zero gap means the strategy is
    signal-starved REGARDLESS of entry count — the watchdog must surface
    this even when entries > 0 happen to slip through."""
    if df.is_empty() or "universe_coverage" not in df.columns:
        return None
    last = df.tail(1).to_dicts()[0].get("universe_coverage")
    if not isinstance(last, dict):
        return None
    gap = last.get("coverage_gap")
    try:
        return int(gap) if gap is not None else None
    except (TypeError, ValueError):
        return None


def check_entries(
    *, data_root: Path, window_hours: int, min_cycles_per_hour: float = 0.8,
) -> tuple[int, str]:
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
    coverage_gap = _last_coverage_gap(df)
    # Latest signal the feature pipeline produced. Distinguishes "no NEW
    # signals (event-strategy sparsity)" from "feature pipeline lagging
    # behind real-time" — both manifest as stale-skips but have very
    # different remediation. Read from the most recent cycle row.
    latest_feature_ts_ms = 0
    if cycles and "latest_feature_ts_ms" in df.columns:
        latest_feature_ts_ms = int(df.tail(1).select(pl.col("latest_feature_ts_ms").fill_null(0)).item())
    feature_age_hours = (
        (time.time() * 1000 - latest_feature_ts_ms) / 3_600_000
        if latest_feature_ts_ms > 0 else 0.0
    )
    # Scale expected cycles by the actual data span within the window, not the
    # full window_hours. After a fresh deploy the oldest data point is recent,
    # so requiring a full 24h worth of cycles would always false-alert for the
    # first 24h of a new deployment.
    oldest_ts_ms = df.select(pl.col("ts_ms").min()).item() if cycles else cutoff_ms
    data_span_hours = (time.time() * 1000 - oldest_ts_ms) / 3_600_000
    effective_hours = min(window_hours, max(data_span_hours, 0.0))
    expected_cycles = int(effective_hours * 60 * min_cycles_per_hour)
    suffix = (
        f"({candidates} candidates seen, {cycles} cycles, {stale} stale-skips, "
        f"coverage_gap={coverage_gap}, latest_feature_age={feature_age_hours:.1f}h)"
    )

    if cycles == 0:
        return 2, f"no submit-cycles in last {window_hours}h"
    # Coverage gap means the universe doesn't reach the rank ceiling the
    # strategy needs to identify rocket-symbols — entries CANNOT fire, so
    # this is always an alert even when historical entries linger in the
    # ledger from before the gap appeared.
    if coverage_gap is not None and coverage_gap > 0:
        return 1, (
            f"ALERT: universe coverage gap={coverage_gap} prevents signal generation. "
            f"Strategy needs prior7 rank coverage up to required threshold but the "
            f"feature build only reaches the observed max. Bootstrap probably "
            f"incomplete or universe_rank_end too low. {suffix}"
        )
    # Cycle starvation: the daemon may have stalled, been killed, or never
    # restarted. Caught here distinct from "ran fine, zero signals."
    if cycles < expected_cycles:
        return 1, (
            f"ALERT: only {cycles} cycles in last {window_hours}h "
            f"(expected >= {expected_cycles}). Daemon may be crashing, OOM-looping, "
            f"or stuck in bootstrap. {suffix}"
        )
    if entries > 0:
        return 0, (
            f"healthy: {entries} entries executed in last {window_hours}h {suffix}"
        )
    # Disambiguate the three failure modes:
    #   - feature_age > MAX_ENTRY_LAG_MINUTES + small buffer: pipeline lag
    #     (feature build hasn't progressed past an old bar). Real bug —
    #     check kline store freshness, WS subscription state.
    #   - feature_age <= MAX_ENTRY_LAG_MINUTES BUT stale-skips > 0 AND
    #     candidates == 0: the same old signals are being re-detected but
    #     rejected on staleness. Real bug — MAX_ENTRY_LAG_MINUTES is tighter
    #     than the feature-build cadence.
    #   - feature_age fresh AND stale-skips == 0 AND candidates == 0:
    #     strategy is just sparse — no events fired in the window. Not a
    #     bug, expected behavior for a q40 / promoted profile.
    if feature_age_hours > 6.5:
        return 1, (
            f"ALERT: 0 entries in last {window_hours}h, latest signal is "
            f"{feature_age_hours:.1f}h old (feature pipeline behind). "
            f"Check kline store freshness vs Bybit klines (ws_klines may be silently "
            f"disconnected) and bootstrap completion. {suffix}"
        )
    if stale > 0 and candidates == 0:
        return 1, (
            f"ALERT: 0 entries in last {window_hours}h, {stale} stale-skips against "
            f"fresh-ish features ({feature_age_hours:.1f}h old). "
            f"MAX_ENTRY_LAG_MINUTES may be tighter than the feature-build cadence. "
            f"{suffix}"
        )
    if candidates == 0 and stale == 0:
        # Strategy is genuinely silent — sparse event firing, normal for
        # the q40 promoted profile when no large-volume events trigger.
        # Demote to info-level alert (still exit 1 so Telegram fires) but
        # don't suggest a fix that doesn't apply.
        return 1, (
            f"INFO: 0 entries in last {window_hours}h — no signals fired (sparse "
            f"strategy, features fresh at {feature_age_hours:.1f}h old). "
            f"{suffix}"
        )
    return 1, (
        f"ALERT: 0 entries in last {window_hours}h {suffix}. "
        f"Check universe coverage, event filters, and entry gates."
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
    p.add_argument(
        "--min-cycles-per-hour", type=float, default=0.8,
        help="Alert if observed cycle/hour rate is below this. Default 0.8 "
        "tolerates brief deploy gaps but catches a daemon stuck in bootstrap "
        "or in an OOM/restart loop.",
    )
    p.add_argument("--telegram", action="store_true", help="Post alert via Telegram if unhealthy")
    args = p.parse_args()

    code, msg = check_entries(
        data_root=args.data_root,
        window_hours=args.window_hours,
        min_cycles_per_hour=args.min_cycles_per_hour,
    )
    print(msg)
    if code != 0 and args.telegram:
        maybe_telegram(f"[liquidity-migration demo health] {msg}")
    return code


if __name__ == "__main__":
    sys.exit(main())
