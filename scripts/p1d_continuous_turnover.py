"""Phase 1 / Avenue D (continuous-fade) — turnover, persistence, and does CONTINUOUS beat DAILY?

Phase 0 found an all-weather 24h continuous rmom short at the per-ts characterization level. The gap to a
TRADEABLE book is turnover/overlap: the per-ts char counts a name every hour it sits in D9, but a real book
enters once and holds. This run closes that gap, read-only on the same panel:

  1. SPELL-ENTRY edge (the tradeable proxy): count a name's 24h-forward short return only at its ENTRY into
     D9 (gap-aware spell start), with a 24h cooldown (no re-entry while a 24h hold is open). mean(-fwd-15bps),
     full / early / recent + 2023/2024/2025H1/recent buckets, both venues. If this stays all-weather positive,
     the edge survives de-overlapping into actual trades (and cost is far lower than the per-ts char implies,
     since trades = spells, not name-hours).
  2. PERSISTENCE / TURNOVER: mean D9 dwell (hours), % single-hour spells, spells/yr, avg book size. Low churn
     (dwell ~ many hours) = tradeable; flicker (dwell ~1-2h) = the per-ts edge is a churn artifact.
  3. CONTINUOUS vs DAILY: the same D9 short edge restricted to the 01:00 UTC snapshot (the daily entry hour)
     = a once-daily rmom-decile short. If ~as strong as all-hours, continuous adds only breadth (more entries),
     not per-trade edge — a key honest result for whether "continuous" is worth the engine build.

Also caches the deciled panel to <root>/_p1_continuous_panel.parquet for fast reuse. EXPLORATORY
characterization (per-spell means, simplified concurrency); NOT a backtest, NOT promotion evidence.

Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/p1d_continuous_turnover.py
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
MS_PER_HOUR = 3_600_000
HOLD_H = 24
COOLDOWN_MS = 24 * MS_PER_HOUR
FEATURES = ["rv_168h", "vov", "dist_low", "xsret7", "xsret3"]
ROUND_TRIP_BPS = 15.0
VENUES = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
BUCKETS = [("2023", "2023-04-01", "2024-01-01"), ("2024", "2024-01-01", "2025-01-01"),
           ("2025H1", "2025-01-01", "2025-06-01"), ("recent", "2025-06-01", "2026-05-29")]
SPLIT_MS = _date_str_to_ms("2025-06-01")


def _deciled_panel(root: Path) -> pl.DataFrame:
    """Build (or load cache): symbol, ts_ms, decile, fwd for the rmom_lo continuous universe."""
    cache = root / "_p1_continuous_panel.parquet"
    if cache.exists():
        return pl.read_parquet(cache)
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
    k = k.with_columns((pl.col("close").shift(-HOLD_H).over("symbol") / pl.col("close") - 1.0).alias("fwd"))
    k = k.with_columns(
        pl.col("ret168").rank().over("ts_ms").alias("xsret7"),
        pl.col("ret72").rank().over("ts_ms").alias("xsret3"),
    )
    k = k.filter(pl.col("ts_ms") >= start_ms)
    # rmom_lo universe (causal day-floor join, within-ts low half)
    rmom = pl.read_parquet(root / "residual_momentum.parquet").rename({"ts_ms": "day_ts"})
    k = k.with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day_ts"))
    k = k.join(rmom, on=["symbol", "day_ts"], how="left").filter(pl.col("residual_momentum").is_not_null())
    k = k.with_columns(
        ((pl.col("residual_momentum").rank().over("ts_ms") - 1) / (pl.len().over("ts_ms") - 1)).alias("_rr")
    ).filter(pl.col("_rr") <= 0.5)
    present = [f for f in FEATURES if f in k.columns]
    k = k.with_columns([
        ((pl.col(f).rank().over("ts_ms") - 1) / (pl.len().over("ts_ms") - 1)).alias(f"_n_{f}") for f in present
    ])
    k = k.with_columns(pl.mean_horizontal([pl.col(f"_n_{f}") for f in present]).alias("composite"))
    k = k.with_columns(
        (((pl.col("composite").rank().over("ts_ms") - 1) * 10) // pl.len().over("ts_ms")).clip(0, 9).alias("decile")
    )
    panel = k.select("symbol", "ts_ms", "decile", "fwd")
    panel.write_parquet(cache)
    return panel


def _short_net(frame: pl.DataFrame, lo=None, hi=None) -> tuple[float, int]:
    if lo is not None:
        frame = frame.filter(pl.col("ts_ms") >= lo)
    if hi is not None:
        frame = frame.filter(pl.col("ts_ms") < hi)
    f = frame.select("fwd").drop_nulls()
    if f.is_empty():
        return float("nan"), 0
    return (-float(f["fwd"].mean()) - ROUND_TRIP_BPS / 1e4) * 1e4, int(f.height)


def _splits(frame: pl.DataFrame, label: str, venue: str) -> dict:
    res = {}
    for name, lo, hi in [("full", None, None), ("early", None, SPLIT_MS), ("recent", SPLIT_MS, None)]:
        s, n = _short_net(frame, lo, hi)
        res[name] = {"short_bps": round(s, 0), "n": n}
    for b, a0, a1 in BUCKETS:
        s, n = _short_net(frame, _date_str_to_ms(a0), _date_str_to_ms(a1))
        res[b] = {"short_bps": round(s, 0), "n": n}
    e, r = res["early"], res["recent"]
    print(f"[{venue}] {label:16s} FULL={res['full']['short_bps']:+5.0f}(n={res['full']['n']})  "
          f"E={e['short_bps']:+5.0f} R={r['short_bps']:+5.0f}  | "
          + " ".join(f"{b}={res[b]['short_bps']:+5.0f}" for b, *_ in BUCKETS), flush=True)
    return res


def main() -> int:
    out: dict = {}
    for venue, root in VENUES.items():
        if not root.exists():
            print(f"SKIP {venue}")
            continue
        print(f"[{venue}] build/load deciled panel ...", flush=True)
        panel = _deciled_panel(root)
        if panel.is_empty():
            print(f"[{venue}] EMPTY")
            continue
        d9 = panel.filter(pl.col("decile") == 9).sort(["symbol", "ts_ms"])
        # gap-aware spell start: first D9 row of symbol, or a >1h gap from the prev D9 row
        d9 = d9.with_columns(
            ((pl.col("ts_ms") - pl.col("ts_ms").shift(1).over("symbol")) > MS_PER_HOUR).fill_null(True).alias("spell_start")
        )
        d9 = d9.with_columns(pl.col("spell_start").cum_sum().over("symbol").alias("spell_id"))
        # persistence: dwell = D9-rows per spell
        dwell = d9.group_by(["symbol", "spell_id"]).len().rename({"len": "dwell_h"})
        n_spells = dwell.height
        mean_dwell = float(dwell["dwell_h"].mean())
        frac_1h = float((dwell["dwell_h"] == 1).mean())
        avg_book = float(panel.filter(pl.col("decile") == 9).group_by("ts_ms").len()["len"].mean())
        years = (d9["ts_ms"].max() - d9["ts_ms"].min()) / (365.25 * MS_PER_DAY)

        # 24h-cooldown debounce over spell-start rows (a name re-enters at most once / 24h)
        starts = d9.filter(pl.col("spell_start")).select("symbol", "ts_ms", "fwd").sort(["symbol", "ts_ms"])
        kept_idx = []
        syms = starts["symbol"].to_list()
        tss = starts["ts_ms"].to_list()
        last: dict[str, int] = {}
        for i, (sy, ts) in enumerate(zip(syms, tss)):
            if sy not in last or ts - last[sy] >= COOLDOWN_MS:
                kept_idx.append(i)
                last[sy] = ts
        entries = starts[kept_idx]
        n_entries = entries.height
        trades_yr = n_entries / years if years else float("nan")

        print(f"[{venue}] persistence: mean_dwell={mean_dwell:.1f}h  frac_1h_spells={frac_1h:.1%}  "
              f"spells={n_spells}  cooldown_entries={n_entries}  avg_book={avg_book:.0f}  trades/yr={trades_yr:.0f}",
              flush=True)
        print(f"[{venue}] short_net_bps (-fwd24 - 15bps):", flush=True)
        per_ts = _splits(panel.filter(pl.col("decile") == 9), "per_ts_char", venue)         # every D9 name-hour
        spell = _splits(entries, "spell_entry_cd24", venue)                                 # tradeable proxy
        daily = _splits(panel.filter((pl.col("decile") == 9) & (pl.col("ts_ms") % MS_PER_DAY == MS_PER_HOUR)),
                        "daily_0100_snap", venue)                                           # continuous vs daily
        out[venue] = {
            "persistence": {"mean_dwell_h": round(mean_dwell, 2), "frac_1h_spells": round(frac_1h, 3),
                            "n_spells": n_spells, "cooldown_entries": n_entries, "avg_book": round(avg_book, 1),
                            "trades_per_yr": round(trades_yr, 0)},
            "per_ts_char": per_ts, "spell_entry_cd24": spell, "daily_0100_snap": daily,
        }
        print(flush=True)

    out_path = SHARED / "p1d_continuous_turnover_2026-05-31.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"DONE -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
