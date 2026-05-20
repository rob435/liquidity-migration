"""WS-4 diligence — decompose the signal_day_range_pct decile spread.

The decile_net edge could live in the SHORT leg (shorting high-range names) or
the LONG leg (longing low-range names). The liquidity-migration strategy is
short-only, so only the short leg helps it; a long-leg-only edge would mean the
signal needs a market-neutral packaging to be captured at all.

Also reports the leg returns vs the universe mean (the alt-beta level) and a
crude time-concentration (best vs worst quarter).

Usage:  python -m research.ws4_legs <window> [signal] [horizon]
"""
from __future__ import annotations

import sys

import numpy as np
import polars as pl

from research._panel import load_scored
from liquidity_migration.ic_diagnostic import add_forward_short_returns

COST_RT = 28.8 / 10_000.0


def main() -> None:
    window = sys.argv[1] if len(sys.argv) > 1 else "oos_binance"
    signal = sys.argv[2] if len(sys.argv) > 2 else "signal_day_range_pct"
    horizon = int(sys.argv[3]) if len(sys.argv) > 3 else 10

    scored, klines = load_scored(window)
    panel = add_forward_short_returns(scored, klines, [horizon], entry_delay_hours=1)
    fwd = f"fwd_short_return_{horizon}d"
    sub = panel.select(["date", signal, fwd]).drop_nulls()

    top_leg, bot_leg, uni_mean = [], [], []
    for _, day in sub.partition_by("date", as_dict=True).items():
        if day.height < 10:
            continue
        d = day.sort(signal, descending=True)
        k = max(1, round(day.height * 0.10))
        top_leg.append(d.head(k)[fwd].mean())          # short the high-signal names
        bot_leg.append(d.tail(k)[fwd].mean())          # the low-signal names
        uni_mean.append(day[fwd].mean())               # universe mean fwd short return

    top = np.array(top_leg); bot = np.array(bot_leg); uni = np.array(uni_mean)
    n = len(top)
    print(f"WS-4 leg decomposition — {window}  signal={signal}  horizon={horizon}d  days={n}")
    print(f"  universe mean fwd short return : {uni.mean()*100:+.3f}%   (the alt-beta level)")
    print(f"  SHORT leg (short high-{signal[:14]}): gross {top.mean()*100:+.3f}%  net {(top.mean()-COST_RT)*100:+.3f}%")
    print(f"  LONG  leg (long low-{signal[:14]}) : gross {(-bot.mean())*100:+.3f}%  net {(-bot.mean()-COST_RT)*100:+.3f}%")
    print(f"  market-neutral (short-uni / long-uni removes beta):")
    print(f"    short-leg excess vs universe : {(top.mean()-uni.mean())*100:+.3f}%")
    print(f"    long-leg  excess vs universe : {(uni.mean()-bot.mean())*100:+.3f}%")
    print(f"  decile spread net = (top-bot)/2 - cost = {((top.mean()-bot.mean())/2 - COST_RT)*100:+.3f}%")

    # crude time concentration: split the day-series into quarters
    q = n // 4
    if q:
        spreads = (top - bot) / 2.0 - COST_RT
        for i in range(4):
            seg = spreads[i*q:(i+1)*q] if i < 3 else spreads[i*q:]
            print(f"  quarter {i+1}: mean decile_net {seg.mean()*100:+.3f}%  ({len(seg)} days)")


if __name__ == "__main__":
    main()
