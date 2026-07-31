#!/usr/bin/env python3
"""Stop-level counterfactual on the CONTINUOUS reconstruction's own trades.

For each candidate stop fraction, any modelled trade whose recorded ``mae``
breaches the stop is closed at exactly the stop instead of its modelled exit.
Costs and funding stay at their recorded values and the trade keeps its recorded
exit date; both are second-order against a sign flip.

Known optimism: ``mae`` comes from hourly bar extremes while the native stop
triggers on MarkPrice, and a real triggered stop is a market order that slips in
a squeeze. Tight-stop numbers are therefore ceilings.

Purpose: choose the declared ``stop_loss_pct`` so the deployed exit rule and the
backtest model the same book — a wide native backstop plus the strategy's own
TP/max-hold exits. Method and caveats: docs/research_findings.md.

Usage:
  python scripts/continuous_stop_counterfactual.py \
      --trades-root ~/SHARED_DATA/bybit_full_pit/reports/equity_curves_phase0/continuous/components/bybit
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import date
from pathlib import Path
from typing import cast

import numpy as np
import polars as pl

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liquidity_migration.continuous_profile import ACTIVE_CONTINUOUS_COMPONENTS  # noqa: E402

# Deployed blend weights by artifact cell.
COMPONENT_WEIGHTS = {
    component.artifact_cell: component.weight for component in ACTIVE_CONTINUOUS_COMPONENTS
}

# Above ~45% a stop never binds before 2x-leverage liquidation, so 40% is the
# ceiling; ``None`` is the unstopped control row.
GRID: tuple[float | None, ...] = (
    None, 0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.12, 0.10, 0.08, 0.05, 0.03, 0.02,
)


def load_trades(root: Path) -> pl.DataFrame:
    frames = []
    for component, weight in COMPONENT_WEIGHTS.items():
        path = root / component / "continuous_trades.csv"
        if not path.is_file():
            raise SystemExit(f"missing trades ledger: {path}")
        frames.append(
            pl.read_csv(path).with_columns(
                pl.lit(component).alias("component"),
                pl.lit(float(weight)).alias("component_blend_weight"),
            )
        )
    return pl.concat(frames, how="vertical_relaxed")


def apply_stop(trades: pl.DataFrame, stop: float | None) -> pl.DataFrame:
    """Replace each breaching trade's gross return with exactly -stop."""
    blend = pl.col("component_blend_weight")
    if stop is None:
        return trades.with_columns(
            (pl.col("net_return") * blend).alias("cf_net"),
            pl.lit(False).alias("stopped"),
        )
    stopped = pl.col("mae") <= -stop
    cf_gross = pl.when(stopped).then(-stop).otherwise(pl.col("gross_trade_return"))
    return trades.with_columns(
        (
            (pl.col("notional_weight") * cf_gross + pl.col("cost_return") + pl.col("funding_return")) * blend
        ).alias("cf_net"),
        stopped.alias("stopped"),
    )


def daily_stats(trades: pl.DataFrame) -> tuple[float, float, float, float, float]:
    daily = (
        trades.group_by("exit_date")
        .agg(pl.col("cf_net").sum().alias("ret"))
        .with_columns(pl.col("exit_date").str.to_date())
        .sort("exit_date")
    )
    first = cast(date, daily["exit_date"].min())
    last = cast(date, daily["exit_date"].max())
    span = pl.date_range(first, last, eager=True).alias("exit_date")
    full = pl.DataFrame({"exit_date": span}).join(daily, on="exit_date", how="left").fill_null(0.0)
    r = full["ret"].to_numpy()
    total = float(r.sum())
    mean, std = float(r.mean()), float(r.std(ddof=1))
    sharpe = mean / std * math.sqrt(365.0) if std > 0 else float("nan")
    years = len(r) / 365.0
    t = sharpe * math.sqrt(years) if math.isfinite(sharpe) else float("nan")
    equity = r.cumsum()
    max_dd = float((equity - np.maximum.accumulate(equity)).min())
    worst = float(r.min())
    return total, sharpe, t, -max_dd, -worst


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trades-root", type=Path, required=True)
    args = ap.parse_args()

    trades = load_trades(args.trades_root.expanduser())
    n = trades.height
    print(f"CONTINUOUS stop counterfactual - {n} trades from {len(COMPONENT_WEIGHTS)} components")
    print("fills at exactly the stop; costs/funding held at recorded values (see docstring)\n")

    hdr = f"{'stop':>6s} {'breached':>9s} {'tp->stop':>9s} {'total':>8s} {'Sharpe':>7s} {'t':>7s} {'maxDD':>7s} {'worst d':>8s}"
    print(hdr)
    print("-" * len(hdr))
    for stop in GRID:
        cf = apply_stop(trades, stop)
        total, sharpe, t, max_dd, worst = daily_stats(cf)
        breached = int(cf["stopped"].sum())
        tp_flipped = int(cf.filter(pl.col("stopped") & (pl.col("exit_reason") == "take_profit")).height)
        label = "none" if stop is None else f"{stop * 100:.0f}%"
        pct = f"{breached / n * 100:.1f}%"
        print(
            f"{label:>6s} {breached:4d}/{pct:>5s} {tp_flipped:9d} {total * 100:+7.2f}% "
            f"{sharpe:+7.2f} {t:+7.2f} {max_dd * 100:6.2f}% {worst * 100:+7.2f}%"
        )
    print("\nRead with S16.3's caveats: hourly-bar mae vs MarkPrice trigger, exact-stop")
    print("fills (optimistic at tight stops), no capital redeployment after early exits.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
