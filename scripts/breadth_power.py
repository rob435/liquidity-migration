#!/usr/bin/env python3
"""Days-to-significance calculator: what breadth buys in statistical power.

For a book taking ``bets_per_day`` positions per day, each with per-bet net
expectancy ``edge_bps`` and per-bet volatility ``vol_bps``, and average
pairwise correlation ``rho`` between same-day bets, the daily Sharpe is

    SR_daily = (N * edge) / (vol * sqrt(N * (1 + (N - 1) * rho)))

and the forward days needed for a t-statistic of ``t_target`` on the mean is

    T = (t_target / SR_daily)^2

With rho = 0 this reduces to T = t^2 * vol^2 / (N * edge^2): power is linear
in independent bets per day — the only lever that shortens the
years-to-significance problem without waiting.

This is a design aid, not evidence: it says how long a *hypothesized* edge
takes to distinguish from zero, not whether the edge exists.
"""

from __future__ import annotations

import argparse
import math


def daily_sharpe(edge_bps: float, vol_bps: float, bets_per_day: float, rho: float) -> float:
    if edge_bps <= 0.0 or vol_bps <= 0.0 or bets_per_day <= 0.0:
        raise ValueError("edge, vol, and bets_per_day must be positive")
    if not 0.0 <= rho < 1.0:
        raise ValueError("rho must be within [0, 1)")
    n = bets_per_day
    return (n * edge_bps) / (vol_bps * math.sqrt(n * (1.0 + (n - 1.0) * rho)))


def days_to_t(edge_bps: float, vol_bps: float, bets_per_day: float, rho: float, t_target: float) -> float:
    return (t_target / daily_sharpe(edge_bps, vol_bps, bets_per_day, rho)) ** 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edge-bps", type=float, default=15.0, help="Per-bet net expectancy.")
    parser.add_argument("--vol-bps", type=float, default=300.0, help="Per-bet volatility.")
    parser.add_argument("--rho", type=float, default=0.15, help="Average same-day pairwise correlation.")
    parser.add_argument("--t-target", type=float, default=2.0)
    parser.add_argument(
        "--bets-per-day",
        type=float,
        nargs="*",
        default=[0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 40.0],
    )
    args = parser.parse_args()
    print(
        f"edge={args.edge_bps}bps vol={args.vol_bps}bps rho={args.rho} "
        f"t_target={args.t_target}"
    )
    print(f"{'bets/day':>9} {'SR_daily':>9} {'SR_annual':>10} {'days to t':>10} {'years':>7}")
    for n in args.bets_per_day:
        sr = daily_sharpe(args.edge_bps, args.vol_bps, n, args.rho)
        days = days_to_t(args.edge_bps, args.vol_bps, n, args.rho, args.t_target)
        print(f"{n:>9.1f} {sr:>9.4f} {sr * math.sqrt(365):>10.2f} {days:>10.0f} {days / 365:>7.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
