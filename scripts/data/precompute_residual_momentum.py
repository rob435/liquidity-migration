"""Precompute the causal PIT residual-momentum selection signal.

For each symbol on the daily grid:

    residual_momentum[D] = sum_{d in [D-9, D-3]} residual_return[d]   (rolling_sum(7).shift(3))

``residual_return[d]`` is fit to the forward return from first-bar close
``d+1`` to ``d+2`` and is available around ``d+2 01:00 UTC``. The consumer
reads day ``D`` at ``D 00:00 UTC``, so ``D-3`` is the newest computable term.
The join uses the same trading-day convention, ``date(ts_ms - 1ms)``.

The active profile keeps the lowest residual-momentum quartile. Demo
consumers exact-join today's row, so the exclusive ``--end`` defaults to
tomorrow UTC to emit it; a stale table would suppress the whole cross-section.
Treat changes to this timing or target as new research.

Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/data/precompute_residual_momentum.py [--root PATH ...] [--start D] [--end D]
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

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import polars as pl  # noqa: E402
from liquidity_migration.research.panels.risk_model import (  # noqa: E402
    COMMON4_FACTOR_COLUMNS,
    build_factor_panel,
    fit_factor_returns,
)
from liquidity_migration.research.panels.daily_feature_panel import MS_PER_DAY, _date_str_to_ms  # noqa: E402
from liquidity_migration.research.panels.residual_momentum import (  # noqa: E402
    RMOM_CAUSAL_SHIFT,
    RMOM_MIN_SAMPLES,
    RMOM_WINDOW,
    residual_momentum_expr,
    residual_momentum_from_residuals,
)

__all__ = (
    "RMOM_CAUSAL_SHIFT",
    "RMOM_MIN_SAMPLES",
    "RMOM_WINDOW",
    "residual_momentum_expr",
    "residual_momentum_from_residuals",
)

SHARED = Path.home() / "SHARED_DATA"
# pad the panel start so the trailing residual window is warm at the first traded signal day
START = "2023-03-01"
COMMON4 = list(COMMON4_FACTOR_COLUMNS)
DEFAULT_ROOTS = [SHARED / "bybit_full_pit", SHARED / "binance_full_pit"]

DEFAULT_APPEND_OVERLAP_DAYS = 14


def _default_end() -> str:
    """Tomorrow UTC, exclusive, so today's causal row is produced.

    Over-running the data is harmless: a root whose klines stop earlier just
    produces no rows past its data.
    """
    return (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()


def _resolve_klines_dataset(root: Path, override: str | None) -> str:
    """Select the explicit research kline dataset, defaulting to canonical hourly bars."""
    del root
    return override or "klines_1h"


def _ms_to_date_str(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()


def _compute_signal(
    root: Path,
    *,
    start: str,
    end: str,
    klines_dataset: str | None = None,
    require_pit_membership: bool = False,
) -> pl.DataFrame:
    kname = _resolve_klines_dataset(root, klines_dataset)
    print(f"[{root.name}] build factor panel + common4 residuals [{start}..{end}) (klines={kname}) ...", flush=True)
    if require_pit_membership:
        panel = build_factor_panel(
            root,
            start=start,
            end=end,
            klines_dataset=kname,
            require_pit_membership=True,
        )
    else:
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
    # Appends null residual rows through the exclusive boundary so shift(3) emits
    # today's exact-join key; the rolling sum ignores them. Tail rows stay
    # provisional (refreshable) until every delayed input has matured.
    return residual_momentum_from_residuals(resid, end=end)


def _write_signal_atomic(path: Path, sig: pl.DataFrame) -> None:
    # Atomic write: a killed/failed refresh must not leave a torn parquet that the live join reads.
    path.parent.mkdir(parents=True, exist_ok=True)
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
            f"max_abs_diff={float(diffs.max()):.12g}. Two causes, and they need "
            "opposite responses: (a) a DELIBERATE change to the signal definition "
            "(RMOM_WINDOW / RMOM_MIN_SAMPLES / RMOM_CAUSAL_SHIFT, or the per-symbol "
            "calendar grid in liquidity_migration/research/panels/residual_momentum.py) makes exactly "
            "this raise fire once per root — confirm the recorded change point, then "
            "rerun with --full-rewrite; (b) no definition change means the SOURCE "
            "residuals moved under a stable row, which is drift to investigate before "
            "rewriting anything. A deployed daily refresh, if one is ever "
            "reinstated, should pass --full-rewrite because live roots are "
            "rolling stores, not stable archives."
        )
    return joined.height


def _append_signal(
    root: Path,
    *,
    out_path: Path,
    existing: pl.DataFrame,
    end: str,
    klines_dataset: str | None,
    append_overlap_days: int,
    require_pit_membership: bool,
) -> int:
    if append_overlap_days < 1:
        raise ValueError("append_overlap_days must be positive")
    _validate_rmom_schema(existing, path=out_path)
    existing = existing.select(["symbol", "ts_ms", "residual_momentum", "is_provisional"]).sort(
        ["symbol", "ts_ms"]
    )
    if existing.is_empty():
        sig = _compute_signal(
            root,
            start=START,
            end=end,
            klines_dataset=klines_dataset,
            require_pit_membership=require_pit_membership,
        )
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
    rebuilt = _compute_signal(
        root,
        start=append_start,
        end=end,
        klines_dataset=klines_dataset,
        require_pit_membership=require_pit_membership,
    )
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
    output_path: Path | None = None,
    require_pit_membership: bool = False,
) -> int:
    out_path = output_path or (root / "residual_momentum.parquet")
    if append and out_path.exists():
        return _append_signal(
            root,
            out_path=out_path,
            existing=pl.read_parquet(out_path),
            end=end,
            klines_dataset=klines_dataset,
            append_overlap_days=append_overlap_days,
            require_pit_membership=require_pit_membership,
        )

    sig = _compute_signal(
        root,
        start=start,
        end=end,
        klines_dataset=klines_dataset,
        require_pit_membership=require_pit_membership,
    )
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
        help="kline store name; default = klines_1h",
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
    ap.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Explicit output parquet for a single root; useful for immutable run-scoped research artifacts.",
    )
    ap.add_argument(
        "--require-pit-membership",
        action="store_true",
        help=(
            "Filter raw hourly bars through archive_trade_manifest before factor features and "
            "cross-sectional ranks. Required for decision-grade historical research."
        ),
    )
    args = ap.parse_args()
    end = args.end or _default_end()
    roots = [Path(r).expanduser() for r in args.root] if args.root else DEFAULT_ROOTS
    if args.output is not None and len(roots) != 1:
        ap.error("--output requires exactly one --root")
    output_path = args.output.expanduser().resolve() if args.output is not None else None
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
            output_path=output_path,
            require_pit_membership=args.require_pit_membership,
        )
    print("DONE precompute_residual_momentum", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
