"""Phase 0b (continuous-fade plan §6) — STRESS the Phase-0 rmom-flips-all-weather finding.

Phase 0 (p0_continuous_age_rmom_split.py) found the PIT-clean residual-momentum gate flips the
continuous decile short ALL-WEATHER on both venues (early short_only & beta-neutral L/S positive),
where age300-alone stayed recent-only. A +120 bps early swing from one gate demands MORE scrutiny.
This battery tries to break it three ways, all read-only on the same panel:

  A. ENTRY-LATENCY (the mandatory backtest-integrity gate): the panel uses 0-delay (features at t,
     forward return from close[t]). D9 fades from hour 1 (-15 bps) — real short-term reversal, or a
     closing-print microstructure artifact? Re-price the forward return at entry delay {0,1,3}h. If
     the edge collapses at +1h it is a same-bar mirage; if it survives it is capturable.
  B. THRESHOLD MONOTONICITY: rmom bottom-{50,33,25}% within ts. A monotone strengthening = robust
     squeeze filter; a knife-edge at 50% = fragile/mined.
  C. FINER TIME-SPLITS: is "early positive" uniform across 2023 / 2024 / 2025H1, or one good window
     (the c2b coarse-split trap)? Bucket short_only + L/S by calendar period.

rmom join + decile math are identical to Phase 0 (and reproduce c2b). EXPLORATORY: look-ahead decile
characterization, per-ts mean forward returns (NOT a backtest — no concurrency/overlap/funding). NOT
promotion evidence; this decides whether the engine-grade rolling backtest (A3/H3) is justified.

Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/p0b_continuous_rmom_stress.py
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
VENUES = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
FEATURES = ["rv_168h", "vov", "dist_low", "xsret7", "xsret3"]
ROUND_TRIP_BPS = 15.0
AGE_MIN_DAYS = 300
LISTING_SCAN_START = "2018-01-01"

DELAYS = [0, 1, 3]           # entry latency in hours after the signal bar close
HOLDS = [24, 72, 168]        # hold horizons
RMOM_QS = [0.50, 0.33, 0.25]  # within-ts low-rmom keep fractions
# finer time buckets (the c2b coarse-split trap check); "early" = first three
BUCKETS = [
    ("2023", "2023-04-01", "2024-01-01"),
    ("2024", "2024-01-01", "2025-01-01"),
    ("2025H1", "2025-01-01", "2025-06-01"),
    ("recent", "2025-06-01", "2026-05-29"),
]


def _build_panel(root: Path) -> pl.DataFrame:
    """Hourly rolling features (= c1/c2b) + delayed forward returns f_d{d}_h{h} = c[t+d+h]/c[t+d]-1."""
    start_ms, end_ms = _date_str_to_ms(START_DATE), _date_str_to_ms(END_DATE)
    kname = _autodetect_dataset_names(root)["klines_dataset"]
    k = _read_window(root, kname, start_ms=start_ms - 40 * MS_PER_DAY, end_ms=end_ms,
                     columns=["ts_ms", "symbol", "close"])
    if k.is_empty():
        return pl.DataFrame()
    k = k.filter(pl.col("close") > 0).unique(["symbol", "ts_ms"]).sort(["symbol", "ts_ms"])
    k = k.with_columns((pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("ret1"))
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
    # delayed forward returns: enter at close[t+d], exit at close[t+d+h]
    for d in DELAYS:
        for h in HOLDS:
            k = k.with_columns(
                (pl.col("close").shift(-(d + h)).over("symbol") / pl.col("close").shift(-d).over("symbol") - 1.0)
                .alias(f"f_d{d}_h{h}")
            )
    k = k.with_columns(
        pl.col("ret168").rank().over("ts_ms").alias("xsret7"),
        pl.col("ret72").rank().over("ts_ms").alias("xsret3"),
    )
    return k.filter(pl.col("ts_ms") >= start_ms)


def _first_listing_ms(root: Path) -> pl.DataFrame:
    kname = _autodetect_dataset_names(root)["klines_dataset"]
    k = _read_window(root, kname, start_ms=_date_str_to_ms(LISTING_SCAN_START),
                     end_ms=_date_str_to_ms(END_DATE), columns=["ts_ms", "symbol"])
    if k.is_empty():
        return pl.DataFrame({"symbol": [], "first_ms": []})
    return k.group_by("symbol").agg(pl.col("ts_ms").min().alias("first_ms"))


def _attach_rmom(feat: pl.DataFrame, root: Path) -> pl.DataFrame:
    p = root / "residual_momentum.parquet"
    if not p.exists():
        return feat.with_columns(pl.lit(None, dtype=pl.Float64).alias("residual_momentum"))
    rmom = pl.read_parquet(p).rename({"ts_ms": "day_ts"})
    feat = feat.with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day_ts"))
    return feat.join(rmom, on=["symbol", "day_ts"], how="left")


def _low_rmom(feat: pl.DataFrame, q: float) -> pl.DataFrame:
    f = feat.filter(pl.col("residual_momentum").is_not_null())
    f = f.with_columns(
        ((pl.col("residual_momentum").rank().over("ts_ms") - 1) / (pl.len().over("ts_ms") - 1)).alias("_rr")
    )
    return f.filter(pl.col("_rr") <= q)


def _assign_decile(feat: pl.DataFrame) -> pl.DataFrame:
    present = [f for f in FEATURES if f in feat.columns]
    feat = feat.with_columns([
        ((pl.col(f).rank().over("ts_ms") - 1) / (pl.len().over("ts_ms") - 1)).alias(f"_n_{f}")
        for f in present
    ])
    feat = feat.with_columns(pl.mean_horizontal([pl.col(f"_n_{f}") for f in present]).alias("composite"))
    return feat.with_columns(
        (((pl.col("composite").rank().over("ts_ms") - 1) * 10) // pl.len().over("ts_ms")).clip(0, 9).alias("decile")
    )


def _perts(feat_dec: pl.DataFrame, fwd: str) -> pl.DataFrame:
    """Per-ts (top=D9, bot=D0, ls=bot-top) for one forward column."""
    sub = feat_dec.select("ts_ms", "decile", fwd).drop_nulls()
    if sub.is_empty():
        return pl.DataFrame()
    dt = sub.group_by(["ts_ms", "decile"]).agg(pl.col(fwd).mean().alias("dm"))
    top = dt.filter(pl.col("decile") == 9).select("ts_ms", pl.col("dm").alias("top"))
    bot = dt.filter(pl.col("decile") == 0).select("ts_ms", pl.col("dm").alias("bot"))
    return top.join(bot, on="ts_ms", how="inner").with_columns((pl.col("bot") - pl.col("top")).alias("ls"))


def _net(j: pl.DataFrame, lo: int | None = None, hi: int | None = None) -> tuple[float, float, int]:
    """short_only_net, ls_net (bps), n_ts over an optional ts window [lo,hi)."""
    if j.is_empty():
        return float("nan"), float("nan"), 0
    if lo is not None:
        j = j.filter(pl.col("ts_ms") >= lo)
    if hi is not None:
        j = j.filter(pl.col("ts_ms") < hi)
    if j.is_empty():
        return float("nan"), float("nan"), 0
    cost = ROUND_TRIP_BPS / 1e4
    top_mean = float(j["top"].mean())
    ls = float(j["ls"].mean())
    return (-top_mean - cost) * 1e4, (ls - 2 * cost) * 1e4, int(j.height)


def main() -> int:
    split_ms = _date_str_to_ms("2025-06-01")
    out: dict = {}
    for venue, root in VENUES.items():
        if not root.exists():
            print(f"SKIP {venue}")
            continue
        print(f"[{venue}] build panel ...", flush=True)
        feat = _build_panel(root)
        if feat.is_empty():
            print(f"[{venue}] EMPTY")
            continue
        first = _first_listing_ms(root)
        feat = feat.join(first, on="symbol", how="left").with_columns(
            ((pl.col("ts_ms") - pl.col("first_ms")) / MS_PER_DAY).alias("age_days")
        )
        feat = _attach_rmom(feat, root)
        aged = feat.filter(pl.col("age_days") >= AGE_MIN_DAYS)
        vres: dict = {}

        # --- A. ENTRY LATENCY (rmom_lo q=.5, and age300+rmom_lo) ---
        print(f"[{venue}] A. ENTRY-LATENCY  (short_only_net bps; the +1h gate is mandatory)", flush=True)
        lat_variants = {"rmom_lo": _assign_decile(_low_rmom(feat, 0.5)),
                        "age300_rmom_lo": _assign_decile(_low_rmom(aged, 0.5))}
        vres["latency"] = {}
        for name, dec in lat_variants.items():
            vres["latency"][name] = {}
            for h in HOLDS:
                row = {}
                line = f"[{venue}]   {name:16s} h{h:<3d} "
                for d in DELAYS:
                    j = _perts(dec, f"f_d{d}_h{h}")
                    s_all, l_all, n = _net(j)
                    s_e, l_e, _ = _net(j, hi=split_ms)
                    s_r, l_r, _ = _net(j, lo=split_ms)
                    row[f"d{d}"] = {"all_short": round(s_all, 0), "all_ls": round(l_all, 0),
                                    "early_short": round(s_e, 0), "early_ls": round(l_e, 0),
                                    "recent_short": round(s_r, 0), "recent_ls": round(l_r, 0), "n_ts": n}
                    line += f" | +{d}h E_s={s_e:+.0f} E_ls={l_e:+.0f} R_s={s_r:+.0f}"
                vres["latency"][name][f"h{h}"] = row
                print(line, flush=True)

        # --- B. THRESHOLD MONOTONICITY (rmom_lo at q in .5/.33/.25; all ages) ---
        print(f"[{venue}] B. THRESHOLD  (early short/LS @ h24 & h168, delay 0)", flush=True)
        vres["threshold"] = {}
        for q in RMOM_QS:
            dec = _assign_decile(_low_rmom(feat, q))
            entry = {}
            line = f"[{venue}]   q={q:.2f} "
            for h in (24, 168):
                j = _perts(dec, f"f_d0_h{h}")
                s_e, l_e, n = _net(j, hi=split_ms)
                s_r, l_r, _ = _net(j, lo=split_ms)
                entry[f"h{h}"] = {"early_short": round(s_e, 0), "early_ls": round(l_e, 0),
                                  "recent_short": round(s_r, 0), "recent_ls": round(l_r, 0), "n_ts_early": n}
                line += f" | h{h} E_s={s_e:+.0f} E_ls={l_e:+.0f} R_s={s_r:+.0f} R_ls={l_r:+.0f}"
            vres["threshold"][f"q{q:.2f}"] = entry
            print(line, flush=True)

        # --- C. FINER TIME BUCKETS (rmom_lo q=.5, delay 0) ---
        print(f"[{venue}] C. TIME BUCKETS  (rmom_lo q=.5; short/LS per period — is early uniform?)", flush=True)
        dec = _assign_decile(_low_rmom(feat, 0.5))
        vres["buckets"] = {}
        for h in (24, 168):
            j = _perts(dec, f"f_d0_h{h}")
            line = f"[{venue}]   h{h:<3d} "
            vres["buckets"][f"h{h}"] = {}
            for bname, b0, b1 in BUCKETS:
                s, l_, n = _net(j, lo=_date_str_to_ms(b0), hi=_date_str_to_ms(b1))
                vres["buckets"][f"h{h}"][bname] = {"short": round(s, 0), "ls": round(l_, 0), "n_ts": n}
                line += f" | {bname} s={s:+.0f} ls={l_:+.0f}"
            print(line, flush=True)

        out[venue] = vres
        print(flush=True)

    out_path = SHARED / "p0b_continuous_rmom_stress_2026-05-31.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"DONE -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
