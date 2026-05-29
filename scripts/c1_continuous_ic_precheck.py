"""C1 pre-check — do the ROLLING (continuous) versions of the 5 IC features carry
cross-venue IC at intraday horizons? Decisive Architecture-B edge test BEFORE building
the full C0 continuous engine (~5-7 days).

Rationale: the daily strategy (Architecture A) is a documented null under honest costs
(R9). Architecture B bets the SAME liquidity-migration features have exploitable signal at
sub-daily (rolling) horizons. This tests that directly and cheaply: compute hourly rolling
versions of the 5 Phase-5 IC survivors on klines_1h (full-PIT), and measure rank IC vs
forward returns at {1,3,24,72,168}h on BOTH venues. If the rolling features lack
cross-venue-consistent IC, Architecture B is also a null and the full C0 engine is not
worth building; if they show consistent IC (esp. strengthening intraday), build C0.

Rolling features (per symbol, hourly bars, strictly backward — PIT-clean):
  realized_vol_7d   -> rv_168h   = std(1h returns) over trailing 168h
  vol_of_vol_30d    -> vov       = std(rv_168h) over trailing 720h
  dist_from_30d_low -> dist_low  = (close - min_720h) / (max_720h - min_720h)
  xs_rank_ret_7d    -> XS rank of ret_168h
  xs_rank_ret_3d    -> XS rank of ret_72h
IC = mean over timestamps of the per-timestamp cross-sectional Spearman(feature, fwd_ret)
(rank both within the timestamp, Pearson of ranks = Spearman) — matches Phase-5/R2.

Read-only; one venue at a time (memory-safe). Dispatch (5950X, Windows):
    $env:POLARS_MAX_THREADS=8; .venv\\Scripts\\python.exe -u scripts\\c1_continuous_ic_precheck.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

from liquidity_migration.signal_harness import (  # noqa: E402
    _autodetect_dataset_names,
    _date_str_to_ms,
    _read_window,
)

SHARED = Path.home() / "SHARED_DATA"
START_DATE, END_DATE = "2023-04-01", "2026-05-28"
MS_PER_DAY = 86_400_000
HORIZONS_H = [1, 3, 24, 72, 168]
VENUES = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
# rolling-feature column -> the daily IC feature it mirrors (all negative IC = short-side)
FEATURES = ["rv_168h", "vov", "dist_low", "xsret7", "xsret3"]


def _continuous_features(root: Path) -> pl.DataFrame:
    """Per (symbol, ts_ms) hourly rolling features + forward returns, full-PIT, backward-only."""
    start_ms, end_ms = _date_str_to_ms(START_DATE), _date_str_to_ms(END_DATE)
    kname = _autodetect_dataset_names(root)["klines_dataset"]
    # pad 40d back so 720h (30d) windows warm up at START_DATE
    k = _read_window(root, kname, start_ms=start_ms - 40 * MS_PER_DAY, end_ms=end_ms, columns=["ts_ms", "symbol", "close"])
    if k.is_empty():
        return pl.DataFrame()
    k = k.filter(pl.col("close") > 0).unique(["symbol", "ts_ms"]).sort(["symbol", "ts_ms"])
    ret1 = (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0)
    k = k.with_columns(ret1.alias("ret1"))
    k = k.with_columns(
        pl.col("ret1").rolling_std(window_size=168, min_samples=48).over("symbol").alias("rv_168h"),
        (pl.col("close") / pl.col("close").shift(72).over("symbol") - 1.0).alias("ret72"),
        (pl.col("close") / pl.col("close").shift(168).over("symbol") - 1.0).alias("ret168"),
        pl.col("close").rolling_min(window_size=720, min_samples=168).over("symbol").alias("min720"),
        pl.col("close").rolling_max(window_size=720, min_samples=168).over("symbol").alias("max720"),
    )
    k = k.with_columns(
        pl.col("rv_168h").rolling_std(window_size=720, min_samples=168).over("symbol").alias("vov"),
        pl.when(pl.col("max720") > pl.col("min720"))
        .then((pl.col("close") - pl.col("min720")) / (pl.col("max720") - pl.col("min720")))
        .otherwise(None).alias("dist_low"),
    )
    # forward returns at each horizon (strictly forward)
    for h in HORIZONS_H:
        k = k.with_columns((pl.col("close").shift(-h).over("symbol") / pl.col("close") - 1.0).alias(f"fwd_{h}h"))
    # XS-rank momentum features per ts (xsret7<-ret168, xsret3<-ret72); rv/vov/dist used raw (IC ranks them)
    k = k.with_columns(
        pl.col("ret168").rank().over("ts_ms").alias("xsret7"),
        pl.col("ret72").rank().over("ts_ms").alias("xsret3"),
    )
    # restrict to the evaluation window (drop the 40d warm-up pad)
    return k.filter(pl.col("ts_ms") >= start_ms)


def _rank_ic(df: pl.DataFrame, feat: str, fwd: str) -> tuple[float, int]:
    """Mean over timestamps of the per-ts cross-sectional Spearman(feat, fwd)."""
    sub = df.select("ts_ms", feat, fwd).drop_nulls()
    if sub.is_empty():
        return 0.0, 0
    sub = sub.with_columns(
        pl.col(feat).rank().over("ts_ms").alias("_fr"),
        pl.col(fwd).rank().over("ts_ms").alias("_yr"),
    )
    per_ts = sub.group_by("ts_ms").agg(pl.corr("_fr", "_yr").alias("ic"), pl.len().alias("n")).filter(pl.col("n") >= 5)
    if per_ts.is_empty():
        return 0.0, 0
    return float(per_ts["ic"].mean()), int(per_ts.height)


def main() -> int:
    print(f"C1 continuous-IC pre-check  window={START_DATE}->{END_DATE}  horizons(h)={HORIZONS_H}\n", flush=True)
    out: dict = {}
    for venue, root in VENUES.items():
        if not root.exists():
            print(f"SKIP {venue}: {root} not found")
            continue
        print(f"[{venue}] build continuous feature panel ...", flush=True)
        feat = _continuous_features(root)
        if feat.is_empty():
            print(f"[{venue}] EMPTY -- skip")
            continue
        print(f"[{venue}] rows={feat.height}", flush=True)
        venue_res: dict = {}
        for f in FEATURES:
            venue_res[f] = {}
            for h in HORIZONS_H:
                ic, n = _rank_ic(feat, f, f"fwd_{h}h")
                venue_res[f][f"{h}h"] = round(ic, 4)
            print(f"[{venue}] {f:9s} IC: " + "  ".join(f"{h}h={venue_res[f][f'{h}h']:+.4f}" for h in HORIZONS_H), flush=True)
        out[venue] = venue_res
        print(flush=True)
    (SHARED / "c1_continuous_ic_precheck_2026-05-29.json").write_text(json.dumps(out, indent=2))
    print("DONE -> c1_continuous_ic_precheck_2026-05-29.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
