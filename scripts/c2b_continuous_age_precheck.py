"""C2b pre-check — does the AGE GATE (E2's winner) make the CONTINUOUS signal tradeable?

c2 found the continuous decile L/S did not clearly survive 15 bps cost. E2 then showed the
single biggest SELECTION lever is dropping young symbols (fresh listings squeeze shorts). This
re-runs the c2 tradeability test with the **age gate** applied to the continuous panel, to
decide whether the multi-day C0 engine is worth building (see
docs/preregistration/exploratory/c0-continuous-engine-scope-2026-05-30.md).

Variants compared per venue (within-timestamp, averaged over ts — same method as c2):
  baseline   : full continuous panel (all symbols)            [reproduces c2]
  age>=300   : symbols with TRUE age >= 300 d at the timestamp [E2 gate]
Read-outs per horizon: top-decile (D9) mean fwd ret (the short), short-only net per hold
(-D9 - 15bps), and within-ts L/S net (D0-D9 - 2x15bps). Tradeable if short-only or L/S net
is clearly positive on BOTH venues. EXPLORATORY (look-ahead forward returns; not a backtest).

True age uses each symbol's first-ever kline ts (min over full history, separate light read),
so it is PIT-correct (not the window-start proxy). One venue at a time; read-only.

Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/c2b_continuous_age_precheck.py
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

from c1_continuous_ic_precheck import (  # noqa: E402
    FEATURES, HORIZONS_H, SHARED, VENUES, END_DATE, MS_PER_DAY,
    _continuous_features,
)
from liquidity_migration.signal_harness import (  # noqa: E402
    _autodetect_dataset_names, _date_str_to_ms, _read_window,
)

ROUND_TRIP_BPS = 15.0
AGE_MIN_DAYS = 300
LISTING_SCAN_START = "2018-01-01"  # before any USDT-perp listing on either venue


def _first_listing_ms(root: Path) -> pl.DataFrame:
    """symbol -> first-ever kline ts (true listing), from a light (ts_ms, symbol) full scan."""
    kname = _autodetect_dataset_names(root)["klines_dataset"]
    k = _read_window(root, kname, start_ms=_date_str_to_ms(LISTING_SCAN_START),
                     end_ms=_date_str_to_ms(END_DATE), columns=["ts_ms", "symbol"])
    if k.is_empty():
        return pl.DataFrame({"symbol": [], "first_ms": []})
    return k.group_by("symbol").agg(pl.col("ts_ms").min().alias("first_ms"))


def _decile_short(feat: pl.DataFrame, ts_lo: int | None = None, ts_hi: int | None = None) -> dict:
    """Within-ts decile of the composite; per-horizon short-only + L/S net vs cost.
    Optional [ts_lo, ts_hi) window for sub-period (recent/early) splits."""
    if ts_lo is not None:
        feat = feat.filter(pl.col("ts_ms") >= ts_lo)
    if ts_hi is not None:
        feat = feat.filter(pl.col("ts_ms") < ts_hi)
    if feat.is_empty():
        return {}
    present = [f for f in FEATURES if f in feat.columns]
    feat = feat.with_columns([
        ((pl.col(f).rank().over("ts_ms") - 1) / (pl.len().over("ts_ms") - 1)).alias(f"_n_{f}")
        for f in present
    ])
    feat = feat.with_columns(pl.mean_horizontal([pl.col(f"_n_{f}") for f in present]).alias("composite"))
    feat = feat.with_columns(
        (((pl.col("composite").rank().over("ts_ms") - 1) * 10) // pl.len().over("ts_ms")).clip(0, 9).alias("decile")
    )
    cost = ROUND_TRIP_BPS / 1e4
    res = {}
    for h in HORIZONS_H:
        fwd = f"fwd_{h}h"
        sub = feat.select("ts_ms", "decile", fwd).drop_nulls()
        if sub.is_empty():
            continue
        dt = sub.group_by(["ts_ms", "decile"]).agg(pl.col(fwd).mean().alias("dm"))
        top = dt.filter(pl.col("decile") == 9).select("ts_ms", pl.col("dm").alias("top"))
        bot = dt.filter(pl.col("decile") == 0).select("ts_ms", pl.col("dm").alias("bot"))
        j = top.join(bot, on="ts_ms", how="inner").with_columns((pl.col("bot") - pl.col("top")).alias("ls"))
        if j.is_empty():
            continue
        ls = float(j["ls"].mean()); top_mean = float(j["top"].mean())
        res[f"{h}h"] = {
            "top_decile_mean_fwd_bps": round(top_mean * 1e4, 1),
            "short_only_net_bps": round((-top_mean - cost) * 1e4, 1),
            "ls_net_bps": round((ls - 2 * cost) * 1e4, 1),
            "n_ts": int(j.height),
            "avg_names_per_ts": int(feat.group_by("ts_ms").len()["len"].mean()),
        }
    return res


def main() -> int:
    print(f"C2b continuous AGE-GATE tradeability  age>={AGE_MIN_DAYS}d  cost={ROUND_TRIP_BPS}bps/leg/hold\n", flush=True)
    out: dict = {}
    for venue, root in VENUES.items():
        if not root.exists():
            print(f"SKIP {venue}: {root} not found"); continue
        print(f"[{venue}] first-listing scan ...", flush=True)
        first = _first_listing_ms(root)
        print(f"[{venue}] build continuous panel ...", flush=True)
        feat = _continuous_features(root)
        if feat.is_empty():
            print(f"[{venue}] EMPTY -- skip"); continue
        feat = feat.join(first, on="symbol", how="left").with_columns(
            ((pl.col("ts_ms") - pl.col("first_ms")) / MS_PER_DAY).alias("age_days")
        )
        venue_res: dict = {}
        # age-sensitivity: 0 (baseline) / 200 / 300 / 400
        for age in (0, 200, 300, 400):
            aged = feat if age == 0 else feat.filter(pl.col("age_days") >= age)
            tag = "baseline" if age == 0 else f"age_ge_{age}"
            venue_res[tag] = _decile_short(aged)
            for h in (72, 168):
                r = venue_res[tag].get(f"{h}h")
                if r:
                    print(f"[{venue}] {tag:11s} {h:3d}h  D9_fwd={r['top_decile_mean_fwd_bps']:+.0f}bps  "
                          f"short_net={r['short_only_net_bps']:+.0f}bps  LS_net={r['ls_net_bps']:+.0f}bps  "
                          f"names/ts={r['avg_names_per_ts']}", flush=True)
        # recent (>=2025-06) vs early (<2025-06) split at age>=300/400, 168h — confound check
        cut = _date_str_to_ms("2025-06-01")
        for age in (300, 400):
            aged = feat.filter(pl.col("age_days") >= age)
            early = _decile_short(aged, ts_hi=cut).get("168h", {})
            recent = _decile_short(aged, ts_lo=cut).get("168h", {})
            venue_res[f"age_ge_{age}_split168"] = {"early": early, "recent": recent}
            print(f"[{venue}] age>={age} 168h SPLIT  early short_net={early.get('short_only_net_bps','?')}bps  "
                  f"recent short_net={recent.get('short_only_net_bps','?')}bps  "
                  f"(recent LS_net={recent.get('ls_net_bps','?')}bps)", flush=True)
        out[venue] = venue_res
        print(flush=True)
    (SHARED / "c2b_continuous_age_precheck_2026-05-30.json").write_text(json.dumps(out, indent=2))
    print("DONE -> c2b_continuous_age_precheck_2026-05-30.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
