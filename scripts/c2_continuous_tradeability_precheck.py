"""C2 pre-check — is the continuous IC signal TRADEABLE after honest costs? Decile-spread
+ market-neutral long/short test on the C1 continuous feature panel, BEFORE building the
full C0 engine (~5-7d).

C1 showed the rolling features carry real cross-venue IC (~-0.13 @168h, strengthening with
horizon). IC != tradeable (Round-1 Phase-6 daily-combined failed on cost + alt-beta). This
tests whether the signal survives cost, especially MARKET-NEUTRAL (long-short), which
neutralizes the alt-market beta that sank the short-only daily strategy.

composite = mean XS-rank of the 5 rolling features (high = strong SHORT, negative IC).
Per timestamp, decile by composite. Per horizon H, per venue, pooled over timestamps:
  D10 mean fwd ret (strongest short; expect NEGATIVE = good short)
  D1  mean fwd ret (weakest short / candidate long)
  L/S spread per hold = mean_fwd(D1) - mean_fwd(D10)   [long D1, short D10; beta-neutral]
  short-only per hold = -mean_fwd(D10)
Compared to a 15 bps taker round-trip (per leg per hold). Tradeable if the cost-adjusted
L/S spread is clearly positive on BOTH venues. One venue at a time; read-only.

Dispatch: $env:POLARS_MAX_THREADS=8; .venv\\Scripts\\python.exe -u scripts\\c2_continuous_tradeability_precheck.py
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
    FEATURES,
    HORIZONS_H,
    SHARED,
    VENUES,
    _continuous_features,
    _rank_ic,
)

ROUND_TRIP_BPS = 15.0  # honest 100%-taker round-trip per leg per hold


def main() -> int:
    print(f"C2 continuous-tradeability pre-check  horizons(h)={HORIZONS_H}  cost={ROUND_TRIP_BPS}bps/leg/hold\n", flush=True)
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
        # composite = mean of per-ts XS ranks (normalized 0..1) of the 5 features; high = short
        present = [f for f in FEATURES if f in feat.columns]
        feat = feat.with_columns([
            ((pl.col(f).rank().over("ts_ms") - 1) / (pl.len().over("ts_ms") - 1)).alias(f"_n_{f}")
            for f in present
        ])
        feat = feat.with_columns(pl.mean_horizontal([pl.col(f"_n_{f}") for f in present]).alias("composite"))
        # decile per ts (0..9); need enough names per ts
        feat = feat.with_columns(
            (((pl.col("composite").rank().over("ts_ms") - 1) * 10) // pl.len().over("ts_ms")).clip(0, 9).alias("decile")
        )
        venue_res: dict = {}
        # composite IC (per-ts Spearman of composite vs fwd, averaged) — must agree in sign
        # with the decile spread; if not, the signal is non-monotonic (extreme reversal).
        comp_ic = {f"{h}h": round(_rank_ic(feat, "composite", f"fwd_{h}h")[0], 4) for h in HORIZONS_H}
        venue_res["_composite_ic"] = comp_ic
        print(f"[{venue}] composite IC: " + "  ".join(f"{h}h={comp_ic[f'{h}h']:+.4f}" for h in HORIZONS_H), flush=True)
        for h in HORIZONS_H:
            fwd = f"fwd_{h}h"
            sub = feat.select("ts_ms", "decile", fwd).drop_nulls()
            if sub.is_empty():
                continue
            # WITHIN-TIMESTAMP L/S (market-neutral), averaged over ts. Pooling decile means
            # across ts confounds the cross-sectional signal with regime (the vol composite is
            # high-prevalence in high-return bull periods -> Simpson's paradox), so the L/S is
            # computed inside each rebalance, then averaged — matching the within-ts IC.
            dt = sub.group_by(["ts_ms", "decile"]).agg(pl.col(fwd).mean().alias("dm"))
            # full within-ts-averaged decile profile (shape: monotonic vs extreme-reversal)
            prof = {int(r["decile"]): round(float(r["p"]), 5) for r in
                    dt.group_by("decile").agg(pl.col("dm").mean().alias("p")).sort("decile").iter_rows(named=True)}
            top = dt.filter(pl.col("decile") == 9).select("ts_ms", pl.col("dm").alias("top"))
            bot = dt.filter(pl.col("decile") == 0).select("ts_ms", pl.col("dm").alias("bot"))
            j = top.join(bot, on="ts_ms", how="inner").with_columns((pl.col("bot") - pl.col("top")).alias("ls"))
            if j.is_empty():
                continue
            ls = float(j["ls"].mean())          # mean per-hold within-ts L/S (long bottom, short top)
            top_mean = float(j["top"].mean())
            bot_mean = float(j["bot"].mean())
            short_only = -top_mean              # per-hold short-only top decile (carries beta)
            cost = ROUND_TRIP_BPS / 1e4
            venue_res[f"{h}h"] = {
                "top_decile_mean_fwd": round(top_mean, 5), "bot_decile_mean_fwd": round(bot_mean, 5),
                "ls_spread_per_hold": round(ls, 5), "ls_spread_bps": round(ls * 1e4, 1),
                "ls_net_per_hold": round(ls - 2 * cost, 5),       # L/S trades 2 legs
                "short_only_net_per_hold": round(short_only - cost, 5),
                "decile_profile": prof, "n_ts": int(j.height),
            }
            print(f"[{venue}] {h:3d}h  L/S/hold={ls*1e4:+.0f}bps  net={ (ls-2*cost)*1e4:+.0f}bps  "
                  f"decile_fwd[D0..D9]=" + " ".join(f"{prof.get(d, float('nan'))*1e4:+.0f}" for d in range(10)) + " bps", flush=True)
        out[venue] = venue_res
        print(flush=True)
    (SHARED / "c2_continuous_tradeability_precheck_2026-05-29.json").write_text(json.dumps(out, indent=2))
    print("DONE -> c2_continuous_tradeability_precheck_2026-05-29.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
