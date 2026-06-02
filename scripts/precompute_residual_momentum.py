"""Precompute the PIT residual-momentum selection signal -> <root>/residual_momentum.parquet.

This is the offline half of the residual-momentum SELECTION gate (P3, operator-greenlit). It
computes, per (symbol, ts_ms) on the daily grid, the trailing common4-factor-residual momentum
known at the signal-close decision (strict lag1 = excludes the signal-day forward residual):

    residual_momentum[D] = sum_{d in [D-7, D-1]} residual_return[d]

where residual_return[d] is the day-d residual from the validated 6-factor risk model's per-day
cross-sectional regression restricted to the 4 always-present (klines/price) factors (common4 —
funding/premium are 38.8% null on binance, see binance-derivative-metrics-missing). PIT-clean:
residual_return[d] completes at d+1 (<= signal close), and the lag1 shift excludes the signal day.

The engine (volume_events.run_volume_event_research) left-joins this on (symbol, daily-grid ts_ms)
to add a `residual_momentum` column, gated by --liquidity-migration-residual-momentum-max
(keep LOW residual-momentum = short the idiosyncratically-weak candidates).

The LIVE continuous demo sleeve joins this table on the CURRENT trading day's ts, so for the live
refresh `--end` MUST advance to today — otherwise the daily systemd refresh keeps writing a table that
ends in the past, the live join finds no row for today, the `is_not_null` filter empties the whole
cross-section, and the sleeve silently emits zero signal. `--end` therefore defaults to TOMORROW (UTC)
so today's causal `residual_momentum[today]` row (built only from days <= today-1) is produced. This is
PIT-safe: `residual_return[d]` completes at d+1 and the lag1 `shift(1)` excludes the signal day, so
advancing the end can never leak look-ahead — it only stops the table going stale.

Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/precompute_residual_momentum.py [--root PATH ...] [--start D] [--end D]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import polars as pl  # noqa: E402
from liquidity_migration.risk_model import build_factor_panel, fit_factor_returns  # noqa: E402

SHARED = Path.home() / "SHARED_DATA"
# pad the panel start so the trailing-7d residual window is warm at the first traded signal day
START = "2023-03-01"
COMMON4 = ["btc_beta", "xs_rank_ret_30d", "realized_vol_rank", "liquidity_rank"]
DEFAULT_ROOTS = [SHARED / "bybit_full_pit", SHARED / "binance_full_pit"]


def _default_end() -> str:
    """Exclusive end = TOMORROW (UTC) so today's causal residual_momentum row is produced. Over-running
    the data is harmless: roots whose klines stop earlier simply produce no rows past their data."""
    return (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()


def precompute(root: Path, *, start: str, end: str) -> int:
    print(f"[{root.name}] build factor panel + common4 residuals [{start}..{end}) ...", flush=True)
    panel = build_factor_panel(root, start=start, end=end)
    if panel.is_empty():
        print(f"[{root.name}] EMPTY panel -- skip")
        return 0
    _fr, resid = fit_factor_returns(panel, factor_cols=COMMON4)  # symbol, ts_ms, residual_return
    sig = (
        resid.sort(["symbol", "ts_ms"])
        .with_columns(
            pl.col("residual_return").rolling_sum(window_size=7, min_samples=4).shift(1).over("symbol").alias("residual_momentum")
        )
        .select("symbol", "ts_ms", "residual_momentum")
        .drop_nulls("residual_momentum")
    )
    out_path = root / "residual_momentum.parquet"
    # Atomic write: a killed/failed refresh must not leave a torn parquet that the live join reads.
    tmp_path = out_path.with_name(f".{out_path.name}.{os.getpid()}.tmp")
    try:
        sig.write_parquet(tmp_path)
        tmp_path.replace(out_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    max_day = sig["ts_ms"].max() if not sig.is_empty() else None
    print(f"[{root.name}] wrote {sig.height} rows (max ts_ms={max_day}) -> {out_path}", flush=True)
    return sig.height


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", action="append", default=None, help="data root(s); default both full-PIT roots")
    ap.add_argument("--start", default=START, help="inclusive panel start date (YYYY-MM-DD)")
    ap.add_argument("--end", default=None, help="exclusive end date (YYYY-MM-DD); default = tomorrow UTC (keeps the live table fresh)")
    args = ap.parse_args()
    end = args.end or _default_end()
    roots = [Path(r).expanduser() for r in args.root] if args.root else DEFAULT_ROOTS
    for root in roots:
        if not root.exists():
            print(f"[skip] {root} not found")
            continue
        precompute(root, start=args.start, end=end)
    print("DONE precompute_residual_momentum", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
