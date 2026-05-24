#!/usr/bin/env python3
"""Tri-root creative gate — NOT a parameter grid.

Windows (per project docs):
  bybit_IS:      Bybit fullpit  2023-05-03 → 2026-05-18
  bybit_OOS_2022: Bybit OOS root 2022-01-01 → 2023-01-01
  binance_OOS_2020: Binance OOS   2020-01-01 → 2021-01-01

Pass = daily Sharpe >= 3.0 AND trades > 100 on EACH window.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from liquidity_migration.config import CostConfig
from liquidity_migration.momentum_factor import (
    VARIANT_ADAPTIVE_DUAL,
    VARIANT_BTC_RESIDUAL,
    VARIANT_DISPERSION_GATED,
    VARIANT_QUALITY_BLEND,
    lo_adaptive_preset,
    lo_skip0_preset,
    run_momentum_factor_research,
)
from liquidity_migration.momentum_trade_forensics import daily_sharpe_from_equity
import polars as pl

WINDOWS = {
    "bybit_IS": (Path("~/SHARED_DATA/bybit_fullpit_1h").expanduser(), "2023-05-03", "2026-05-18"),
    "bybit_OOS_2022": (Path("~/SHARED_DATA/bybit_oos_pre2023").expanduser(), "2022-01-01", "2023-01-01"),
    "binance_OOS_2020": (Path("~/SHARED_DATA/binance_oos_pit").expanduser(), "2020-01-01", "2021-01-01"),
}

# Pre-registered structural hypotheses (5 architectures, one shot each).
HYPOTHESES: list[tuple[str, callable]] = []  # filled in main


def _eval(root: Path, start: str, end: str, cfg, tag: str) -> dict:
    report_dir = root / "reports" / f"tri_gate_{tag}_{start[:4]}"
    meta = run_momentum_factor_research(
        root, config=cfg, cost_config=CostConfig(), report_dir=report_dir,
    )
    eq = pl.read_csv(report_dir / "momentum_factor_equity.csv")
    dsh = daily_sharpe_from_equity(eq)
    n = meta["rows"]["trades"]
    return {
        "daily_sharpe": dsh,
        "trades": n,
        "total_return": meta["summary"]["total_return"],
        "pass": dsh >= 3.0 and n > 100,
    }


def main() -> None:
    hypotheses = [
        ("H1_lo_skip0_baseline", lambda s, e: lo_skip0_preset(start_date=s, end_date=e)),
        (
            "H2_quality_blend",
            lambda s, e: replace(
                lo_skip0_preset(start_date=s, end_date=e),
                carry_weight=0.0,
                factor_variant=VARIANT_QUALITY_BLEND,
                max_realized_vol=1.4,
                max_turnover_rank=12,
            ),
        ),
        (
            "H3_btc_residual",
            lambda s, e: replace(
                lo_skip0_preset(start_date=s, end_date=e),
                carry_weight=0.0,
                factor_variant=VARIANT_BTC_RESIDUAL,
                max_realized_vol=1.4,
            ),
        ),
        (
            "H4_dispersion_gated",
            lambda s, e: replace(
                lo_skip0_preset(start_date=s, end_date=e),
                carry_weight=0.0,
                factor_variant=VARIANT_DISPERSION_GATED,
                min_momentum_dispersion=0.14,
                max_realized_vol=1.4,
            ),
        ),
        ("H5_adaptive_dual", lambda s, e: lo_adaptive_preset(start_date=s, end_date=e)),
        (
            "H6_adaptive_wide_uni",
            lambda s, e: replace(
                lo_adaptive_preset(start_date=s, end_date=e),
                universe_size=50,
                long_quantile=0.33,
            ),
        ),
    ]

    results: list[dict] = []
    for hname, factory in hypotheses:
        row: dict = {"hypothesis": hname, "windows": {}}
        all_pass = True
        for wname, (root, start, end) in WINDOWS.items():
            cfg = factory(start, end)
            try:
                m = _eval(root, start, end, cfg, f"{hname}_{wname}")
                row["windows"][wname] = m
                if not m["pass"]:
                    all_pass = False
                print(
                    f"{hname:28} {wname:18} sh={m['daily_sharpe']:.2f} "
                    f"n={m['trades']:3d} ret={m['total_return']:+.2%} {'PASS' if m['pass'] else 'fail'}"
                )
            except Exception as ex:
                row["windows"][wname] = {"error": str(ex)}
                all_pass = False
                print(f"{hname:28} {wname:18} ERROR {ex}")
        row["tri_pass"] = all_pass
        results.append(row)
        print()

    out = Path("~/SHARED_DATA/bybit_fullpit_1h/reports/tri_root_creative_gate.json").expanduser()
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    winners = [r for r in results if r["tri_pass"]]
    print(f"Tri-pass count: {len(winners)} / {len(results)}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
