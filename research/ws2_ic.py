"""WS-2 — IC vs (entry-lag x hold-horizon).

The entry-lag bug (WS-0d) turned entry timing into a real, costless lever, so
WS-2 is a 2-D study, not horizon alone:

  A. lag-decay   — IC of the signal at a fixed 3d horizon, entered 1/6/12/24h
                   after the signal close. Confirms (or refutes) "enter fresh".
  B. horizon     — IC of the signal at entry-lag 1h, across forward horizons
                   1/2/3/5/7/10/14 days. Identifies the best raw and
                   cost-adjusted horizon.

Cost-adjusted IC proxy: a signal with IC c on an H-day move captures a gross
edge that scales with the cross-sectional return spread (grows with H), against
a fixed round-trip cost. We report IC and a cost-adjusted score = mean daily
(top-decile minus bottom-decile) net short return at 28.8 bps round-trip.

Measured on IS-train only (methodology law: IC before P&L, on the train window).

Usage:  python -m research.ws2_ic <window>   (default is_train)
"""
from __future__ import annotations

import sys

import numpy as np
import polars as pl

from research._panel import load_scored
from liquidity_migration.ic_diagnostic import add_forward_short_returns, cross_sectional_ic

SIGNALS = ["reversion_score", "z_rank_jump", "signal_day_range_pct", "return_7d"]
HORIZONS = [1, 2, 3, 5, 7, 10, 14]
LAGS = [1, 6, 12, 24]
COST_RT = 28.8 / 10_000.0  # round-trip cost as a fraction


def decile_spread_net(panel: pl.DataFrame, signal: str, fwd_col: str,
                      min_names: int = 10) -> float:
    """Mean daily net short return of a top-decile-short / bottom-decile-long
    decile spread — a P&L-flavoured companion to the rank IC. One round-trip
    cost is charged per leg per day."""
    sub = panel.select(["date", signal, fwd_col]).drop_nulls()
    daily = []
    for _, day in sub.partition_by("date", as_dict=True).items():
        if day.height < min_names:
            continue
        d = day.sort(signal, descending=True)
        k = max(1, round(day.height * 0.10))
        top = d.head(k)[fwd_col].mean()      # most extended -> short -> fwd short return
        bot = d.tail(k)[fwd_col].mean()      # least extended -> long leg
        daily.append((top - bot) / 2.0 - COST_RT)
    return float(np.mean(daily)) if daily else float("nan")


def main() -> None:
    window = sys.argv[1] if len(sys.argv) > 1 else "is_train"
    print(f"WS-2 IC study — window={window}")
    scored, klines = load_scored(window)
    print(f"scored rows={scored.height}  days={scored['date'].n_unique()}\n")

    # --- A. lag-decay at fixed 3d horizon ---
    print("=== A. lag-decay (3d horizon) ===")
    print(f"{'signal':<18}{'lag=1h':>10}{'lag=6h':>10}{'lag=12h':>10}{'lag=24h':>10}")
    lag_panels = {lag: add_forward_short_returns(scored, klines, [3], entry_delay_hours=lag)
                  for lag in LAGS}
    for sig in SIGNALS:
        cells = []
        for lag in LAGS:
            ic = cross_sectional_ic(lag_panels[lag], sig, "fwd_short_return_3d").mean_ic
            cells.append(f"{ic:>10.4f}")
        print(f"  {sig:<16}{''.join(cells)}")

    # --- B. horizon sweep at lag=1h ---
    print("\n=== B. horizon sweep (entry lag 1h) ===")
    panel = add_forward_short_returns(scored, klines, HORIZONS, entry_delay_hours=1)
    print(f"{'signal':<18}{'horizon':>9}{'mean_ic':>10}{'t_stat':>9}{'decile_net':>12}{'n_days':>8}")
    for sig in SIGNALS:
        for h in HORIZONS:
            col = f"fwd_short_return_{h}d"
            r = cross_sectional_ic(panel, sig, col)
            spread = decile_spread_net(panel, sig, col)
            print(f"  {sig:<16}{h:>9}{r.mean_ic:>10.4f}{r.t_stat:>9.2f}"
                  f"{spread:>12.5f}{r.n_days:>8}")
        print()


if __name__ == "__main__":
    main()
