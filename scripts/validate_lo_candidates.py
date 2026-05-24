#!/usr/bin/env python3
"""Validate long-only momentum candidates from trade-study discoveries."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from liquidity_migration.config import CostConfig
from liquidity_migration.momentum_factor import MomentumFactorConfig, run_momentum_factor_research
from liquidity_migration.momentum_trade_forensics import daily_sharpe_from_equity
from liquidity_migration.storage import read_dataset_columns
import polars as pl

ROOT = Path("~/SHARED_DATA/bybit_fullpit_1h").expanduser()
START, END = "2023-05-03", "2026-05-18"

LO_SKIP0 = dict(
    mode="long_only",
    momentum_skip_days=0,
    carry_weight=1.5,
    require_positive_ts_momentum_for_longs=True,
    vol_target_annual=0.15,
    regime_off_scale=0.0,
    use_regime_filter=True,
)

CANDIDATES = [
    ("LO_skip0_baseline", LO_SKIP0),
    ("LO_best_sweep_carry0", {**LO_SKIP0, "carry_weight": 0.0}),
    ("LO_vol_cap_063", {**LO_SKIP0, "max_realized_vol": 0.63}),
    ("LO_top6_liquid", {**LO_SKIP0, "max_turnover_rank": 6}),
    ("LO_carry0_vol063", {**LO_SKIP0, "carry_weight": 0.0, "max_realized_vol": 0.63}),
    ("LO_carry0_top6", {**LO_SKIP0, "carry_weight": 0.0, "max_turnover_rank": 6}),
    ("LO_3d_rebal", {**LO_SKIP0, "rebalance_days": 3, "carry_weight": 0.0}),
    ("LO_wide_q33", {**LO_SKIP0, "long_quantile": 0.33, "carry_weight": 0.0}),
]


def main() -> None:
    rows = []
    for name, overrides in CANDIDATES:
        cfg = replace(MomentumFactorConfig(start_date=START, end_date=END), **overrides)
        out = ROOT / f"reports/momentum_lo_{name}"
        meta = run_momentum_factor_research(ROOT, config=cfg, cost_config=CostConfig(), report_dir=out)
        eq = pl.read_csv(out / "momentum_factor_equity.csv")
        daily_sh = daily_sharpe_from_equity(eq)
        s = meta["summary"]
        rows.append({
            "name": name,
            "trades": s.get("trades"),
            "total_return": s.get("total_return"),
            "sharpe_basket": s.get("sharpe_like"),
            "sharpe_daily": daily_sh,
            "max_drawdown": s.get("max_drawdown"),
            "win_rate": s.get("trade_win_rate"),
            "avg_split_sharpe": meta["promotion"]["avg_split_sharpe"],
        })
        print(f"{name}: trades={rows[-1]['trades']} ret={rows[-1]['total_return']:+.2%} daily_sh={daily_sh:.2f}")

    summary = sorted(rows, key=lambda r: r["sharpe_daily"], reverse=True)
    out_path = ROOT / "reports/momentum_lo_candidates_summary.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
