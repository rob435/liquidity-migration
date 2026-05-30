"""Confirm the `div` risk-engineering improvement reproduces on the CURRENT v11a base,
both venues, before promoting it to the deployed profile (EXPLORATORY -> promotion evidence).

baseline = _v11a_long_native_config() as deployed (uni10, mc5, no vol-target)
div      = baseline + {universe_size=50, max_concurrent_positions=10,
                       enable_vol_target=True, vol_target_annual=0.60, vol_target_max_scale=1.0}
Reports daily-aligned Sharpe, MAR, maxDD, total return, trades, run_label from each run's equity.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from liquidity_migration.config import load_config  # noqa: E402
from liquidity_migration.long_native import run_long_native_research  # noqa: E402
from liquidity_migration.long_native_event_demo import _v11a_long_native_config  # noqa: E402

S = Path.home() / "SHARED_DATA"
VENUES = {"bybit": S / "bybit_full_pit", "binance": S / "binance_full_pit_strategy"}
COSTS = load_config("configs/volume_alpha.default.yaml").costs
DIV = dict(universe_size=50, max_concurrent_positions=10, enable_vol_target=True,
           vol_target_annual=0.60, vol_target_max_scale=1.0)


def eq_metrics(report_dir: Path):
    csv = report_dir / "long_native_equity.csv"
    if not csv.exists():
        return None
    e = pl.read_csv(csv)
    if e.is_empty():
        return None
    r = e["basket_return"].to_numpy()
    days = e["date"].to_list()
    eq = np.cumprod(1 + r)
    d = np.array([np.datetime64(x) for x in days])
    yrs = (d[-1] - d[0]) / np.timedelta64(365, "D")
    sh = r.mean() / r.std() * np.sqrt(365) if r.std() > 0 else float("nan")
    mdd = float((eq / np.maximum.accumulate(eq) - 1).min())
    mar = (eq[-1] ** (1 / yrs) - 1) / abs(mdd) if mdd < 0 else float("nan")
    return sh, mar, mdd, eq[-1]


def main() -> int:
    print(f"  {'venue':>8} {'config':>14} {'trades':>6} {'Sharpe':>7} {'MAR':>6} {'maxDD%':>7} {'final':>7}  label")
    for tag, root in VENUES.items():
        for name, ov in (("v11a baseline", {}), ("v11a + div", DIV)):
            cfg = replace(_v11a_long_native_config(), **ov)
            rep = Path(root) / "reports" / "div_promo_verify" / name.replace(" ", "_").replace("+", "p")
            payload = run_long_native_research(root, config=cfg, cost_config=COSTS, report_dir=rep)
            trades = payload.get("rows", {}).get("trades", 0)
            label = payload.get("run_label", "?")
            m = eq_metrics(rep)
            if m:
                sh, mar, mdd, fin = m
                print(f"  {tag:>8} {name:>14} {trades:>6} {sh:7.2f} {mar:6.2f} {mdd*100:7.1f} {fin:7.2f}  {label}")
            else:
                print(f"  {tag:>8} {name:>14} {trades:>6}  (no equity)  {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
