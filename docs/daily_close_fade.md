# Daily-Close Fade

This is the active alpha lead. The current full-listing research candidate is
documented in `docs/daily_close_fade_full_listing_scam_tail_stage4_20260510.md`.

## Hypothesis

Near the UTC daily close, small/mid-cap perp gainers that are extended versus
the market, above VWAP, and supported by late volume often mean-revert. The
system shorts those names and exits mechanically.

## Current Implementation Contract

```text
Config: configs/daily_close_fade.lowcap_scam_tail_stage4_selected.yaml
Signal time: 23:00 UTC
Ranking: day_return using data available at 23:00
Ranking bars: completed 1m bars strictly before 23:00, because Bybit klines are
  timestamped by candle `startTime`
Candidate side: short only
Candidate filter: pump-like only
Excluded alpha coins: current top-cap/category-leader majors and meme leaders,
  listed explicitly in the selected config
Age filter: listed >= 10 days
Liquidity bucket: prior 7d baseline quote-turnover ranks 226+
Per-coin quality gate: none beyond pump-like, age, PIT archive membership, and
  liquidity tail
Required context: funding is modeled; OI, premium, and signed-flow columns are
  attached when available but are not hard eligibility gates for the low-cap
  tail because OI coverage is incomplete there
Top N: 1
Sizing: score_capped, score_weight_power=1.0, no fixed max_position_weight
Capacity: cap notional at 0.05% of signal-time day turnover and 0.10% of
  prior baseline turnover
Impact: charge participation-based market impact in addition to fixed
  round-trip fee/slippage bps
Entry: equal 1m TWAP over [23:00, 00:00)
Entry price: average of all filled 1m opens
Max hold: 360m after TWAP completes
Hard stop: 8% above average entry
Hard stop active: immediately from first fill
Take profit: 10%
Adaptive protection active: final add + 120m
Time-decay take profit:
  start at 10% when adaptive protection activates
  linearly decay to 4% over 120m
  exit whole symbol when the bar low touches the current decayed target
Adaptive protection:
  vol_trailing_stop_mult = 0.0
  mfe_giveback_pct = 0.0
Adaptive protection state: starts at activation time; it must not inherit
  pre-activation lows or MFE from the TWAP/delay window
TWAP stop-adding guard: disabled in the current implementation
No VWAP-reversion TP
Exit: flatten whole symbol
Re-entry: none in the same symbol/date
```

Important: ranking at 23:00 and pretending to fill before 23:00 is lookahead.
Including the 23:00 1m bar in a 23:00 decision is also lookahead because that
bar has just opened. The implemented backtest ranks at 23:00 using completed
bars through 22:59 only.

## Backtest Semantics

For Bybit USDT linear perps, short return is:

```text
(entry_price - exit_price) / entry_price
```

The TWAP model:

- uses 60 equal slices from 22:00 through 22:59;
- records `entry_fill_count`, `entry_fill_fraction`,
  `entry_complete_time`, `stop_active_time`, and
  `profit_protection_active_time`;
- scales actual weight down if the disaster stop fires before all slices fill;
- uses the current average entry for the disaster stop during accumulation;
- allows the hard stop to trigger from the first fill;
- activates all profit exits only after final add plus
  `profit_protection_delay_minutes`.
- can use a causal time-decay take-profit: the configured fixed TP starts as
  the target, then decays after profit protection activation toward a floor.
  This is checked after the fixed TP and before max-hold, so same-bar fixed TP
  still receives the better fill and stop-vs-profit ambiguity remains
  conservative.
- starts trailing/giveback state at `profit_protection_active_time`; pre-active
  MFE remains a diagnostic but must not arm a profit exit.
- can optionally stop adding future TWAP slices after an adverse move, but that
  is research-only until it survives out-of-sample validation.

Same-bar ambiguity remains conservative: if a stop and a profit exit are both
eligible inside one 1m OHLC bar, the protective stop wins.

## Context Features

The current feature builder adds optional causal context when the datasets are
present:

```text
funding: latest funding rate known at signal time
open_interest: latest OI plus 1h/24h OI changes known at signal time
premium: premium-index kline close, or mark/index price basis if both are
  available, lagged to the latest completed hourly candle known at signal time
signed_flow_1h: prior completed hour buy/sell imbalance and trade count
market context: median day return and positive-rate style market momentum
```

These feed diagnostic columns:

```text
organic_move_score
manipulation_risk_score
squeeze_risk_score
context_fade_score
```

If these datasets are missing, the columns are still present but the backtest
validity report blocks promotion because funding/carry, OI, tape/trade-count,
and premium coverage are not proof-grade.

## Backtest Validity

Every daily-close report now writes a `backtest_validity` section with:

```text
required run label
promotion allowed true/false
rule gate pass/fail table
config hash
data-root fingerprint
decision/order/fill/exit/state lifecycle
```

Current-universe runs are labelled `biased_benchmark`. Runs without PIT
membership, funding/carry, capacity caps, market impact, split stability, and
forward TWAP reconciliation cannot support promotion, no matter how good the
headline Sharpe looks.

## Current Candidate

The 2026-05-10 stage-4 full-listing scam-tail sweep is the current selected
research candidate:

```text
Search report: data/volume_alpha/reports/daily_close_fade_sharpe_target_full_listing_scam_tail_stage4_time_decay_tp/
Exact run: data/volume_alpha/reports/daily_close_fade_full_listing_scam_tail_stage4_time_decay_selected/
Config: configs/daily_close_fade.lowcap_scam_tail_stage4_selected.yaml
Window: 2025-05-08 to 2026-05-08
Total return: +11.48%
Sharpe-like: 5.51
Max drawdown: -3.17%
Trades: 74
Baskets: 74
Min chronological split: +2.10%
Post-TWAP <=16m exits: 2 / 74
Exit mix: time_decay_take_profit 37, max_hold 22, stop_loss 12, take_profit 3
Audit label: candidate
Can support promotion: false until forward/demo TWAP reconciliation passes
```

This is a research candidate, not a production approval. The selected row was
chosen over the absolute highest-return row because it had a stronger Sharpe,
profit factor, and split floor while still increasing return and reducing
max-hold exits versus stage 3. The search also found wider top-N variants for
research trade count, but those had lower Sharpe, weaker split margins, and
higher time-stop rates. The next required evidence is paper-forward/demo
reconciliation, not another hidden
backtest tweak on the same validation year.

## Profit-Protection Audit Update

This is the current honest read after the 2026-05-08 profit-protection audit.
The previous headline benchmark is not promotable because adaptive exits were
warm-started from pre-activation lows.

Actual legacy artifacts showed the problem:

```text
Legacy delayed-hard-stop run:    647 / 750 trades exited by post-TWAP minute 16
Legacy immediate-hard-stop run:  647 / 750 trades exited by post-TWAP minute 16
Legacy basket clustering:        342 / 435 baskets had every trade exit by minute 16
```

Corrected semantics start adaptive trailing/giveback state only once protection
is active. Re-running the same current-top-160 feature set with the default
parameters gives:

```text
Trades: 750
Baskets: 435
Total return: -4.75%
Sharpe-like: 0.22
Max drawdown: -53.15%
Worst split in constrained grid: no candidate cleared all splits
Default clustered exits after fix: 126 / 750 by post-TWAP minute 16
Default median post-TWAP hold after fix: 27m
```

A constrained 216-variant corrected-protection grid over hold length,
protection delay, vol-trail width, and MFE giveback settings found zero
variants positive in all train/validation/OOS splits. The best split-ranked
variant was MFE-only (`hold=180m`, `profit_delay=15m`,
`mfe_activation=1%`, `giveback=20%`, `vol_trail=0`) with +39.12% total return,
but it lost -22.87% in the 2023-2024 train split and had -40.79% max drawdown.
That is not a promotion candidate.

Artifacts:

```text
data/research_reports/profit_protection_recheck_20260508/
```

Detailed write-up:

```text
docs/profit_protection_audit_20260508.md
```

## Legacy Current-Universe Benchmark

This is a benchmark only, not proof.

```text
Dataset: current top-160 Bybit symbols
Range: 2023-05-15 to 2026-05-02
Trades: 750
Baskets: 435
Total return: +16,991.58%
Sharpe-like: 10.65
Max drawdown: -15.39%
Worst day: -12.84%
Win rate: 82.27%
Profit factor: 6.08
```

Artifacts:

```text
data/research_reports/daily_close_twap_2200_2300_current_top160_20260504/
```

Honest read: this number is mechanically large but invalid as alpha evidence.
It is current-universe biased, has a large worst day, and used the legacy
warm-started adaptive protection behavior that caused most positions to exit at
minute 16. Do not call this real-money proven, and do not use it as a promoted
benchmark.

## Entry-Risk Audit

The original 15-minute disaster-stop delay left the first TWAP slices naked.
That is not acceptable for a short-only pump-fade system. The immediate-stop
change itself was directionally right, but the audit below used the legacy
warm-started adaptive protection behavior and is no longer a promotion basis.

Latest 3-year current-top-160 audit:

```text
Base delayed hard stop:        +16,896.41%, Sharpe 10.63, max DD -15.39%
Immediate hard stop:           +16,991.58%, Sharpe 10.65, max DD -15.39%
Immediate + 5% stop-adding:     +7,180.79%, Sharpe 16.11, max DD  -2.92%
```

The 5% stop-adding guard cut drawdown in this biased sample, but because this
audit used legacy adaptive-exit semantics it must be retested before it can be
treated as a research candidate.

Artifacts:

```text
data/research_reports/daily_close_twap_2200_2300_current_top160_20260504/entry_risk_audit/
```

## Forensic Rerun

This reruns the corrected implementation on the old current-top-160 dataset.
Use it for comparison against the failed legacy artifacts, not for promotion.

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
5. Confirm corrected non-warm-start profit protection survives those splits.
6. Include funding/carry, capacity caps, and market impact in the ledger.
7. Implement forward/demo TWAP slicing and audit fill quality.

## Forward/Demo Boundary

Forward/demo currently refuses to fake TWAP as one synthetic fill. That is deliberate.
The next engineering task is explicit slice-level paper/demo execution:

```text
22:00 start slices
update average entry as slices fill
monitor disaster stop from first fill
stop adding and flatten if disaster stop hits
enable adaptive protection from 23:15
flatten whole symbol on exit
no same-symbol re-entry that day
```
