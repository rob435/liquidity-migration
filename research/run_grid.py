"""WS-1 / WS-3 — backtest grids on the corrected (entry-lag-fixed) harness.

To keep grids cheap, Layer 1 (alpha) and Layer 2 (portfolio) are computed once
per distinct alpha/portfolio config and only Layer 3 (execution: cost,
stop-fill-mode, hold) is re-simulated per cell. Features are loaded once.

Studies:
  cost   — WS-1: cost grid {10,15,20,28.8,48,67} bps x stop-fill {stop,bar_extreme}.
           Layer 1-2 identical across all cells -> one book, many simulates.
  regime — WS-3: continuous scaler vs hard gate (regime_hard_threshold=-0.05),
           each at {28.8 stop, 48 stop, 28.8 bar_extreme}.

Usage:  python -m research.run_grid <window> <study>
"""
from __future__ import annotations

import sys
import time
from dataclasses import replace

from research._panel import load_features
from liquidity_migration.reversion_alpha import (
    ReversionConfig, compute_reversion_score, construct_target_book, prepare_klines, simulate,
)

COST_GRID = [10.0, 15.0, 20.0, 28.8, 48.0, 67.0]
FILL_MODES = ["stop", "bar_extreme"]


def _print_row(label: str, m: dict) -> None:
    print(f"  {label:<34}{m['trades']:>7}{m['total_return']:>12.4f}"
          f"{m['max_drawdown']:>11.4f}{m['sharpe']:>9.2f}{m['win_rate']:>9.4f}")


def study_cost(window: str, features, klines, prepared, base: ReversionConfig) -> None:
    # Layer 1-2 identical for the whole grid -> compute book once.
    scored = compute_reversion_score(features, base)
    book = construct_target_book(scored, base)
    print(f"\n=== WS-1 cost grid — {window} (book rows={book.height}) ===")
    print(f"  {'cost_bps / fill':<34}{'trades':>7}{'total_ret':>12}"
          f"{'max_dd':>11}{'sharpe':>9}{'win':>9}")
    for fill in FILL_MODES:
        for cost in COST_GRID:
            cfg = replace(base, cost_round_trip_bps=cost, stop_fill_mode=fill)
            res = simulate(book, klines, cfg, prepared=prepared)
            _print_row(f"{cost:>6.1f} bps  {fill}", res["metrics"])
        print()


def study_regime(window: str, features, klines, prepared, base: ReversionConfig) -> None:
    print(f"\n=== WS-3 regime gate — {window} ===")
    print(f"  {'regime / cost / fill':<34}{'trades':>7}{'total_ret':>12}"
          f"{'max_dd':>11}{'sharpe':>9}{'win':>9}")
    variants = [
        ("continuous", None),
        ("hard@-0.05", -0.05),
    ]
    l3 = [("28.8 stop", 28.8, "stop"), ("48 stop", 48.0, "stop"),
          ("28.8 advfill", 28.8, "bar_extreme")]
    for rlabel, thr in variants:
        l12 = replace(base, regime_hard_threshold=thr)
        scored = compute_reversion_score(features, l12)
        book = construct_target_book(scored, l12)
        for clabel, cost, fill in l3:
            cfg = replace(l12, cost_round_trip_bps=cost, stop_fill_mode=fill)
            res = simulate(book, klines, cfg, prepared=prepared)
            _print_row(f"{rlabel}  {clabel}", res["metrics"])
        print()


def main() -> None:
    window = sys.argv[1] if len(sys.argv) > 1 else "is_train"
    study = sys.argv[2] if len(sys.argv) > 2 else "cost"
    t0 = time.time()
    features, klines, start, end = load_features(window)
    base = ReversionConfig(start=start, end=end, entry_delay_hours=1)  # corrected harness
    prepared = prepare_klines(klines)  # built once, reused across every grid cell
    print(f"window={window}  features+klines prepared in {time.time()-t0:.0f}s")
    if study == "cost":
        study_cost(window, features, klines, prepared, base)
    elif study == "regime":
        study_regime(window, features, klines, prepared, base)
    else:
        raise SystemExit(f"unknown study: {study}")
    print(f"total {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
