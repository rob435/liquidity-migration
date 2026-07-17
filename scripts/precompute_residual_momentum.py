"""Precompute the causal PIT residual-momentum selection signal.

For each symbol on the daily grid:

    residual_momentum[D] = sum_{d in [D-9, D-3]} residual_return[d]   (rolling_sum(7).shift(3))

``residual_return[d]`` is fit to the forward return from first-bar close
``d+1`` to ``d+2`` and is available around ``d+2 01:00 UTC``. The consumer
reads day ``D`` at ``D 00:00 UTC``, so ``D-3`` is the newest computable term.
The join uses the same trading-day convention, ``date(ts_ms - 1ms)``.

The active profile keeps the lowest residual-momentum quartile. Demo/paper
consumers exact-join today's row, so the exclusive ``--end`` defaults to
tomorrow UTC to emit it; a stale table would suppress the whole cross-section.
Treat changes to this timing or target as new research.

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
from liquidity_migration.risk_model import (  # noqa: E402
    COMMON4_FACTOR_COLUMNS,
    build_factor_panel,
    fit_factor_returns,
)
from liquidity_migration.daily_feature_panel import MS_PER_DAY, _date_str_to_ms  # noqa: E402

SHARED = Path.home() / "SHARED_DATA"
# pad the panel start so the trailing residual window is warm at the first traded signal day
START = "2023-03-01"
COMMON4 = list(COMMON4_FACTOR_COLUMNS)
DEFAULT_ROOTS = [SHARED / "bybit_full_pit", SHARED / "binance_full_pit"]

# Shift three days so every residual in the day-D signal is computable by D 00:00 UTC.
RMOM_WINDOW = 7
RMOM_MIN_SAMPLES = 4
RMOM_CAUSAL_SHIFT = 3
DEFAULT_APPEND_OVERLAP_DAYS = 14


def residual_momentum_expr() -> "pl.Expr":
    """The (causal) residual-momentum polars expression. Factored out so the exact window+shift is a
    single source of truth and is unit-testable without running the full factor-panel build."""
    return (
        pl.col("residual_return")
        .rolling_sum(window_size=RMOM_WINDOW, min_samples=RMOM_MIN_SAMPLES)
        .shift(RMOM_CAUSAL_SHIFT)
        .over("symbol")
        .alias("residual_momentum")
    )


def _default_end() -> str:
    """Exclusive end = TOMORROW (UTC) so today's causal residual_momentum row is produced. Over-running
    the data is harmless: roots whose klines stop earlier simply produce no rows past their data."""
    return (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()


def _resolve_klines_dataset(root: Path, override: str | None) -> str:
    """Select the research or WS-driven kline dataset present in this root."""
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
    # A shifted rolling window can still emit causal values after a symbol's
    # final real residual.  Pad every symbol far enough to materialize those
    # final values, then stop once fewer than ``RMOM_MIN_SAMPLES`` real
    # residuals can remain in the window.  Tying padding to the global end date
    # made a full rebuild erase previously emitted keys when a symbol aged out:
    # the same source history then produced a different table depending on the
    # day it was built.
    trailing_days = RMOM_CAUSAL_SHIFT + RMOM_WINDOW - RMOM_MIN_SAMPLES
    pad = (
        resid.group_by("symbol")
        .agg(pl.col("ts_ms").max().alias("_last"))
        .filter(pl.col("_last") < end_day)
        .with_columns(
            pl.min_horizontal(
                pl.col("_last") + trailing_days * MS_PER_DAY,
                pl.lit(end_day, dtype=pl.Int64),
            ).alias("_pad_end")
        )
        .with_columns(
            pl.int_ranges(
                pl.col("_last") + MS_PER_DAY,
                pl.col("_pad_end") + MS_PER_DAY,
                MS_PER_DAY,
            ).alias("ts_ms")
        )
        .explode("ts_ms")
        .filter(pl.col("ts_ms").is_not_null())
        .with_columns(pl.lit(None, dtype=pl.Float64).alias("residual_return"))
        .select("symbol", "ts_ms", "residual_return")
    )
    if pad.is_empty():
        return resid
    return pl.concat([resid, pad], how="vertical")


def residual_momentum_from_residuals(
    resid: pl.DataFrame,
    *,
    end: str,
) -> pl.DataFrame:
    """Apply the canonical shift-3 window and explicit provisional-state owner."""

    required = {"symbol", "ts_ms", "residual_return"}
    missing = sorted(required - set(resid.columns))
    if missing:
        raise ValueError(f"residual owner input missing columns: {missing}")
    resid = resid.select(
        pl.col("symbol").cast(pl.String, strict=True),
        pl.col("ts_ms").cast(pl.Int64, strict=True),
        pl.col("residual_return").cast(pl.Float64, strict=True),
    ).sort(["symbol", "ts_ms"])
    if resid.is_empty():
        return pl.DataFrame(
            schema={
                "symbol": pl.String,
                "ts_ms": pl.Int64,
                "residual_momentum": pl.Float64,
                "is_provisional": pl.Boolean,
            }
        )
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
            (
                (pl.col("ts_ms") - RMOM_CAUSAL_SHIFT * MS_PER_DAY)
                > pl.col("_last_real_ts_ms")
            )
            .fill_null(True)
            .alias("is_provisional")
        )
        .select("symbol", "ts_ms", "residual_momentum", "is_provisional")
        .drop_nulls("residual_momentum")
    )


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
    # Append null residual rows through the exclusive boundary so shift(3) emits
    # today's exact-join key. Polars ignores those nulls inside the rolling sum.
    # Tail rows remain provisional until every delayed input has matured; stable
    # history is immutable while provisional keys may be refreshed.
    return residual_momentum_from_residuals(resid, end=end)


def _write_signal_atomic(path: Path, sig: pl.DataFrame) -> None:
    # Atomic write: a killed/failed refresh must not leave a torn parquet that the live join reads.
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        sig.write_parquet(tmp_path)
        tmp_path.replace(path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _validate_rmom_schema(sig: pl.DataFrame, *, path: Path) -> None:
    required = {"symbol", "ts_ms", "residual_momentum", "is_provisional"}
    missing = required.difference(sig.columns)
    if missing:
        raise RuntimeError(f"{path} missing required residual_momentum columns: {sorted(missing)}")
    dupes = sig.group_by(["symbol", "ts_ms"]).len().filter(pl.col("len") > 1)
    if not dupes.is_empty():
        raise RuntimeError(f"{path} has duplicate residual_momentum keys; refusing append: {dupes.head(5).to_dicts()}")
    if sig.schema["is_provisional"] != pl.Boolean:
        raise RuntimeError(f"{path} is_provisional must be boolean")


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
    existing = existing.select(["symbol", "ts_ms", "residual_momentum", "is_provisional"]).sort(
        ["symbol", "ts_ms"]
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
