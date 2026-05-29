"""Regime-conditioned momentum factor study (DESCRIPTIVE, EXPLORATORY).

Question (Greenig/CTA framing): does cross-sectional alt-momentum actually have
forward predictive value unconditionally, or only conditional on BTC regime
(trend state + realized-vol regime)? If alt-momentum is mostly leveraged BTC
beta in uptrends and noise otherwise, the long sleeve should *gate* momentum on
BTC regime — that's the Sharpe lever.

Method: for each in-universe coin-day, rank by trailing momentum, measure the
FORWARD 7d return of the top vs bottom momentum quintile, bucketed by BTC
regime. This is a descriptive efficacy study (forward return uses look-ahead by
construction) — NOT a backtest. It tells us WHICH regimes to trade momentum in.

Vol terciles are full-sample here (descriptive); a tradeable gate must use a
trailing percentile (noted in the conditioning code).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from liquidity_migration.momentum_signals import daily_bars  # noqa: E402
from liquidity_migration.storage import read_dataset_columns  # noqa: E402

ANN = math.sqrt(365.0)
S = Path.home() / "SHARED_DATA"
VENUES = {"bybit": S / "bybit_full_pit", "binance": S / "binance_full_pit_strategy"}
UNIVERSE_N = 30
MOM_DAYS = 30
FWD_DAYS = 7


def study(label: str, root: Path) -> None:
    kl = read_dataset_columns(root, "klines_1h",
                              columns=["ts_ms", "symbol", "date", "open", "high", "low", "close", "turnover_quote"])
    if kl.is_empty():
        print(f"\n### {label}: empty klines")
        return
    d = daily_bars(kl).sort(["symbol", "ts_ms"])
    d = d.with_columns([
        (pl.col("close") / pl.col("close").shift(1).over("symbol")).log().alias("lr"),
        (pl.col("close") / pl.col("close").shift(MOM_DAYS).over("symbol") - 1.0).alias("mom"),
        (pl.col("close").shift(-FWD_DAYS).over("symbol") / pl.col("close") - 1.0).alias("fwd"),
        pl.col("turnover_quote").rolling_median(90, min_samples=60).over("symbol").alias("tov90"),
    ])
    d = d.with_columns(pl.col("tov90").rank(method="ordinal", descending=True).over("ts_ms").alias("urank"))

    # BTC regime, joined by date
    btc = d.filter(pl.col("symbol") == "BTCUSDT").sort("ts_ms").with_columns([
        pl.col("close").rolling_mean(200, min_samples=100).alias("sma200"),
        (pl.col("lr").rolling_std(30, min_samples=20) * ANN).alias("rv30"),
        (pl.col("lr").rolling_std(7, min_samples=5) * ANN).alias("rv7"),
        (pl.col("lr").rolling_std(90, min_samples=60) * ANN).alias("rv90"),
    ])
    btc = btc.with_columns([
        (pl.col("close") > pl.col("sma200")).alias("btc_up"),
        (pl.col("rv7") / pl.col("rv90")).alias("vol_burst"),
    ])
    lo, hi = btc["rv30"].quantile(0.33), btc["rv30"].quantile(0.66)
    btc = btc.with_columns(
        pl.when(pl.col("rv30") <= lo).then(pl.lit("lo"))
          .when(pl.col("rv30") <= hi).then(pl.lit("mid"))
          .otherwise(pl.lit("hi")).alias("vol_reg")
    ).select(["ts_ms", "btc_up", "vol_reg", "vol_burst", "rv30"])

    uni = (
        d.filter((pl.col("urank") <= UNIVERSE_N) & pl.col("mom").is_not_null() & pl.col("fwd").is_not_null() & (pl.col("symbol") != "BTCUSDT"))
        .join(btc, on="ts_ms", how="inner")
    )
    # momentum quintile within each date
    uni = uni.with_columns((pl.col("mom").rank(method="average").over("ts_ms") / pl.len().over("ts_ms")).alias("mpct"))
    top = uni.filter(pl.col("mpct") >= 0.8)
    bot = uni.filter(pl.col("mpct") <= 0.2)

    def stat(df):
        if df.is_empty():
            return (0, float("nan"), float("nan"))
        r = df["fwd"]
        sh = r.mean() / r.std() * math.sqrt(365 / FWD_DAYS) if r.std() and r.std() > 0 else float("nan")
        return (df.height, r.mean(), sh)

    print(f"\n################ {label}  (top-{UNIVERSE_N} universe, mom={MOM_DAYS}d, fwd={FWD_DAYS}d) ################")
    print("Long-only momentum = forward return of TOP-momentum quintile. Spread = top - bottom quintile.")
    print(f"{'regime':28s} {'n_top':>7} {'top_fwd%':>9} {'top_Sh':>7} {'spread%':>8}")
    # by BTC trend
    for up in [True, False]:
        nt, mt, st = stat(top.filter(pl.col("btc_up") == up))
        nb, mb, sb = stat(bot.filter(pl.col("btc_up") == up))
        print(f"{'BTC_up=' + str(up):28s} {nt:7d} {mt*100:9.2f} {st:7.2f} {(mt-mb)*100:8.2f}")
    # by vol regime
    for vr in ["lo", "mid", "hi"]:
        nt, mt, st = stat(top.filter(pl.col("vol_reg") == vr))
        nb, mb, sb = stat(bot.filter(pl.col("vol_reg") == vr))
        print(f"{'BTC_vol=' + vr:28s} {nt:7d} {mt*100:9.2f} {st:7.2f} {(mt-mb)*100:8.2f}")
    # combined trend x vol
    for up in [True, False]:
        for vr in ["lo", "mid", "hi"]:
            nt, mt, st = stat(top.filter((pl.col("btc_up") == up) & (pl.col("vol_reg") == vr)))
            nb, mb, sb = stat(bot.filter((pl.col("btc_up") == up) & (pl.col("vol_reg") == vr)))
            print(f"{'up=' + str(up)[0] + ' vol=' + vr:28s} {nt:7d} {mt*100:9.2f} {st:7.2f} {(mt-mb)*100:8.2f}")
    # vol-burst (Minsky de-risk): high burst = reversal risk
    bq = btc["vol_burst"].quantile(0.8)
    for tag, cond in [("vol_burst<=P80", pl.col("vol_burst") <= bq), ("vol_burst>P80", pl.col("vol_burst") > bq)]:
        nt, mt, st = stat(top.filter(cond))
        print(f"{tag:28s} {nt:7d} {mt*100:9.2f} {st:7.2f} {'':>8}")


def main() -> int:
    for label, root in VENUES.items():
        study(label, root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
