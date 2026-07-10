"""Precompute the PIT residual-momentum selection signal -> <root>/residual_momentum.parquet.

This is the offline half of the residual-momentum SELECTION gate (P3, operator-greenlit). It
computes, per (symbol, ts_ms) on the daily grid, the trailing common4-factor-residual momentum
known strictly before the decision (CAUSAL — see the timing proof below):

    residual_momentum[D] = sum_{d in [D-9, D-3]} residual_return[d]   (rolling_sum(7).shift(3))

where residual_return[d] is the day-d residual from the validated 6-factor risk model's per-day
cross-sectional regression restricted to the 4 always-present (klines/price) factors (common4 —
funding/premium are 38.8% null on binance, see binance-derivative-metrics-missing).

CAUSALITY (fixed 2026-06-03 — was a confirmed look-ahead; STATE.md "rmom look-ahead unconfirmed"):
the residual is fit against a FORWARD return (fit_factor_returns target_col='fwd_ret_1d' =
first_bar_close[d+2]/first_bar_close[d+1] - 1, daily_feature_panel._attach_forward_returns), so
residual_return[d] does NOT complete until first_bar_close[d+2] is available ≈ (d+2) 01:00 UTC. The
LIVE continuous consumer wakes from 00:00 UTC of day D and reads residual_momentum[day D]; for that
to be strictly PIT, the NEWEST summed residual_return must complete ≤ D 00:00 UTC, i.e. its index
≤ D-3. Hence shift(3): residual_momentum[D] = sum residual_return[D-9..D-3], whose newest term
residual_return[D-3] completes (D-1) 01:00 UTC < D 00:00 UTC. (The old shift(1) summed
residual_return[D-1], which completes D+1 01:00 UTC — up to ~25h of future data: that was the bug.)
The live/continuous join (continuous_events: floor to start-of-day D) is aligned
to the same trading day (date(ts_ms-1ms)).

Deployment note: the live continuous profile now uses the post-look-ahead shift(3)
rmom table with `rmom_quantile=0.25`. Treat any further rmom latency or target
change as a fresh research change requiring pre-registration.

The continuous engine left-joins this on (symbol, daily-grid ts_ms) to add a
`residual_momentum` column and keeps LOW residual-momentum names (short the
idiosyncratically-weak candidates).

When enabled, the continuous demo/paper sleeve joins this table on the CURRENT trading day's ts, so for the live
refresh `--end` MUST advance to today — otherwise the daily systemd refresh keeps writing a table that
ends in the past, the live join finds no row for today, the `is_not_null` filter empties the whole
cross-section, and the sleeve silently emits zero signal. `--end` therefore defaults to TOMORROW (UTC)
so today's `residual_momentum[today]` row is produced (the staleness fix is correct and independent of
the look-ahead debt above; advancing `--end` only stops the table going stale).

Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/precompute_residual_momentum.py [--root PATH ...] [--start D] [--end D]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import polars as pl  # noqa: E402
from liquidity_migration.risk_model import build_factor_panel, fit_factor_returns  # noqa: E402
from liquidity_migration.daily_feature_panel import MS_PER_DAY, _date_str_to_ms  # noqa: E402

SHARED = Path.home() / "SHARED_DATA"
# pad the panel start so the trailing residual window is warm at the first traded signal day
START = "2023-03-01"
COMMON4 = ["btc_beta", "xs_rank_ret_30d", "realized_vol_rank", "liquidity_rank"]
DEFAULT_ROOTS = [SHARED / "bybit_full_pit", SHARED / "binance_full_pit"]

# residual_momentum[D] = sum residual_return[D-9 .. D-3]: a rolling RMOM_WINDOW-day sum of the daily
# factor residual, shifted RMOM_CAUSAL_SHIFT days. The shift is the causality guarantee: residual_return
# is fit against a FORWARD return (fwd_ret_1d = first_bar_close[d+2]/first_bar_close[d+1]-1), so
# residual_return[d] only completes ≈(d+2) 01:00 UTC. The live continuous consumer reads
# residual_momentum[day D] from D 00:00 UTC, so the NEWEST summed residual_return must complete ≤ D
# 00:00, i.e. its index ≤ D-3 -> shift 3 (residual_return[D-3] completes (D-1) 01:00 < D 00:00). See
# the module docstring; pinned by test_residual_momentum_is_causal_shift3.
RMOM_WINDOW = 7
RMOM_CAUSAL_SHIFT = 3
# fit_factor_returns targets first-bar close[d+2] / first-bar close[d+1].
# The newest residual used by RMOM[D] is d=D-shift, and its target is complete
# at the first 1h bar close on d+2. These constants make the conservative A0
# causal-computability timestamp mechanically derivable without pretending an
# unrecorded historical cron publication time exists.
RMOM_FORWARD_TARGET_COMPLETION_DAYS = 2
RMOM_FIRST_BAR_CLOSE_OFFSET_HOURS = 1
DEFAULT_APPEND_OVERLAP_DAYS = 14
# Tables written before provenance existed can only be upgraded
# conservatively. The provisional edge is bounded by the forward-target lag,
# causal shift, and one preseeded day; treating the final five days per symbol
# as mutable is intentionally wider than the observed one-day maturation.
LEGACY_PROVISIONAL_TAIL_DAYS = RMOM_CAUSAL_SHIFT + 2


def residual_momentum_expr() -> "pl.Expr":
    """The (causal) residual-momentum polars expression. Factored out so the exact window+shift is a
    single source of truth and is unit-testable without running the full factor-panel build."""
    return (
        pl.col("residual_return")
        .rolling_sum(window_size=RMOM_WINDOW, min_samples=4)
        .shift(RMOM_CAUSAL_SHIFT)
        .over("symbol")
        .alias("residual_momentum")
    )


def _default_end() -> str:
    """Exclusive end = TOMORROW (UTC) so today's causal residual_momentum row is produced. Over-running
    the data is harmless: roots whose klines stop earlier simply produce no rows past their data."""
    return (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()


def _resolve_klines_dataset(root: Path, override: str | None) -> str:
    """Which kline store this root actually holds. Research full-PIT roots use ``klines_1h``; the
    live demo/paper roots store their WS-driven klines under ``event_demo_klines_1h``. The risk-model
    autodetect always assumes ``klines_1h``, so a refresh against a live root read zero klines and
    silently wrote nothing (the 2026-06-02 continuous zero-signal blackout). Sniff the real dir."""
    if override:
        return override
    if (root / "klines_1h").is_dir():
        return "klines_1h"
    if (root / "event_demo_klines_1h").is_dir():
        return "event_demo_klines_1h"
    return "klines_1h"  # default; an empty read is then reported by the EMPTY-panel branch


def _ms_to_date_str(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()


def _append_trailing_pad(resid: pl.DataFrame, *, end: str) -> pl.DataFrame:
    if resid.is_empty():
        return resid
    end_day = (_date_str_to_ms(end) // MS_PER_DAY) * MS_PER_DAY
    active_cutoff = end_day - 8 * MS_PER_DAY  # only pad symbols with data within a rolling window of `end`
    pad = (
        resid.group_by("symbol")
        .agg(pl.col("ts_ms").max().alias("_last"))
        .filter(pl.col("_last") >= active_cutoff)
        .with_columns(pl.int_ranges(pl.col("_last") + MS_PER_DAY, end_day + MS_PER_DAY, MS_PER_DAY).alias("ts_ms"))
        .explode("ts_ms")
        .filter(pl.col("ts_ms").is_not_null())
        .with_columns(pl.lit(None, dtype=pl.Float64).alias("residual_return"))
        .select("symbol", "ts_ms", "residual_return")
    )
    if pad.is_empty():
        return resid
    return pl.concat([resid, pad], how="vertical")


def _compute_signal(root: Path, *, start: str, end: str, klines_dataset: str | None = None) -> pl.DataFrame:
    kname = _resolve_klines_dataset(root, klines_dataset)
    print(f"[{root.name}] build factor panel + common4 residuals [{start}..{end}) (klines={kname}) ...", flush=True)
    panel = build_factor_panel(root, start=start, end=end, klines_dataset=kname)
    if panel.is_empty():
        print(f"[{root.name}] EMPTY panel -- skip")
        return pl.DataFrame(
            schema={
                "symbol": pl.String,
                "ts_ms": pl.Int64,
                "residual_momentum": pl.Float64,
                "is_provisional": pl.Boolean,
            }
        )
    _fr, resid = fit_factor_returns(panel, factor_cols=COMMON4)  # symbol, ts_ms, residual_return
    resid = resid.sort(["symbol", "ts_ms"]).select("symbol", "ts_ms", "residual_return")
    # The LIVE join floors `now` to TODAY's day_ts and exact-matches residual_momentum[day_ts]; but
    # residual_return[d] only completes at ≈(d+2) 01:00 UTC (forward-return target), so on a live root
    # the raw table ends ~2 days behind today and the live decile then drops EVERY symbol (the
    # is_not_null filter empties it -> the silent zero-signal blackout). residual_momentum[D] =
    # sum residual_return[D-9 .. D-3] (shift(3)) is strictly causal (its newest term completes
    # (D-1) 01:00 < D 00:00), so for any symbol still trading at the trailing edge we append
    # null-residual rows from its last residual day through `end` (= tomorrow UTC) and let the SAME
    # rolling_sum(7)+shift(3) carry the trailing real-residual sum onto today's (and tomorrow's,
    # covering the 00:00->00:20 daily-refresh rollover) row. Polars rolling_sum ignores in-window
    # nulls and counts only non-null obs against min_samples. A padded row whose newest required
    # residual is not yet present is explicitly PROVISIONAL: its value is causal but can mature when
    # that delayed residual arrives. Stable history remains immutable; provisional tail keys may be
    # refreshed by the append path.
    last_real = (
        resid.filter(pl.col("residual_return").is_not_null())
        .group_by("symbol")
        .agg(pl.col("ts_ms").max().alias("_last_real_ts_ms"))
    )
    resid = _append_trailing_pad(resid, end=end)
    return (
        resid.sort(["symbol", "ts_ms"])
        .join(last_real, on="symbol", how="left")
        .with_columns(residual_momentum_expr())
        .with_columns(
            ((pl.col("ts_ms") - RMOM_CAUSAL_SHIFT * MS_PER_DAY) > pl.col("_last_real_ts_ms"))
            .fill_null(True)
            .alias("is_provisional")
        )
        .select("symbol", "ts_ms", "residual_momentum", "is_provisional")
        .drop_nulls("residual_momentum")
    )


def _write_signal_atomic(path: Path, sig: pl.DataFrame) -> None:
    # Atomic write: a killed/failed refresh must not leave a torn parquet that the live join reads.
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        sig.write_parquet(tmp_path)
        tmp_path.replace(path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _validate_rmom_schema(sig: pl.DataFrame, *, path: Path) -> None:
    required = {"symbol", "ts_ms", "residual_momentum"}
    missing = required.difference(sig.columns)
    if missing:
        raise RuntimeError(f"{path} missing required residual_momentum columns: {sorted(missing)}")
    dupes = sig.group_by(["symbol", "ts_ms"]).len().filter(pl.col("len") > 1)
    if not dupes.is_empty():
        raise RuntimeError(f"{path} has duplicate residual_momentum keys; refusing append: {dupes.head(5).to_dicts()}")
    if "is_provisional" in sig.columns and sig.schema["is_provisional"] != pl.Boolean:
        raise RuntimeError(f"{path} is_provisional must be boolean")


def _with_provisional_provenance(sig: pl.DataFrame) -> pl.DataFrame:
    """Normalize provenance, conservatively upgrading a legacy three-column table."""
    if "is_provisional" in sig.columns:
        return sig.with_columns(pl.col("is_provisional").cast(pl.Boolean).fill_null(True))
    return (
        sig.with_columns(pl.col("ts_ms").max().over("symbol").alias("_legacy_symbol_max"))
        .with_columns(
            (pl.col("ts_ms") >= pl.col("_legacy_symbol_max") - LEGACY_PROVISIONAL_TAIL_DAYS * MS_PER_DAY).alias(
                "is_provisional"
            )
        )
        .drop("_legacy_symbol_max")
    )


def _assert_append_overlap_matches(
    existing: pl.DataFrame,
    rebuilt: pl.DataFrame,
    *,
    overlap_start_ms: int,
    overlap_end_ms: int,
    rtol: float = 1e-10,
    atol: float = 1e-12,
) -> int:
    old = (
        existing.filter((pl.col("ts_ms") >= overlap_start_ms) & (pl.col("ts_ms") <= overlap_end_ms))
        .filter(~pl.col("is_provisional"))
        .select(["symbol", "ts_ms", pl.col("residual_momentum").alias("old_residual_momentum")])
        .sort(["symbol", "ts_ms"])
    )
    new = (
        rebuilt.filter((pl.col("ts_ms") >= overlap_start_ms) & (pl.col("ts_ms") <= overlap_end_ms))
        .select(
            [
                "symbol",
                "ts_ms",
                pl.col("residual_momentum").alias("new_residual_momentum"),
                pl.col("is_provisional").alias("new_is_provisional"),
            ]
        )
        .sort(["symbol", "ts_ms"])
    )
    if old.is_empty():
        raise RuntimeError("residual_momentum append cannot verify overlap: stable existing overlap is empty")
    joined = old.join(new, on=["symbol", "ts_ms"], how="inner")
    if joined.height != old.height:
        raise RuntimeError(
            "residual_momentum append overlap key mismatch: "
            f"existing={old.height} rebuilt={new.height} matched={joined.height}; use --full-rewrite after inspection"
        )
    if joined["new_is_provisional"].any():
        raise RuntimeError(
            "residual_momentum append would demote stable overlap rows to provisional; source coverage regressed"
        )
    old_vals = joined["old_residual_momentum"].to_numpy()
    new_vals = joined["new_residual_momentum"].to_numpy()
    if not np.array_equal(np.isnan(old_vals), np.isnan(new_vals)):
        raise RuntimeError(
            "residual_momentum append overlap NaN positions changed; use --full-rewrite after inspection"
        )
    finite = ~np.isnan(old_vals)
    if finite.any() and not np.allclose(old_vals[finite], new_vals[finite], rtol=rtol, atol=atol):
        diffs = np.abs(old_vals[finite] - new_vals[finite])
        raise RuntimeError(
            "residual_momentum append overlap values changed: "
            f"max_abs_diff={float(diffs.max()):.12g}; use --full-rewrite after inspection"
        )
    return joined.height


def _append_signal(
    root: Path,
    *,
    existing: pl.DataFrame,
    end: str,
    klines_dataset: str | None,
    append_overlap_days: int,
) -> int:
    out_path = root / "residual_momentum.parquet"
    if append_overlap_days < 1:
        raise ValueError("append_overlap_days must be positive")
    _validate_rmom_schema(existing, path=out_path)
    existing = (
        _with_provisional_provenance(existing)
        .select(["symbol", "ts_ms", "residual_momentum", "is_provisional"])
        .sort(["symbol", "ts_ms"])
    )
    if existing.is_empty():
        sig = _compute_signal(root, start=START, end=end, klines_dataset=klines_dataset)
        _write_signal_atomic(out_path, sig.sort(["symbol", "ts_ms"]))
        print(f"[{root.name}] wrote {sig.height} rows (empty existing table) -> {out_path}", flush=True)
        return sig.height

    existing_max = int(existing["ts_ms"].max())
    end_day = (_date_str_to_ms(end) // MS_PER_DAY) * MS_PER_DAY
    if existing_max > end_day:
        print(
            f"[{root.name}] residual_momentum already current through {_ms_to_date_str(existing_max)} "
            f"(requested {_ms_to_date_str(end_day)}); skip append",
            flush=True,
        )
        return 0

    history_days = append_overlap_days + RMOM_WINDOW + RMOM_CAUSAL_SHIFT + 5
    append_start = _ms_to_date_str(existing_max - history_days * MS_PER_DAY)
    rebuilt = _compute_signal(root, start=append_start, end=end, klines_dataset=klines_dataset)
    _validate_rmom_schema(rebuilt, path=out_path)
    overlap_start_ms = existing_max - append_overlap_days * MS_PER_DAY
    matched = _assert_append_overlap_matches(
        existing, rebuilt, overlap_start_ms=overlap_start_ms, overlap_end_ms=existing_max
    )
    refreshed = rebuilt.filter(pl.col("ts_ms") >= overlap_start_ms).select(
        ["symbol", "ts_ms", "residual_momentum", "is_provisional"]
    )
    if refreshed.is_empty():
        print(f"[{root.name}] overlap ok ({matched} rows); no new residual_momentum rows to append", flush=True)
        return 0

    preserved = existing.filter(pl.col("ts_ms") < overlap_start_ms)
    combined = pl.concat([preserved, refreshed], how="vertical").sort(["symbol", "ts_ms"])
    _validate_rmom_schema(combined, path=out_path)
    _write_signal_atomic(out_path, combined)
    rows_added = combined.height - existing.height
    tail_rows = refreshed.filter(pl.col("ts_ms") > existing_max).height
    overlap_rows = refreshed.height - tail_rows
    print(
        f"[{root.name}] stable overlap ok ({matched} rows); refreshed {overlap_rows} overlap rows "
        f"and appended {tail_rows} tail rows (net {rows_added:+d}) "
        f"through {_ms_to_date_str(int(combined['ts_ms'].max()))} -> {out_path}",
        flush=True,
    )
    return max(rows_added, 0)


def precompute(
    root: Path,
    *,
    start: str,
    end: str,
    klines_dataset: str | None = None,
    append: bool = True,
    append_overlap_days: int = DEFAULT_APPEND_OVERLAP_DAYS,
) -> int:
    out_path = root / "residual_momentum.parquet"
    if append and out_path.exists():
        return _append_signal(
            root,
            existing=pl.read_parquet(out_path),
            end=end,
            klines_dataset=klines_dataset,
            append_overlap_days=append_overlap_days,
        )

    sig = _compute_signal(root, start=start, end=end, klines_dataset=klines_dataset)
    _write_signal_atomic(out_path, sig)
    max_day = sig["ts_ms"].max() if not sig.is_empty() else None
    print(f"[{root.name}] wrote {sig.height} rows (max ts_ms={max_day}) -> {out_path}", flush=True)
    return sig.height


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", action="append", default=None, help="data root(s); default both full-PIT roots")
    ap.add_argument("--start", default=START, help="inclusive panel start date (YYYY-MM-DD)")
    ap.add_argument(
        "--end",
        default=None,
        help="exclusive end date (YYYY-MM-DD); default = tomorrow UTC (keeps the live table fresh)",
    )
    ap.add_argument(
        "--klines-dataset",
        default=None,
        help="kline store name; default = sniff (klines_1h, else event_demo_klines_1h for live roots)",
    )
    ap.add_argument(
        "--full-rewrite",
        action="store_true",
        help="Recompute the full table and atomically replace residual_momentum.parquet instead of appending a checked tail.",
    )
    ap.add_argument(
        "--append-overlap-days",
        type=int,
        default=DEFAULT_APPEND_OVERLAP_DAYS,
        help="Existing trailing days to recompute and compare before appending new residual_momentum rows.",
    )
    args = ap.parse_args()
    end = args.end or _default_end()
    roots = [Path(r).expanduser() for r in args.root] if args.root else DEFAULT_ROOTS
    for root in roots:
        if not root.exists():
            print(f"[skip] {root} not found")
            continue
        precompute(
            root,
            start=args.start,
            end=end,
            klines_dataset=args.klines_dataset,
            append=not args.full_rewrite,
            append_overlap_days=args.append_overlap_days,
        )
    print("DONE precompute_residual_momentum", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
