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


def ws_first_ratio(df: pl.DataFrame) -> dict[str, float]:
    """Fraction of cycles served by the WS-fed caches vs the REST fallback.

    The cycle persists ``ticker_source`` / ``private_snapshot_source`` as
    ``'ws_cache'`` (sub-50ms WS snapshot) or ``'rest'`` (fallback because the
    cache was stale/unseeded). A low WS-first % over many cycles means the WS
    pipeline is effectively dead and production is on the slow REST path — the
    exact silent regression the WS engineering exists to prevent. Returns
    ``{}`` when the columns are absent (legacy ledgers)."""
    out: dict[str, float] = {}
    rows = df.height
    if rows == 0:
        return out
    if "ticker_source" in df.columns:
        ws_n = df.filter(pl.col("ticker_source") == "ws_cache").height
        out["ticker_pct"] = 100.0 * ws_n / rows
    if "private_snapshot_source" in df.columns:
        ws_n = df.filter(pl.col("private_snapshot_source") == "ws_cache").height
        out["private_pct"] = 100.0 * ws_n / rows
    # Only return when BOTH are present so the caller's f-string is safe.
    return out if {"ticker_pct", "private_pct"} <= out.keys() else {}


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
    # `skipped_stale` in each cycle row is the PER-CYCLE count of candidates
    # the cycle just rejected as stale. Summing across cycles (the old
    # behavior) overstates magnitude wildly — the same 44 candidates from
    # the 04:00 UTC bar got re-counted ~700 times to produce the misleading
    # "32833 stale-skips" alert on 2026-05-25. Use the LATEST cycle's count
    # as the representative per-cycle figure; if you want lifetime totals
    # the daemon's own running stats are the right source, not this watchdog.
    stale_per_cycle = (
        int(df.tail(1).select(pl.col("skipped_stale").fill_null(0)).item())
        if cycles and "skipped_stale" in df.columns
        else 0
    )
    coverage_gap = _last_coverage_gap(df)
    # Two ages we need to tell apart:
    #   - kline_age: how stale the WS-fed kline store is. >6h here = real
    #     feed bug (WS disconnected, bootstrap stuck).
    #   - feature_age: how old the latest EVENT detected by the strategy
    #     is. Sparse strategies (q40 promoted) can have feature_age >> 6h
    #     while klines are perfectly fresh, just because no event passed
    #     the filter for many hours.
    # Reading just feature_age conflates the two and misdiagnoses sparse
    # strategy as feed lag (observed 2026-05-25 alert chain).
    now_ms = int(time.time() * 1000)
    latest_feature_ts_ms = 0
    kline_store_max_ts_ms = 0
    if cycles:
        if "latest_feature_ts_ms" in df.columns:
            latest_feature_ts_ms = int(df.tail(1).select(pl.col("latest_feature_ts_ms").fill_null(0)).item())
        if "kline_store_max_ts_ms" in df.columns:
            kline_store_max_ts_ms = int(df.tail(1).select(pl.col("kline_store_max_ts_ms").fill_null(0)).item())
    feature_age_hours = (now_ms - latest_feature_ts_ms) / 3_600_000 if latest_feature_ts_ms > 0 else 0.0
    kline_age_hours = (now_ms - kline_store_max_ts_ms) / 3_600_000 if kline_store_max_ts_ms > 0 else float("nan")
    # Scale expected cycles by the actual data span within the window, not the
    # full window_hours. After a fresh deploy the oldest data point is recent,
    # so requiring a full 24h worth of cycles would always false-alert for the
    # first 24h of a new deployment.
    oldest_ts_ms = df.select(pl.col("ts_ms").min()).item() if cycles else cutoff_ms
    data_span_hours = (time.time() * 1000 - oldest_ts_ms) / 3_600_000
    effective_hours = min(window_hours, max(data_span_hours, 0.0))
    expected_cycles = int(effective_hours * 60 * min_cycles_per_hour)
    kline_age_text = f"{kline_age_hours:.1f}h" if kline_store_max_ts_ms > 0 else "unknown"
    # WS-vs-REST reality (observability): the cycle persists ticker_source /
    # private_snapshot_source ('ws_cache' when served by the WS-fed cache, 'rest'
    # on fallback). Surfacing the WS-first % makes a silently-dead WS feed
    # visible — without it, production could run REST-only (the slow path the WS
    # pipeline exists to avoid) and nobody would know.
    ws = ws_first_ratio(df) if cycles else {}
    ws_text = ""
    if ws:
        ws_text = (
            f", ws_first=ticker:{ws['ticker_pct']:.0f}%/private:{ws['private_pct']:.0f}%"
        )
    suffix = (
        f"({candidates} candidates seen, {cycles} cycles, {stale_per_cycle} stale-skips/cycle, "
        f"coverage_gap={coverage_gap}, kline_age={kline_age_text}, "
        f"latest_feature_age={feature_age_hours:.1f}h{ws_text})"
    )

    if cycles == 0:
        return 2, f"no submit-cycles in last {window_hours}h"
    # Coverage gap means the universe doesn't reach the rank ceiling the
    # strategy needs to identify rocket-symbols — entries CANNOT fire, so
    # this is always an alert even when historical entries linger in the
    # ledger from before the gap appeared.
    if coverage_gap is not None and coverage_gap > 0:
        return 1, (
            f"ALERT: universe coverage gap={coverage_gap} blocks signal generation. "
            f"The strategy needs prior7 rank coverage up to the required threshold "
            f"but the feature build only reaches the observed max. "
            f"Action: wait for bootstrap to complete; if persistent, raise "
            f"UNIVERSE_RANK_END in the demo service env. {suffix}"
        )
    # Cycle starvation: the daemon may have stalled, been killed, or never
    # restarted. Caught here distinct from "ran fine, zero signals."
    if cycles < expected_cycles:
        return 1, (
            f"ALERT: only {cycles} cycles in last {window_hours}h "
            f"(expected >= {expected_cycles}). The daemon may be crashing, "
            f"OOM-looping, or stuck in bootstrap. "
            f"Action: systemctl status liquidity-migration-bybit-demo + "
            f"journalctl -u liquidity-migration-bybit-demo --since '1h ago'. {suffix}"
        )
    if entries > 0:
        return 0, (
            f"healthy: {entries} entries executed in last {window_hours}h {suffix}"
        )
    # Diagnose using kline_age (real feed freshness) FIRST, then feature_age
    # (which can lag for sparse strategies even when feed is fine):
    #
    #   - kline_age > 2h: the WS-fed kline store is genuinely behind. Real
    #     bug; the rest of the cycle is doing what it can with stale data.
    #   - kline_age fresh + feature_age old + stale > 0: same old signals
    #     re-detected each cycle, all rejected as stale. Most commonly
    #     just sparse-event strategy (q40 promoted typically fires 1-3
    #     candidates per day and nothing more); bumping MAX_ENTRY_LAG
    #     wouldn't help because those candidates are already-traded
    #     symbols blocked by "open position blocks re-entry".
    #   - kline_age fresh + candidates == 0 + stale == 0: truly silent.
    #     Strategy is sparse; no events triggered. Normal.
    #
    # kline_age==NaN means an older cycle ledger doesn't carry the field
    # yet — fall back to the old feature_age heuristic with a softer hint.
    import math
    have_kline_age = kline_store_max_ts_ms > 0 and not math.isnan(kline_age_hours)

    if have_kline_age and kline_age_hours > 2.0:
        return 1, (
            f"ALERT: kline feed stale — store is {kline_age_hours:.1f}h behind "
            f"real time. WS klines are silently disconnected or bootstrap stuck. "
            f"0 entries fired in {window_hours}h. "
            f"Action: journalctl -u liquidity-migration-bybit-demo for "
            f"kline_stream_manager errors; restart the service if no recovery. {suffix}"
        )
    # The remaining cases are NORMAL operating modes for a sparse-event
    # strategy. Return exit code 0 so the watchdog timer does NOT send a
    # telegram — they're just observed states, not problems. The text is
    # still printed to the journal in case an operator runs the script
    # manually or greps the timer's last run.
    if stale_per_cycle > 0 and candidates == 0:
        return 0, (
            f"OK (sparse): no new signals — same {stale_per_cycle} candidates from "
            f"{feature_age_hours:.1f}h ago re-detected each cycle and rejected "
            f"as past MAX_ENTRY_LAG. Kline feed fresh; strategy is just quiet. {suffix}"
        )
    if candidates == 0 and stale_per_cycle == 0:
        return 0, (
            f"OK (silent): no signals fired in {window_hours}h. Kline feed "
            f"fresh; strategy is just quiet. {suffix}"
        )
    return 1, (
        f"ALERT: 0 entries in last {window_hours}h with {candidates} candidates "
        f"+ {stale_per_cycle} stale-skips/cycle — falls outside the known sparse / "
        f"stale-feed patterns. Check universe coverage, event filters, and entry gates. {suffix}"
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
