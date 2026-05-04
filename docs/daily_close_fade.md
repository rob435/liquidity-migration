# Daily-Close Fade

This is the active alpha lead.

## Hypothesis

Near the UTC daily close, small/mid-cap perp gainers that are extended versus
the market, above VWAP, and supported by late volume often mean-revert. The
system shorts those names and exits mechanically.

## Current Research Contract

```text
Signal time: 22:00 UTC
Ranking: day-to-date vol-adjusted return using data available at 22:00
Candidate side: short only
Candidate filter: pump-like only
Excluded majors: BTC, ETH, SOL, BNB
Age filter: listed >= 10 days
Liquidity bucket: prior 7d baseline quote-turnover ranks 31-150
Per-coin quality gate:
  coin_excess_vs_market >= 0.08
  vwap_extension >= 0.035
  late_volume_ratio >= 1.0
Top N: 5
Sizing: score_capped, score_weight_power=1.0, max_position_weight=0.80
Entry: equal 1m TWAP over [22:00, 23:00)
Entry price: average of all filled 1m opens
Max hold: 180m after TWAP completes
Hard stop: 20% above average entry
Hard stop active: first fill + 15m
Adaptive protection active: final add + 15m
Adaptive protection:
  vol_trailing_stop_mult = 0.25
  mfe_giveback_activation_pct = 0.01
  mfe_giveback_pct = 0.20
No fixed TP
No VWAP-reversion TP
Exit: flatten whole symbol
Re-entry: none in the same symbol/date
```

Important: ranking at 23:00 and pretending to fill from 22:00 is lookahead. The
implemented backtest ranks at 22:00.

## Backtest Semantics

For Bybit USDT linear perps, short return is:

```text
(entry_price - exit_price) / entry_price
```

The TWAP model:

- uses 60 equal slices from 22:00 through 22:59;
- records `entry_fill_count`, `entry_fill_fraction`,
  `entry_complete_time`, and `profit_protection_active_time`;
- scales actual weight down if the disaster stop fires before all slices fill;
- uses the current average entry for the disaster stop during accumulation;
- activates all profit exits only after final add plus `stop_delay_minutes`.

Same-bar ambiguity remains conservative: if a stop and a profit exit are both
eligible inside one 1m OHLC bar, the protective stop wins.

## Latest Current-Universe Benchmark

This is a benchmark only, not proof.

```text
Dataset: current top-160 Bybit symbols
Range: 2023-05-15 to 2026-05-02
Trades: 750
Baskets: 435
Total return: +16,896.41%
Sharpe-like: 10.63
Max drawdown: -15.39%
Worst day: -12.84%
Win rate: 82.27%
Profit factor: 6.08
```

Artifacts:

```text
data/research_reports/daily_close_twap_2200_2300_current_top160_20260504/
```

Honest read: the result is extremely strong but still current-universe biased,
and the worst day is large. Do not call this real-money proven.

## Run

```bash
python -m aggression_carry \
  --data-root data/daily-close-fade-1m-3y-current-top160-20230503-20260503 \
  --config configs/volume_alpha.default.yaml \
  daily-close-fade
```

## Proof Gate

Before promotion:

1. Build point-in-time universe membership from Bybit public archives.
2. Use archive-derived 1m bars for all eligible symbol/date pairs.
3. Rerun the exact TWAP contract without threshold changes.
4. Check train, validation, and OOS years.
5. Implement forward/demo TWAP slicing and audit fill quality.

## Forward/Demo Boundary

Forward/demo currently refuses to fake TWAP as one synthetic fill. That is deliberate.
The next engineering task is explicit slice-level paper/demo execution:

```text
22:00 start slices
update average entry as slices fill
monitor disaster stop from first fill + 15m
stop adding and flatten if disaster stop hits
enable adaptive protection from 23:15
flatten whole symbol on exit
no same-symbol re-entry that day
```
