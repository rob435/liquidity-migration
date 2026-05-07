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
Liquidity bucket: prior 7d baseline quote-turnover ranks 31+; top 30 excluded
Per-coin quality gate:
  coin_excess_vs_market >= 0.08
  vwap_extension >= 0.035
  late_volume_ratio >= 1.0
Top N: 5
Sizing: score_capped, score_weight_power=1.0, max_position_weight=0.80
Entry: 60 equal 1m TWAP slices from 22:01 through 23:00
Entry price: average of all filled 1m opens
Max hold: 180m after TWAP completes
Hard stop: 20% above average entry
Hard stop active: immediately from first fill
Adaptive protection active: final scheduled add + 15m
Adaptive protection:
  vol_trailing_stop_mult = 0.25
  mfe_giveback_activation_pct = 0.01
  mfe_giveback_pct = 0.20
TWAP stop-adding guard: disabled in the promoted contract
No fixed TP
No VWAP-reversion TP
Exit: flatten whole symbol
Re-entry: none in the same symbol/date
```

Important: ranking at 23:00 and pretending to fill from 22:00 is lookahead. The
implemented backtest ranks at 22:00 and never fills at the same timestamp used
for ranking. Because Bybit/archive 1m bars are stored with minute-open
timestamps, the 22:00 signal uses only bars whose end time is no later than
22:00; for the active 22:00 signal, the last input bar has open timestamp 21:59.

Forward/demo must preserve the same timing contract. The 22:00 signal scan is a
separate job; the demo order sync uses cached 22:00 candidates and opens paper
trades only at 22:01. If the signal scan is late, the correct behavior is to
miss the trade and report drift, not to start submitting later TWAP slices.

## Backtest Semantics

For Bybit USDT linear perps, short return is:

```text
(entry_price - exit_price) / entry_price
```

The TWAP model:

- uses 60 equal slices from 22:01 through 23:00;
- records `entry_fill_count`, `entry_fill_fraction`,
  `entry_complete_time`, `stop_active_time`, and
  `profit_protection_active_time`;
- writes `daily_close_fade_entry_fills` with one row per expected child slice;
- scales actual weight down if the disaster stop fires before all slices fill;
- records missing historical child bars as `missed_no_bar` instead of silently
  pretending they filled;
- uses the current average entry for the disaster stop during accumulation;
- allows the hard stop to trigger from the first fill;
- activates all profit exits only after the final scheduled add plus
  `profit_protection_delay_minutes`.
- can optionally stop adding future TWAP slices after an adverse move, but that
  is research-only until it survives out-of-sample validation.

Adaptive protection is a 1m-bar model in backtest and forward paper. The demo
runtime also has fast public-trade protection after the same activation time,
using the same vol-trailing and MFE-giveback thresholds, with exits written
through the shared demo execution ledger. Bybit has exchange-side trailing-stop
support, but using it as canonical profit protection would change execution
semantics unless the backtest is updated to model Bybit's exact trailing-stop
behavior. A native 20% full-position disaster stop is a closer candidate for
future demo parity because it maps directly to the current average-entry stop.

Same-bar ambiguity remains conservative: if a stop and a profit exit are both
eligible inside one 1m OHLC bar, the protective stop wins.

## Latest Current-Universe Benchmark

This is a biased benchmark only, not proof. It is retained for continuity and
must be rerun before comparison because the canonical signal now explicitly
uses only 1m bars whose end time is no later than 22:00.

```text
Dataset: current top-160 Bybit symbols
Range: 2023-05-15 to 2026-05-02
Trades: 750
Baskets: 435
Total return: +15,522.18%
Sharpe-like: 10.51
Max drawdown: -15.23%
Worst day: -13.17%
Win rate: 81.60%
Profit factor: 5.88
```

Artifacts:

```text
data/research_reports/daily_close_twap_2200_2300_current_top160_20260504/
data/research_reports/daily_close_fade_twap_continuation_20260505/
```

Honest read: the result is extremely strong but still current-universe biased,
and the worst day is large. Do not call this real-money proven.

## Research Boundary

Old one-off loss-filter, pause/resume, tape-confirmation, archive-batch, and
readiness helper scripts have been removed from the active repo surface. The
current contract above is the canonical daily-close fade path. Any future
execution-control change needs a new named experiment and a fresh run record
under `data/research_reports/research_log/`; do not infer promotion from stale
local reports.

## Run

```bash
python -m aggression_carry \
  --data-root data/daily-close-fade-1m-3y-current-top160-20230503-20260503 \
  --config configs/volume_alpha.default.yaml \
  daily-close-fade
```

## Proof Gate

Before promotion:

1. Continue archive-PIT coverage repair: the local PIT root currently has only
   `9,222` usable close-fade rows out of `367,773` manifest rows.
2. Rebuild point-in-time universe membership so symbols absent from the local
   manifest, such as SPKUSDT-style newer contracts, are not silently excluded.
3. Use archive-derived 1m bars for all eligible symbol/date pairs.
4. Rerun the exact TWAP contract without threshold changes.
5. Check train, validation, and OOS years.
6. Implement forward/demo TWAP slicing and audit fill quality.

## Forward/Demo Boundary

Forward/demo must not fake TWAP as one synthetic fill. Paper and demo shadowing
use explicit slice-level accounting:

```text
22:00 rank/signal from bars whose end time is no later than 22:00
22:01-23:00 scheduled child slices
update average entry as slices fill
monitor disaster stop from first fill
stop adding and flatten if disaster stop hits
enable adaptive protection from 23:15
flatten whole symbol on exit
no same-symbol re-entry that day
```
