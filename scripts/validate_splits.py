"""Per-year split-stability + MAR check for a long_native_lab variant vs baseline.

Reads each variant's long_native_baskets.csv (both venues), builds the calendar-day
equity, and reports per-year daily-aligned Sharpe + overall MAR (CAGR/|maxDD|). A
genuine improvement should hold across most years, not be driven by one period.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

S = Path.home() / "SHARED_DATA"
ROOTS = {"bybit": S / "bybit_full_pit", "binance": S / "binance_full_pit_strategy"}


def daily(path: Path) -> pl.DataFrame:
    df = pl.read_csv(path)
    return df.group_by("exit_date").agg(pl.col("basket_return").sum().alias("r")).sort("exit_date")


def metrics(d: pl.DataFrame) -> tuple[float, float, float, float]:
    if d.is_empty():
        return (0.0, float("nan"), 0.0, float("nan"))
    dates = [np.datetime64(x) for x in d["exit_date"].to_list()]
    r = np.array(d["r"].to_list(), dtype=float)
    eq = np.cumprod(1 + r)
    yrs = (dates[-1] - dates[0]) / np.timedelta64(365, "D")
    cagr = eq[-1] ** (1 / yrs) - 1 if yrs > 0 else float("nan")
    mdd = float((eq / np.maximum.accumulate(eq) - 1).min())
    sharpe = r.mean() / r.std() * np.sqrt(365) if r.std() > 0 else float("nan")
    mar = cagr / abs(mdd) if mdd < 0 else float("nan")
    return (eq[-1] - 1, sharpe, mdd, mar)


def per_year_sharpe(d: pl.DataFrame) -> dict[str, float]:
    d = d.with_columns(pl.col("exit_date").str.slice(0, 4).alias("yr"))
    out = {}
    for yr, g in d.group_by("yr"):
        r = np.array(g["r"].to_list(), dtype=float)
        out[yr[0] if isinstance(yr, tuple) else yr] = (r.mean() / r.std() * np.sqrt(365)) if r.std() > 0 else float("nan")
    return dict(sorted(out.items()))


def show(variant: str) -> None:
    for venue, root in ROOTS.items():
        p = root / "reports" / "long_native_lab" / f"{variant}__{venue}" / "long_native_baskets.csv"
        if not p.exists():
            print(f"  {variant} {venue}: MISSING {p}")
            continue
        d = daily(p)
        tot, sh, mdd, mar = metrics(d)
        py = per_year_sharpe(d)
        pys = "  ".join(f"{y}:{s:+.2f}" for y, s in py.items())
        print(f"  {variant:14s} {venue:8s} tot={tot*100:6.0f}% Sharpe={sh:.2f} maxDD={mdd*100:6.1f}% MAR={mar:.2f}")
        print(f"                 per-yr Sharpe: {pys}")


def main() -> int:
    variants = sys.argv[1:] or ["baseline", "uni30_vt60"]
    for v in variants:
        show(v)
    return 0


if __name__ == "__main__":
    sys.exit(main())
