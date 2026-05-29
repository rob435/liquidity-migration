"""R9 pre-check — does the composite IC signal sort EVENT trades by short profitability?

Cheap, decisive diagnostic BEFORE building the full R9 IC-augmented engine integration.
The R1 hardened re-baseline showed the event-driven strategy is negative-return on binance
under honest costs; the only R9 lever that can flip a negative return positive is
SELECTIVITY (filter events to the profitable subset). R2 found the 5 IC features collapse
to ~1 factor that "selects largely the same high-vol/extended alt basket" as the event
trigger — so IC selectivity may add little. This tests it directly:

For each venue, build the 5-IC-feature panel (full-PIT), cross-sectionally rank each
feature per day, average -> composite_ic (all 5 have NEGATIVE IC, so HIGH composite =
strong SHORT signal). Join composite_ic at the trade's SIGNAL day (entry_signal_ts_ms
floored to the 00:00-UTC grid) to the hardened re-baseline event trades. Report, per
venue x cell: Spearman corr(composite_ic, gross_trade_return) [+ = IC sorts shorts by
profit] and mean gross_trade_return by composite_ic quintile.

If high-composite event trades are NOT more profitable shorts (esp. binance / R1_drop_all_4)
=> IC selectivity cannot rescue the negative event-driven return => R9-IC futile, default
do-nothing. If they clearly are => build the full R9 IC-augmented stack.

Read-only on the working roots. Dispatch (5950X, Windows):
    $env:POLARS_MAX_THREADS=8; .venv\\Scripts\\python.exe -u scripts\\r9_ic_selectivity_precheck.py
"""
from __future__ import annotations

import json
import sys
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

from liquidity_migration.signal_harness import _xs_rank, build_feature_panel  # noqa: E402

SHARED = Path.home() / "SHARED_DATA"
REBASE_TAG = "r1_rebaseline_hardened_2026-05-29"
START_DATE, END_DATE = "2023-04-01", "2026-05-28"
MS_PER_DAY = 86_400_000
FEATURES = ["vol_of_vol_30d", "realized_vol_7d", "dist_from_30d_low", "xs_rank_ret_7d", "xs_rank_ret_3d"]
VENUES = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
CELLS = ["00_baseline", "R1_drop_all_4"]


def _composite_ic(root: Path) -> pl.DataFrame:
    """Per (symbol, day-ts_ms) composite IC = mean of the 5 features' XS ranks (high=short)."""
    panel = build_feature_panel(root, start=START_DATE, end=END_DATE, feature_specs=",".join(FEATURES), forward_horizons=(1,))
    if panel.is_empty():
        return pl.DataFrame()
    rank_cols = []
    for f in FEATURES:
        if f in panel.columns:
            panel = _xs_rank(panel, f, out_col=f"_r_{f}")
            rank_cols.append(f"_r_{f}")
    if not rank_cols:
        return pl.DataFrame()
    return panel.select(
        "symbol", "ts_ms",
        pl.mean_horizontal([pl.col(c) for c in rank_cols]).alias("composite_ic"),
    )


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    if x.size < 3:
        return 0.0
    rx = pl.Series(x).rank().to_numpy()
    ry = pl.Series(y).rank().to_numpy()
    if rx.std() == 0 or ry.std() == 0:
        return 0.0
    return float(np.corrcoef(rx, ry)[0, 1])


def main() -> int:
    print(f"R9 IC-selectivity pre-check  window={START_DATE}->{END_DATE}  features={FEATURES}\n", flush=True)
    out: dict = {}
    for venue, root in VENUES.items():
        if not root.exists():
            print(f"SKIP {venue}: {root} not found")
            continue
        print(f"[{venue}] build composite_ic panel ...", flush=True)
        comp = _composite_ic(root)
        if comp.is_empty():
            print(f"[{venue}] EMPTY composite panel -- skip")
            continue
        for cell in CELLS:
            f = root / "reports" / REBASE_TAG / cell / "volume_event_best_trades.csv"
            if not f.exists():
                print(f"[{venue}/{cell}] MISSING {f}")
                continue
            t = pl.read_csv(f)
            sig_col = "entry_signal_ts_ms" if "entry_signal_ts_ms" in t.columns else "entry_ts_ms"
            t = t.select(
                "symbol",
                ((pl.col(sig_col) // MS_PER_DAY) * MS_PER_DAY).alias("ts_ms"),
                pl.col("gross_trade_return").alias("gtr"),
            ).join(comp, on=["symbol", "ts_ms"], how="left")
            j = t.drop_nulls(["composite_ic", "gtr"])
            n, matched = t.height, j.height
            if matched < 5:
                print(f"[{venue}/{cell}] only {matched}/{n} trades matched composite_ic -- skip")
                continue
            ic = j["composite_ic"].to_numpy()
            gtr = j["gtr"].to_numpy()
            rho = _spearman(ic, gtr)
            # quintile means of gross_trade_return by composite_ic
            jq = j.with_columns((pl.col("composite_ic").rank() * 5 // (matched + 1)).alias("q"))
            qmeans = {int(r["q"]): round(float(r["gtr_mean"]), 4)
                      for r in jq.group_by("q").agg(pl.col("gtr").mean().alias("gtr_mean")).sort("q").iter_rows(named=True)}
            res = {
                "n_trades": n, "matched": matched,
                "spearman_ic_vs_gross": round(rho, 4),
                "mean_gtr_top_quintile": qmeans.get(4), "mean_gtr_bottom_quintile": qmeans.get(0),
                "gtr_mean_by_ic_quintile": qmeans,
                "ic_std_among_trades": round(float(ic.std()), 4),
                "overall_mean_gtr": round(float(gtr.mean()), 4),
            }
            out[f"{venue}/{cell}"] = res
            print(f"[{venue}/{cell}] matched={matched}/{n}  spearman(ic,gross)={rho:+.4f}  "
                  f"gtr top-Q={res['mean_gtr_top_quintile']} bot-Q={res['mean_gtr_bottom_quintile']}  "
                  f"ic_std={res['ic_std_among_trades']}  mean_gtr={res['overall_mean_gtr']}", flush=True)
    (SHARED / "r9_ic_selectivity_precheck_2026-05-29.json").write_text(json.dumps(out, indent=2))
    print("\nDONE -> r9_ic_selectivity_precheck_2026-05-29.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
