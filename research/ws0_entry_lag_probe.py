"""WS-0 diagnostic — what entry lag does reversion_alpha.simulate actually use?

My IC tool reproduced the report's signal *ordering* but every component IC
came out systematically lower. The likeliest buried assumption is the gap
between the signal close and the modelled entry. This traces it from raw bars.
"""
from __future__ import annotations

import polars as pl

from liquidity_migration.reversion_alpha import (
    ReversionConfig, compute_reversion_score,
)
from liquidity_migration.volume_events import _enriched_event_features
from liquidity_migration.volume_features import build_volume_features
from liquidity_migration.storage import read_dataset

MS_PER_HOUR = 3_600_000

root = "~/SHARED_DATA/binance_oos_pit"
klines = read_dataset(root, "klines_1h")

# 1) hourly bar timestamp convention for one symbol
btc = klines.filter(pl.col("symbol") == "BTCUSDT").sort("ts_ms")
ts = btc["ts_ms"].to_list()[:6]
print("=== hourly ts (first 6 BTCUSDT bars) ===")
for t in ts:
    print(f"  {t}  {pl.from_epoch(pl.Series([t]), time_unit='ms').dt.strftime('%Y-%m-%d %H:%M')[0]}")
print(f"  spacing = {(ts[1]-ts[0])/MS_PER_HOUR:.0f}h")

# 2) build the panel and pick one (symbol,date) row
cfg = ReversionConfig(start="2021-01-01", end="2021-02-01")
manifest = read_dataset(root, "archive_trade_manifest")
features = _enriched_event_features(build_volume_features(klines), klines, manifest,
                                    funding=pl.DataFrame(), open_interest=pl.DataFrame(),
                                    signed_flow_1h=pl.DataFrame(), mark_price_1h=pl.DataFrame(),
                                    index_price_1h=pl.DataFrame(), premium_index_1h=pl.DataFrame())
features = features.filter((pl.col("date") >= "2021-01-01") & (pl.col("date") < "2021-02-01"))
scored = compute_reversion_score(features, cfg)
row = scored.filter(pl.col("symbol") == "BTCUSDT").sort("date").to_dicts()[5]
date = row["date"]
print(f"\n=== panel row: symbol=BTCUSDT date={date} ===")
print(f"  panel ts_ms = {row['ts_ms']}  -> {pl.from_epoch(pl.Series([row['ts_ms']]), time_unit='ms').dt.strftime('%Y-%m-%d %H:%M')[0]}")
print(f"  daily_close = {row['daily_close']}   daily_return_1d = {row['daily_return_1d']:.6f}")

# 3) which hourly bar has close == daily_close -> when the signal trading day ENDED
match = btc.filter((pl.col("close") - row["daily_close"]).abs() < 1e-6).sort("ts_ms")
if match.height:
    last = match.to_dicts()[-1]
    print(f"  hourly bar whose close == daily_close: ts={last['ts_ms']} "
          f"-> {pl.from_epoch(pl.Series([last['ts_ms']]), time_unit='ms').dt.strftime('%Y-%m-%d %H:%M')[0]}")
    signal_close_ts = last["ts_ms"]
else:
    signal_close_ts = None
    print("  (no exact close match)")

# 4) what entry_ts does simulate compute for this date?
day_ms = int(pl.Series([date]).str.to_datetime().dt.timestamp("ms")[0])
for delay in (1,):
    entry_ts = day_ms + 23 * MS_PER_HOUR + delay * MS_PER_HOUR
    print(f"\n  simulate entry_ts (delay={delay}h) = {entry_ts} "
          f"-> {pl.from_epoch(pl.Series([entry_ts]), time_unit='ms').dt.strftime('%Y-%m-%d %H:%M')[0]}")
    if signal_close_ts is not None:
        lag_h = (entry_ts - signal_close_ts) / MS_PER_HOUR
        print(f"  >>> effective lag from signal close to entry = {lag_h:.0f} hours")
    entry_bar = btc.filter(pl.col("ts_ms") == entry_ts)
    print(f"  entry bar exists in klines: {entry_bar.height == 1}"
          + (f"  open={entry_bar['open'][0]}" if entry_bar.height == 1 else ""))
