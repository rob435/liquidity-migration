"""Phase 0 (continuous-fade plan §6) — does age300 + rmom make the c2b rolling signal ALL-WEATHER?

Reproduces the c2b continuous-decile precheck (baseline + age>=300) AND adds:
  - the residual-momentum gate (P3b / RD1's squeeze filter): within each ts keep the LOW-rmom
    half (short idiosyncratically-weak names), joined causally from <root>/residual_momentum.parquet
    (daily grid, lag1 PIT-clean) onto each hourly bar by day-floor (rmom[D] uses residuals <= D-1,
    known at 00:00 D, so a same-day-floor join to an hourly bar at t >= 00:00 D is causal).
  - an EARLY / RECENT split (boundary 2025-06-01, matching c2b's split168 and RD1's recent def)
    on every variant at 24 / 72 / 168 h.

The §6 fork (verdict): if age300+rmom flips the EARLY short-only AND beta-neutral L/S POSITIVE on
BOTH venues -> BB2 was a c2b universe/decile artifact, the continuous edge is real -> fast-track to
the engine-grade rolling backtest (A3/H3). If still early-negative on either venue -> BB2 is
structural -> pivot to Avenue C (regime gate / market-neutral) or file the null.

Variants per venue (within-ts decile of the 5-feature composite, same method as c2b):
  baseline          : full continuous panel (all symbols)              [reproduces c2b]
  age_ge_300        : true-age >= 300 d at the ts                       [reproduces c2b, E2 gate]
  rmom_lo           : within-ts LOW residual-momentum half (all ages)   [P3b gate, decompose]
  age_ge_300_rmom_lo: age>=300 AND within-ts low-rmom half              [the §6 target stack]

Read-outs (per horizon, per split): top-decile (D9) mean fwd ret (the short), short-only net
(-D9 - 15 bps), within-ts beta-neutral L/S net (D0 - D9 - 2x15 bps). EXPLORATORY: look-ahead
decile characterization (forward returns score deciles; within-ts ranks are full-sample). NOT a
backtest, NOT promotion evidence.

Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/p0_continuous_age_rmom_split.py
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
SPLIT_DATE = "2025-06-01"  # early (26 mo) / recent (12 mo) boundary — matches c2b split168 + RD1
MS_PER_DAY = 86_400_000
HORIZONS_H = [1, 3, 24, 72, 168]
SPLIT_HORIZONS = [24, 72, 168]
VENUES = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
FEATURES = ["rv_168h", "vov", "dist_low", "xsret7", "xsret3"]
ROUND_TRIP_BPS = 15.0
AGE_MIN_DAYS = 300
LISTING_SCAN_START = "2018-01-01"


def _continuous_features(root: Path) -> pl.DataFrame:
    """Per (symbol, ts_ms) hourly rolling features + forward returns, full-PIT, backward-only.

    Verbatim from c1_continuous_ic_precheck so the baseline/age columns reproduce c2b exactly.
    """
    start_ms, end_ms = _date_str_to_ms(START_DATE), _date_str_to_ms(END_DATE)
    kname = _autodetect_dataset_names(root)["klines_dataset"]
    k = _read_window(root, kname, start_ms=start_ms - 40 * MS_PER_DAY, end_ms=end_ms,
                     columns=["ts_ms", "symbol", "close"])
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
    for h in HORIZONS_H:
        k = k.with_columns((pl.col("close").shift(-h).over("symbol") / pl.col("close") - 1.0).alias(f"fwd_{h}h"))
    k = k.with_columns(
        pl.col("ret168").rank().over("ts_ms").alias("xsret7"),
        pl.col("ret72").rank().over("ts_ms").alias("xsret3"),
    )
    return k.filter(pl.col("ts_ms") >= start_ms)


def _first_listing_ms(root: Path) -> pl.DataFrame:
    """symbol -> first-ever kline ts (true PIT listing age), from a light (ts_ms, symbol) scan."""
    kname = _autodetect_dataset_names(root)["klines_dataset"]
    k = _read_window(root, kname, start_ms=_date_str_to_ms(LISTING_SCAN_START),
                     end_ms=_date_str_to_ms(END_DATE), columns=["ts_ms", "symbol"])
    if k.is_empty():
        return pl.DataFrame({"symbol": [], "first_ms": []})
    return k.group_by("symbol").agg(pl.col("ts_ms").min().alias("first_ms"))


def _attach_rmom(feat: pl.DataFrame, root: Path) -> pl.DataFrame:
    """Causal left-join of the daily lag1 residual_momentum onto each hourly bar (day-floor)."""
    p = root / "residual_momentum.parquet"
    if not p.exists():
        return feat.with_columns(pl.lit(None, dtype=pl.Float64).alias("residual_momentum"))
    rmom = pl.read_parquet(p).rename({"ts_ms": "day_ts"})
    feat = feat.with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day_ts"))
    return feat.join(rmom, on=["symbol", "day_ts"], how="left")


def _decile_perts(feat: pl.DataFrame) -> tuple[dict[int, pl.DataFrame], int]:
    """Within-ts composite decile (c2b math). Returns {h: frame(ts_ms, top, bot, ls)} + avg names/ts."""
    present = [f for f in FEATURES if f in feat.columns]
    feat = feat.with_columns([
        ((pl.col(f).rank().over("ts_ms") - 1) / (pl.len().over("ts_ms") - 1)).alias(f"_n_{f}")
        for f in present
    ])
    feat = feat.with_columns(pl.mean_horizontal([pl.col(f"_n_{f}") for f in present]).alias("composite"))
    feat = feat.with_columns(
        (((pl.col("composite").rank().over("ts_ms") - 1) * 10) // pl.len().over("ts_ms")).clip(0, 9).alias("decile")
    )
    avg_names = int(feat.group_by("ts_ms").len()["len"].mean()) if feat.height else 0
    out: dict[int, pl.DataFrame] = {}
    for h in HORIZONS_H:
        fwd = f"fwd_{h}h"
        sub = feat.select("ts_ms", "decile", fwd).drop_nulls()
        if sub.is_empty():
            continue
        dt = sub.group_by(["ts_ms", "decile"]).agg(pl.col(fwd).mean().alias("dm"))
        top = dt.filter(pl.col("decile") == 9).select("ts_ms", pl.col("dm").alias("top"))
        bot = dt.filter(pl.col("decile") == 0).select("ts_ms", pl.col("dm").alias("bot"))
        j = top.join(bot, on="ts_ms", how="inner").with_columns((pl.col("bot") - pl.col("top")).alias("ls"))
        if not j.is_empty():
            out[h] = j
    return out, avg_names


def _agg(j: pl.DataFrame, avg_names: int) -> dict | None:
    """Aggregate a (possibly ts-filtered) per-ts frame into the c2b read-out dict."""
    if j is None or j.is_empty():
        return None
    cost = ROUND_TRIP_BPS / 1e4
    ls = float(j["ls"].mean())
    top_mean = float(j["top"].mean())
    return {
        "top_decile_mean_fwd_bps": round(top_mean * 1e4, 1),
        "short_only_net_bps": round((-top_mean - cost) * 1e4, 1),
        "ls_net_bps": round((ls - 2 * cost) * 1e4, 1),
        "n_ts": int(j.height),
        "avg_names_per_ts": avg_names,
    }


def _variant_results(feat: pl.DataFrame, split_ms: int) -> dict:
    """Full-window read-outs at all horizons + early/recent splits at the split horizons."""
    perts, avg_names = _decile_perts(feat)
    res: dict = {}
    for h in HORIZONS_H:
        r = _agg(perts.get(h), avg_names)
        if r:
            res[f"{h}h"] = r
    for h in SPLIT_HORIZONS:
        j = perts.get(h)
        if j is None:
            continue
        early = _agg(j.filter(pl.col("ts_ms") < split_ms), avg_names)
        recent = _agg(j.filter(pl.col("ts_ms") >= split_ms), avg_names)
        res[f"split_{h}h"] = {"early": early, "recent": recent}
    return res


def _low_rmom_half(feat: pl.DataFrame) -> pl.DataFrame:
    """Keep within-ts LOW residual-momentum half (the squeeze filter: short the idiosyncratically weak)."""
    f = feat.filter(pl.col("residual_momentum").is_not_null())
    f = f.with_columns(
        ((pl.col("residual_momentum").rank().over("ts_ms") - 1) / (pl.len().over("ts_ms") - 1)).alias("_rmom_rank")
    )
    return f.filter(pl.col("_rmom_rank") <= 0.5)


def main() -> int:
    split_ms = _date_str_to_ms(SPLIT_DATE)
    print(f"P0 continuous age+rmom split  window={START_DATE}->{END_DATE}  split={SPLIT_DATE}  "
          f"age>={AGE_MIN_DAYS}d  cost={ROUND_TRIP_BPS}bps/leg/hold\n", flush=True)
    out: dict = {}
    for venue, root in VENUES.items():
        if not root.exists():
            print(f"SKIP {venue}: {root} not found")
            continue
        print(f"[{venue}] first-listing scan ...", flush=True)
        first = _first_listing_ms(root)
        print(f"[{venue}] build continuous panel ...", flush=True)
        feat = _continuous_features(root)
        if feat.is_empty():
            print(f"[{venue}] EMPTY -- skip")
            continue
        feat = feat.join(first, on="symbol", how="left").with_columns(
            ((pl.col("ts_ms") - pl.col("first_ms")) / MS_PER_DAY).alias("age_days")
        )
        feat = _attach_rmom(feat, root)
        rmom_cov = float(feat["residual_momentum"].is_not_null().mean()) if feat.height else 0.0
        print(f"[{venue}] rows={feat.height}  rmom_coverage={rmom_cov:.1%}", flush=True)

        aged = feat.filter(pl.col("age_days") >= AGE_MIN_DAYS)
        variants = {
            "baseline": feat,
            f"age_ge_{AGE_MIN_DAYS}": aged,
            "rmom_lo": _low_rmom_half(feat),
            f"age_ge_{AGE_MIN_DAYS}_rmom_lo": _low_rmom_half(aged),
        }
        venue_res: dict = {}
        for tag, vfeat in variants.items():
            venue_res[tag] = _variant_results(vfeat, split_ms)
        out[venue] = venue_res

        # readable summary: 72h + 168h full, then early/recent
        for tag in variants:
            for h in (72, 168):
                r = venue_res[tag].get(f"{h}h")
                if r:
                    print(f"[{venue}] {tag:22s} {h:3d}h  D9_fwd={r['top_decile_mean_fwd_bps']:+7.0f}  "
                          f"short_net={r['short_only_net_bps']:+7.0f}  LS_net={r['ls_net_bps']:+7.0f}  "
                          f"n/ts={r['avg_names_per_ts']}", flush=True)
        print(f"[{venue}] --- EARLY/RECENT split @168h (the all-weather test) ---", flush=True)
        for tag in variants:
            sp = venue_res[tag].get("split_168h")
            if not sp:
                continue
            e, rc = sp.get("early"), sp.get("recent")
            if e and rc:
                print(f"[{venue}] {tag:22s} EARLY short={e['short_only_net_bps']:+7.0f} LS={e['ls_net_bps']:+7.0f}"
                      f"  |  RECENT short={rc['short_only_net_bps']:+7.0f} LS={rc['ls_net_bps']:+7.0f}", flush=True)
        print(flush=True)

    out_path = SHARED / "p0_continuous_age_rmom_2026-05-31.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"DONE -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
