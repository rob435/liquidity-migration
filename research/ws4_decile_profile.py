"""Chase the contradiction: signal_day_range_pct has IC > 0 but decile_net < 0
on IS-train. IC is rank-based (robust); decile_net is mean-based (outlier-
sensitive). This profiles all 10 deciles — mean, median, win-rate of the
forward short return — to see whether the relationship is monotone in the
MEDIAN (rank skill, mean killed by tail outliers) or genuinely non-monotone.

Usage:  python -m research.ws4_decile_profile <window> [signal] [horizon]
"""
from __future__ import annotations

import sys

import numpy as np
import polars as pl

from research._panel import load_scored
from liquidity_migration.ic_diagnostic import add_forward_short_returns


def main() -> None:
    window = sys.argv[1] if len(sys.argv) > 1 else "is_train"
    signal = sys.argv[2] if len(sys.argv) > 2 else "signal_day_range_pct"
    horizon = int(sys.argv[3]) if len(sys.argv) > 3 else 10

    scored, klines = load_scored(window)
    panel = add_forward_short_returns(scored, klines, [horizon], entry_delay_hours=1)
    fwd = f"fwd_short_return_{horizon}d"
    sub = panel.select(["date", signal, fwd]).drop_nulls()

    # accumulate per-decile fwd-short-return samples across all days
    buckets: list[list[float]] = [[] for _ in range(10)]
    for _, day in sub.partition_by("date", as_dict=True).items():
        if day.height < 20:
            continue
        d = day.sort(signal, descending=False)   # ascending: decile 0 = lowest signal
        n = d.height
        vals = d[fwd].to_list()
        for i in range(10):
            lo = i * n // 10
            hi = (i + 1) * n // 10
            buckets[i].extend(vals[lo:hi])

    print(f"decile profile — {window}  signal={signal}  horizon={horizon}d")
    print(f"  decile 0 = lowest {signal}, decile 9 = highest (the names shorted)")
    print(f"  {'decile':<8}{'n':>8}{'mean_fwd':>11}{'median_fwd':>12}{'win_rate':>10}{'p05':>9}{'p95':>9}")
    means = []
    for i, b in enumerate(buckets):
        a = np.array(b)
        means.append(a.mean())
        print(f"  {i:<8}{len(a):>8}{a.mean()*100:>10.3f}%{np.median(a)*100:>11.3f}%"
              f"{(a > 0).mean():>10.3f}{np.percentile(a,5)*100:>8.2f}%{np.percentile(a,95)*100:>8.2f}%")
    top, bot = means[9], means[0]
    print(f"\n  top-decile mean {top*100:+.3f}%  bottom-decile mean {bot*100:+.3f}%"
          f"  -> spread (top-bot) {(top-bot)*100:+.3f}%")
    print("  fwd_short_return > 0 means the name FELL (good for a short).")
    print("  If median rises with decile but the top-decile MEAN collapses,")
    print("  the rank-IC is real but un-tradeable: fat right tail (pumps that")
    print("  kept running) destroys the mean P&L of shorting the top decile.")


if __name__ == "__main__":
    main()
