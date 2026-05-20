"""WS-5 — capacity & portfolio bounds.

Square-root market-impact model on the names the strategy actually shorts.
For an order of notional Q in a name with daily dollar volume ADV and daily
return volatility sigma:

    impact_fraction ~= C * sigma * sqrt(Q / ADV)

C is the impact coefficient (Almgren-style ~0.5-1; crypto mid-caps are thinner,
so we report C in {0.5, 1.0}). At AUM A with gross 1.0 across 5 names, each
position is ~0.2*A notional. The capacity ceiling is the AUM at which the
per-trade impact equals the per-trade gross edge (net-of-impact edge -> 0).

Usage:  python -m research.ws5_capacity <window>   (default oos_binance)
"""
from __future__ import annotations

import sys

import numpy as np
import polars as pl

from research._panel import WINDOWS
from liquidity_migration.reversion_alpha import ReversionConfig, run_reversion_backtest
from liquidity_migration.storage import read_dataset

MS_PER_DAY = 86_400_000
AUM_GRID = [250_000, 500_000, 1_000_000, 2_000_000, 5_000_000, 10_000_000]
PER_NAME_FRACTION = 0.20  # gross 1.0 / 5 active


def main() -> None:
    window = sys.argv[1] if len(sys.argv) > 1 else "oos_binance"
    root, start, end = WINDOWS[window]
    cfg = ReversionConfig(start=start, end=end, entry_delay_hours=1)
    res = run_reversion_backtest(root, cfg)
    trades = res["trades"]
    if trades.is_empty():
        print("no trades"); return

    klines = read_dataset(root, "klines_1h")
    # daily dollar volume per (symbol, day)
    daily_dv = (
        klines.with_columns((pl.col("ts_ms") // MS_PER_DAY).alias("day"))
        .group_by(["symbol", "day"]).agg(pl.col("turnover_quote").sum().alias("dv"))
    )
    # daily return vol per symbol (close-to-close)
    daily_close = (
        klines.with_columns((pl.col("ts_ms") // MS_PER_DAY).alias("day"))
        .group_by(["symbol", "day"]).agg(pl.col("close").last().alias("c"))
        .sort(["symbol", "day"])
        .with_columns((pl.col("c") / pl.col("c").shift(1).over("symbol") - 1.0).alias("ret"))
    )
    vol_by_symbol = {r["symbol"]: r["ret"]
                     for r in daily_close.group_by("symbol").agg(pl.col("ret").std().alias("ret")).iter_rows(named=True)}

    # per-trade ADV: the signal-day dollar volume of the shorted name
    tr = trades.with_columns(
        (pl.col("entry_ts_ms") // MS_PER_DAY).alias("entry_day")
    )
    adv_lookup = {(r["symbol"], r["day"]): r["dv"] for r in daily_dv.iter_rows(named=True)}
    advs, sigmas, gross = [], [], []
    for t in tr.iter_rows(named=True):
        # signal day is the entry day minus 1 (entry is ~1h into the next day)
        adv = adv_lookup.get((t["symbol"], t["entry_day"] - 1)) or adv_lookup.get((t["symbol"], t["entry_day"]))
        sig = vol_by_symbol.get(t["symbol"])
        if adv and adv > 0 and sig and sig > 0:
            advs.append(adv); sigmas.append(sig); gross.append(t["gross_return"])
    advs = np.array(advs); sigmas = np.array(sigmas); gross = np.array(gross)
    print(f"WS-5 capacity — {window}:  {len(advs)} trades with ADV+vol")
    print(f"  traded-name daily $volume: median ${np.median(advs)/1e6:.1f}M  "
          f"p25 ${np.percentile(advs,25)/1e6:.1f}M  p75 ${np.percentile(advs,75)/1e6:.1f}M")
    print(f"  daily return vol: median {np.median(sigmas):.3f}")
    print(f"  mean per-trade GROSS return: {gross.mean()*100:.3f}%  "
          f"(median {np.median(gross)*100:.3f}%)")

    gross_edge = max(gross.mean(), 0.0)
    print(f"\n  {'AUM':>10}{'pos notional':>14}{'impact@C=0.5':>14}{'impact@C=1.0':>14}")
    for aum in AUM_GRID:
        q = PER_NAME_FRACTION * aum
        # per-trade mean impact over the realised trades
        imp05 = np.mean(0.5 * sigmas * np.sqrt(q / advs))
        imp10 = np.mean(1.0 * sigmas * np.sqrt(q / advs))
        print(f"  ${aum/1e6:>8.2f}M{q/1e3:>12.0f}k{imp05*100:>13.3f}%{imp10*100:>13.3f}%")
    print(f"\n  per-trade gross edge to cover: {gross_edge*100:.3f}%")
    # capacity ceiling: AUM where mean impact (C=1) == gross edge
    # impact ~ sqrt(AUM), so AUM_cap = AUM_ref * (edge/impact_ref)^2
    ref = 1_000_000
    imp_ref = np.mean(1.0 * sigmas * np.sqrt(PER_NAME_FRACTION * ref / advs))
    if imp_ref > 0 and gross_edge > 0:
        cap = ref * (gross_edge / imp_ref) ** 2
        print(f"  capacity ceiling (C=1, impact==gross edge): ~${cap/1e6:.2f}M AUM")
    else:
        print("  capacity ceiling: N/A (gross edge <= 0 -> no capacity at any size)")


if __name__ == "__main__":
    main()
