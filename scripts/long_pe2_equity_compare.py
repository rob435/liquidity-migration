"""PE2 vs baseline — honest daily-MTM equity comparison (both venues).

Reads tonight's long_regularity_2026-06-10 run artifacts (00_baseline, LR30_prov),
renders MTM equity + drawdown per venue. Display artifact for the PE2 receipt;
numbers come from the receipts, not this chart.

Usage:
    PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=6 .venv/bin/python scripts/long_pe2_equity_compare.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import polars as pl  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from liquidity_migration.long_native import _mtm_daily_curve, _mtm_summary  # noqa: E402
from liquidity_migration.storage import read_dataset_columns  # noqa: E402

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "long_pe2_equity_compare_2026-06-10.png"


def main() -> None:
    fig, axes = plt.subplots(2, 2, figsize=(15, 8), sharex="col",
                             gridspec_kw={"height_ratios": [3, 1]})
    for col, venue in enumerate(["bybit", "binance"]):
        root = SHARED / f"{venue}_full_pit"
        base_dir = root / "reports" / "long_regularity_2026-06-10"
        klines = read_dataset_columns(root, "klines_1h", columns=["ts_ms", "symbol", "date", "close"])
        ax, axd = axes[0][col], axes[1][col]
        for cell, color, lw, label in [
            ("00_baseline", "#7f7f7f", 1.1, "deployed v11a (baseline)"),
            ("LR30_prov", "#1f77b4", 1.5, "PE2 provisional entry (NOT adopted)"),
        ]:
            trades = pl.read_csv(base_dir / cell / "long_native_trades.csv")
            mtm = _mtm_daily_curve(trades, klines)
            s = _mtm_summary(mtm)
            days = mtm["date"].to_list()
            ax.plot(days, mtm["equity"].to_list(), color=color, lw=lw,
                    label=(f"{label}: ret {s['mtm_total_return']:+.1%} "
                           f"DD {s['mtm_max_drawdown']:.1%} Sh {s['mtm_daily_sharpe']} "
                           f"active {s['mtm_active_day_frac']:.0%}"))
            if cell == "LR30_prov":
                axd.fill_between(days, [d * 100 for d in mtm["drawdown"].to_list()], 0.0,
                                 color="#1f77b4", alpha=0.45, label="PE2 MTM drawdown")
            else:
                axd.plot(days, [d * 100 for d in mtm["drawdown"].to_list()],
                         color="#7f7f7f", lw=0.9, label="baseline MTM drawdown")
        ax.set_title(f"{venue} — long FC book, honest daily MTM (deployed 3x cost stress)")
        ax.legend(loc="upper left", fontsize=8)
        ax.grid(alpha=0.25)
        ax.set_ylabel("equity (x)")
        axd.legend(loc="lower left", fontsize=7)
        axd.grid(alpha=0.25)
        axd.set_ylabel("drawdown (%)")
        del klines
    fig.suptitle("PE2 provisional trigger-hour entry vs deployed baseline — 2023-04..2026-05. "
                 "PE2 FAILED its binance adoption bar by 1% (receipt long-provisional-entry-engine-2026-06-10); "
                 "shown for review, NOT deployed. OOS re-judgment armed.", fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(OUT, dpi=130)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
