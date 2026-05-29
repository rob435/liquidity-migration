"""Combined long+short book diversification analysis (research/EXPLORATORY).

Replicates portfolio_hedge.run_portfolio_hedge_report's additive-overlay method
(combined daily return = short_return + w * long_return) but reads each sleeve's
own baskets file and adds the repo's primary metrics: calendar-day-aligned
Sharpe and MAR (both leverage-invariant, so they isolate the diversification
benefit from any gross-exposure illusion). Measured over the date OVERLAP so
short-alone (w=0) and combined are apples-to-apples.

NOT a promotion artifact: the long sleeve's standalone return is a 2023-2026
in-sample phenomenon (failed pre-2023 OOS per the deleted long_native_findings).
The robust, low-overfitting signal here is the short<->long correlation and the
variance-reduction it implies.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

S = Path.home() / "SHARED_DATA"


def daily(path: Path) -> dict[str, float]:
    df = pl.read_csv(path)
    g = df.group_by("exit_date").agg(pl.col("basket_return").sum().alias("r")).sort("exit_date")
    return dict(zip(g["exit_date"].to_list(), g["r"].to_list()))


def metrics(dates: list[str], rets: list[float]) -> tuple[float, float, float, float, float]:
    r = np.array(rets, dtype=float)
    eq = np.cumprod(1 + r)
    tot = eq[-1] - 1
    yrs = (np.datetime64(dates[-1]) - np.datetime64(dates[0])) / np.timedelta64(365, "D")
    cagr = eq[-1] ** (1 / yrs) - 1 if yrs > 0 else float("nan")
    peak = np.maximum.accumulate(eq)
    mdd = float((eq / peak - 1).min())
    sharpe = r.mean() / r.std() * np.sqrt(365) if r.std() > 0 else float("nan")
    mar = cagr / abs(mdd) if mdd < 0 else float("nan")
    return tot, cagr, sharpe, mdd, mar


def analyze(label: str, short_path: Path, long_path: Path, weights: list[float]) -> None:
    if not short_path.exists() or not long_path.exists():
        miss = "short" if not short_path.exists() else "long"
        print(f"\n### {label}: MISSING {miss} ledger")
        return
    s, lg = daily(short_path), daily(long_path)
    lo, hi = max(min(s), min(lg)), min(max(s), max(lg))
    cal = [str(d) for d in np.arange(np.datetime64(lo), np.datetime64(hi) + 1, dtype="datetime64[D]")]
    sv = np.array([s.get(d, 0.0) for d in cal])
    lv = np.array([lg.get(d, 0.0) for d in cal])
    corr_cal = float(np.corrcoef(sv, lv)[0, 1])
    common = [d for d in cal if d in s and d in lg]
    corr_co = float(np.corrcoef([s[d] for d in common], [lg[d] for d in common])[0, 1]) if len(common) > 2 else float("nan")
    worst20 = set(pl.DataFrame({"d": list(s), "r": list(s.values())}).sort("r").head(20)["d"].to_list())
    long_on_worst20 = sum(lg.get(d, 0.0) for d in worst20)
    print(f"\n### {label}   overlap {lo} -> {hi}  ({len(cal)}d)")
    print(f"    corr(calendar)={corr_cal:+.3f}   corr(common {len(common)}d)={corr_co:+.3f}")
    print(f"    long P&L added on short's worst-20 days: {long_on_worst20*100:+.2f}%   (active: short {int((sv!=0).sum())}d, long {int((lv!=0).sum())}d)")
    print(f"    {'w':>5} {'tot%':>8} {'CAGR%':>7} {'Sharpe':>7} {'maxDD%':>8} {'MAR':>6}")
    base = None
    for w in weights:
        tot, cagr, sh, mdd, mar = metrics(cal, (sv + w * lv).tolist())
        tag = "  <- short-alone" if w == 0 else ""
        if w == 0:
            base = (sh, mar)
        print(f"    {w:5.2f} {tot*100:8.1f} {cagr*100:7.1f} {sh:7.2f} {mdd*100:8.1f} {mar:6.2f}{tag}")
    if base:
        lt, lc, lsh, lmdd, lmar = metrics(cal, lv.tolist())
        print(f"    baseline short-alone: Sharpe {base[0]:.2f}, MAR {base[1]:.2f}")
        print(f"    long-alone (over overlap): tot {lt*100:.1f}%  Sharpe {lsh:.2f}  maxDD {lmdd*100:.1f}%  MAR {lmar:.2f}")
        # max-Sharpe of two uncorrelated sleeves ~ sqrt(S_short^2 + S_long^2)
        import math
        print(f"    sqrt(S_short^2+S_long^2) reference = {math.sqrt(base[0]**2 + lsh**2):.2f}")


def main() -> int:
    W = [0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]
    analyze(
        "BINANCE (full_pit_strategy, clean full-PIT)",
        S / "binance_full_pit_strategy/reports/volume_event_research/volume_event_best_baskets.csv",
        S / "binance_full_pit_strategy/reports/long_native_v11a_rerun/fc_min_day_015/long_native_baskets.csv",
        W,
    )
    analyze(
        "BYBIT (r1 baseline short + v11a long)",
        S / "bybit_full_pit/reports/r1_filter_audit_2026-05-28/00_baseline/volume_event_best_baskets.csv",
        S / "bybit_full_pit/reports/long_native_v11a_rerun/fc_min_day_015/long_native_baskets.csv",
        W,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
