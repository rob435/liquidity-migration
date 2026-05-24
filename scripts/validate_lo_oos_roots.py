#!/usr/bin/env python3
"""One-shot OOS sanity: lo_sharpe3 and lo_skip0 on pre-2023 roots (label: oos_sanity)."""
from __future__ import annotations

import json
from pathlib import Path

from liquidity_migration.config import CostConfig
from liquidity_migration.momentum_factor import lo_sharpe3_preset, lo_skip0_preset, run_momentum_factor_research
from liquidity_migration.momentum_trade_forensics import daily_sharpe_from_equity
import polars as pl

ROOTS = {
    "bybit_pre2023": Path("~/SHARED_DATA/bybit_oos_pre2023").expanduser(),
    "binance_pre2023": Path("~/SHARED_DATA/binance_oos_pit").expanduser(),
}
WINDOWS = {
    "bybit_pre2023": ("2021-01-01", "2023-05-01"),
    "binance_pre2023": ("2020-01-01", "2023-05-01"),
}
OUT = Path("~/SHARED_DATA/bybit_fullpit_1h/reports/momentum_lo_oos_sanity").expanduser()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    for preset_name, factory in (("lo_skip0", lo_skip0_preset), ("lo_sharpe3", lo_sharpe3_preset)):
        for root_name, root in ROOTS.items():
            start, end = WINDOWS[root_name]
            cfg = factory(start_date=start, end_date=end)
            report = OUT / f"{preset_name}_{root_name}"
            try:
                meta = run_momentum_factor_research(root, config=cfg, cost_config=CostConfig(), report_dir=report)
                eq = pl.read_csv(report / "momentum_factor_equity.csv")
                daily_sh = daily_sharpe_from_equity(eq)
                results.append({
                    "preset": preset_name,
                    "root": root_name,
                    "trades": meta["rows"]["trades"],
                    "total_return": meta["summary"]["total_return"],
                    "daily_sharpe": daily_sh,
                    "basket_sharpe": meta["summary"]["sharpe_like"],
                    "max_dd": meta["summary"]["max_drawdown"],
                    "splits": meta["splits"],
                    "run_label": "oos_sanity_one_shot",
                })
                print(
                    f"{preset_name} @ {root_name}: trades={meta['rows']['trades']} "
                    f"ret={meta['summary']['total_return']:+.2%} daily_sh={daily_sh:.2f}"
                )
            except Exception as e:
                results.append({"preset": preset_name, "root": root_name, "error": str(e)})
                print(f"{preset_name} @ {root_name}: FAILED {e}")

    (OUT / "oos_sanity.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {OUT / 'oos_sanity.json'}")


if __name__ == "__main__":
    main()
