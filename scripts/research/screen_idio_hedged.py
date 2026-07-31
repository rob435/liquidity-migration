#!/usr/bin/env python3
"""Mode (b): does actually holding the hedge pay for itself?

The idio-chart screen (``scripts/research/screen_idio_charts.py``) reports every signal
against two targets: the raw executable return, and the residual return. The
residual number is not tradable on its own -- earning it requires holding the
factor hedge, whose cost that screen does not model. This script closes that
gap by building the hedged book explicitly and charging the hedge.

WHAT IS ACTUALLY HEDGEABLE. COMMON4 is (btc_beta, xs_rank_ret_30d,
realized_vol_rank, liquidity_rank). Three of those are cross-sectional ranks
with no tradable instrument -- there is no contract that pays "liquidity rank".
Only ``btc_beta`` has one, so mode (b) here is a **BTC-beta-neutral** decile
book, not a fully factor-neutral one; the residual is not fully harvestable.

THE BOOK. Each day, the equal-weight decile long/short from the same signal, plus
a BTC leg sized to cancel the book's net beta:

    net_beta[t]   = mean(beta | long) - mean(beta | short)
    hedged_ret[t] = spread_ret[t] - net_beta[t] * btc_ret[t]

The hedge is rebalanced daily and charged on the change in its own notional,
``|net_beta[t] - net_beta[t-1]|``, at the measured round trip. The spread leg is
charged on its measured turnover exactly as in the unhedged screen, so the two
are compared on one cost basis.

DECLARED KILL CONDITION (``docs/archive/2026-07-30-idio-charts.md``): if the
hedged book's net Sharpe does not exceed the unhedged book's at the same cost
basis, the idio-chart programme is closed for this repository.

Dispatch:
    .venv/bin/python -u scripts/research/screen_idio_hedged.py \\
        --panel data/idio_panel/bybit/panel_common4.parquet
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration.research.panels.cross_section import (  # noqa: E402
    MEASURED_ROUND_TRIP_BP,
    summary,
    top_by,
)
from liquidity_migration.research.panels.idio_features import CHART_FEATURES  # noqa: E402
from scripts.research.screen_idio_charts import (  # noqa: E402
    ALL_SOURCES,
    DAYS_PER_YEAR,
    bonferroni_t,
    realized_turnover,
)

BTC = "BTCUSDT"


def _btc_returns(panel: pl.DataFrame) -> pl.DataFrame:
    """BTC's own forward return per decision day -- the hedge instrument's P&L."""
    return (
        panel.filter(pl.col("symbol") == BTC)
        .select("ts_ms", pl.col("fwd_ret_1d").alias("_btc_ret"))
        .unique(subset="ts_ms")
        .drop_nulls()
    )


def hedged_book(
    panel: pl.DataFrame,
    *,
    signal: str,
    btc: pl.DataFrame,
    cut: float,
    min_names: int,
) -> pl.DataFrame | None:
    """Per-day spread return, net beta, and the beta-hedged return, in bp."""
    need = ["ts_ms", "symbol", signal, "fwd_ret_1d", "btc_beta"]
    frame = panel.select(need).drop_nulls()
    frame = frame.filter(pl.len().over("ts_ms") >= min_names)
    if frame.is_empty():
        return None
    legs = (
        frame.with_columns(
            ((pl.col(signal).rank("ordinal").over("ts_ms") - 0.5) / pl.len().over("ts_ms")).alias("_p")
        )
        .filter((pl.col("_p") <= cut) | (pl.col("_p") >= 1.0 - cut))
        .with_columns((pl.col("_p") >= 1.0 - cut).alias("_is_long"))
    )
    if legs.is_empty():
        return None
    per_day = (
        legs.group_by("ts_ms")
        .agg(
            pl.col("fwd_ret_1d").filter(pl.col("_is_long")).mean().alias("_r_long"),
            pl.col("fwd_ret_1d").filter(~pl.col("_is_long")).mean().alias("_r_short"),
            pl.col("btc_beta").filter(pl.col("_is_long")).mean().alias("_b_long"),
            pl.col("btc_beta").filter(~pl.col("_is_long")).mean().alias("_b_short"),
        )
        .drop_nulls()
        .join(btc, on="ts_ms", how="inner")
        .sort("ts_ms")
    )
    if per_day.is_empty():
        return None
    return per_day.with_columns(
        ((pl.col("_r_long") - pl.col("_r_short")) * 1e4).alias("spread_bp"),
        (pl.col("_b_long") - pl.col("_b_short")).alias("net_beta"),
    ).with_columns(
        (pl.col("spread_bp") - pl.col("net_beta") * pl.col("_btc_ret") * 1e4).alias("hedged_bp"),
        pl.col("net_beta").diff().abs().fill_null(0.0).alias("hedge_turn"),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", required=True, type=Path)
    ap.add_argument("--universe", type=int, default=150)
    ap.add_argument("--cut", type=float, default=0.10)
    ap.add_argument("--min-names", type=int, default=20)
    ap.add_argument("--coverage-floor", type=float, default=0.90)
    args = ap.parse_args(argv)

    panel = pl.read_parquet(args.panel)
    if "btc_beta" not in panel.columns:
        raise SystemExit("panel lacks btc_beta; rebuild with the current scripts/data/build_idio_panel.py")
    btc = _btc_returns(panel)
    print(f"panel {args.panel}")
    print(f"  rows={panel.height} days={panel['ts_ms'].n_unique()}  BTC days={btc.height}\n")
    if args.universe > 0:
        panel = top_by(panel, "turnover_quote", args.universe, period="ts_ms")

    idio_mask = pl.col("coverage").fill_null(0.0) >= args.coverage_floor
    full_cost = 2.0 * MEASURED_ROUND_TRIP_BP

    print("MODE (b): BTC-beta-hedged decile book vs the same book unhedged.")
    print("Only btc_beta is hedgeable; the three rank factors have no instrument.")
    print("Both legs charged at the measured round trip on their own turnover.\n")
    print(f"  {'feature':<20s} {'source':<8s} {'|beta|':>7s} {'hTurn':>6s} "
          f"{'unhedged':>9s} {'hedged':>8s} {'unh_shrp':>9s} {'hed_shrp':>9s} {'delta':>7s}")

    rows: list[dict] = []
    for source in ALL_SOURCES:
        for feature in CHART_FEATURES:
            signal = f"{source}_{feature}"
            if signal not in panel.columns:
                continue
            scoped = panel.filter(idio_mask) if source in ("idio", "iz") else panel
            book = hedged_book(
                scoped, signal=signal, btc=btc, cut=args.cut, min_names=args.min_names
            )
            if book is None or book.height < 50:
                continue
            spread_turn = realized_turnover(
                scoped, signal=signal, target="fwd_ret_1d", cut=args.cut, min_names=args.min_names
            )
            if spread_turn != spread_turn:
                continue
            spread_cost = spread_turn * full_cost
            # The hedge leg turns over its own notional; charge one round trip
            # on the change in hedge size.
            hedge_cost = float(book["hedge_turn"].mean()) * MEASURED_ROUND_TRIP_BP

            # Best direction, consistent with the unhedged screen.
            sgn = 1.0 if float(book["spread_bp"].mean()) >= 0 else -1.0
            unh = np.asarray(book["spread_bp"].to_list()) * sgn
            hed = np.asarray(book["hedged_bp"].to_list()) * sgn
            unh_s = summary(unh, periods_per_year=DAYS_PER_YEAR, cost_bp=spread_cost)
            hed_s = summary(hed, periods_per_year=DAYS_PER_YEAR, cost_bp=spread_cost + hedge_cost)
            rows.append(
                {
                    "source": source, "feature": feature,
                    "abs_net_beta": float(book["net_beta"].abs().mean()),
                    "hedge_turn": float(book["hedge_turn"].mean()),
                    "unhedged_bp": unh_s.mean_bp, "hedged_bp": hed_s.mean_bp,
                    "unhedged_sharpe": unh_s.sharpe, "hedged_sharpe": hed_s.sharpe,
                    "unhedged_t": unh_s.t_stat, "hedged_t": hed_s.t_stat,
                    "delta_sharpe": hed_s.sharpe - unh_s.sharpe,
                }
            )
            print(f"  {feature:<20s} {source:<8s} {rows[-1]['abs_net_beta']:>7.3f} "
                  f"{rows[-1]['hedge_turn']:>6.3f} {unh_s.mean_bp:>9.2f} {hed_s.mean_bp:>8.2f} "
                  f"{unh_s.sharpe:>9.2f} {hed_s.sharpe:>9.2f} {hed_s.sharpe - unh_s.sharpe:>7.3f}")

    if not rows:
        print("no scorable cells")
        return 1
    res = pl.DataFrame(rows)
    wins = int(res.filter(pl.col("delta_sharpe") > 0).height)
    t_crit = bonferroni_t(92)
    print("\n" + "=" * 92)
    print(f"hedging improves net Sharpe in {wins}/{res.height} cells")
    print(f"  median delta = {res['delta_sharpe'].median():+.3f}   "
          f"mean delta = {res['delta_sharpe'].mean():+.3f}")
    best = res.sort("hedged_sharpe", descending=True).head(1)
    print(f"  best hedged cell: {best['source'].item()} {best['feature'].item()} "
          f"Sharpe {best['hedged_sharpe'].item():.2f} t {best['hedged_t'].item():.2f}")
    survivors = res.filter((pl.col("hedged_bp") > 0.0) & (pl.col("hedged_t").abs() > t_crit))
    print(f"  hedged cells profitable and clearing t > {t_crit:.2f}: {survivors.height}/{res.height}")
    print("\nDECLARED KILL CONDITION: if hedging does not raise net Sharpe, the")
    print("idio-chart programme is closed for this repository.")
    print(f"  -> {'NOT MET (programme continues)' if wins > res.height / 2 else 'MET (programme closes)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
