# Full PIT Bybit Scam-Tail Stage 4

Date: 2026-05-10

## Status

This is the current selected research candidate, not a live-trading approval.
Stage 4 adds a causal time-decay take-profit to reduce max-hold exits without
reintroducing the broken profit-protection behavior that exited immediately
after activation. Promotion remains blocked by the forward/demo TWAP
reconciliation gate.

## Data Standard

- Window: 2025-05-08 through 2026-05-08 exclusive.
- Universe: full Bybit USDT perp public-trading archive manifest, no symbols
  file.
- Manifest rows: 122,911 symbol-days.
- Processed rows: 122,114 symbol-days, 99.35%.
- Sparse listing rows are audited but not tradable unless row-level bar
  coverage is present.
- Funding is modeled. Open interest, premium, and signed-flow context are
  attached when available but are not hard gates for this low-cap tail because
  OI coverage is incomplete.

## New Exit Feature

The new feature is `time_decay_take_profit`.

For short trades:

- fixed take-profit remains the first profit target;
- after `profit_protection_delay_minutes`, the target starts at
  `take_profit_pct`;
- it linearly decays to `time_decay_take_profit_floor_pct` over
  `time_decay_take_profit_minutes`;
- if the 1m bar low touches the current decayed target, the whole symbol exits
  with `exit_reason=time_decay_take_profit`;
- hard stops are still checked first, so stop-vs-profit same-bar ambiguity
  remains conservative;
- fixed TP is checked before the decayed TP, so a better same-bar fixed TP fill
  is not replaced by a lower decayed target.

This is causal: it uses only current bar OHLC, configured timing, average entry,
and the profit-protection activation timestamp.

## Selected Logic

Config: `configs/daily_close_fade.lowcap_scam_tail_stage4_selected.yaml`

Rules:

- At 23:00 UTC, rank every PIT-listed Bybit USDT perp by completed same-day
  return.
- Eligible names must be pump-like, at least 10 days old, archive-tradable on
  that date, in 7-day baseline liquidity rank 226+, and outside the alpha-coin
  exclusion list.
- Short the single highest same-day return name.
- Enter using a 60-minute TWAP from 23:00 to 00:00 UTC.
- Hold up to 360 minutes after TWAP completion unless stopped or profit exits
  trigger.
- Hard stop: 8%, active from the first fill.
- Fixed take profit: 10%.
- Profit protection delay: 120 minutes after final TWAP add.
- Time-decay TP: decay from 10% to 4% over 120 minutes after protection
  activation.
- MFE giveback: disabled in the selected row.
- Capacity is capped at the smaller of 0.05% of same-day turnover or 0.10% of
  7-day baseline turnover.
- Market impact cost: 3 bps per 1% turnover participation, charged round-trip.
- Base round-trip cost: 9.6 bps.

## Result

Exact run:
`data/volume_alpha/reports/daily_close_fade_full_listing_scam_tail_stage4_time_decay_selected/daily_close_fade_report.md`

- Total return: 11.48%.
- Sharpe-like: 5.51.
- Max drawdown: -3.17%.
- Trades: 74.
- Baskets: 74.
- Win rate: 70.27%.
- Profit factor: 1.86.
- Average basket gross exposure: 8.56%.
- All 74 trades were capacity-limited.
- Funding return contribution: -1.52%.
- Market impact cost contribution: 0.037%.

Chronological splits:

| Split | Return | Sharpe-like | Max DD | Baskets |
|---|---:|---:|---:|---:|
| 1 | 2.10% | 2.98 | -2.34% | 25 |
| 2 | 5.33% | 6.40 | -1.32% | 25 |
| 3 | 3.67% | 8.43 | -0.70% | 24 |

Exit reasons:

| Exit | Trades |
|---|---:|
| Time-decay take profit | 37 |
| Max hold | 22 |
| Stop loss | 12 |
| Fixed take profit | 3 |

The synchronized-exit issue remains absent:

- Exits within 16 minutes after final TWAP add: 2 of 74.
- Exits within 30 minutes after final TWAP add: 5 of 74.
- Median post-TWAP hold: 241.5 minutes.
- P90 post-TWAP hold: 360 minutes.

## Stage 3 Comparison

Stage 3 selected row:

- Return: 10.15%.
- Sharpe-like: 4.87.
- Max drawdown: -3.03%.
- Max-hold exits: 38 of 74.
- Profit factor: 1.70.
- Median post-TWAP hold: 360 minutes.

Stage 4 selected row:

- Return: 11.48%.
- Sharpe-like: 5.51.
- Max drawdown: -3.17%.
- Max-hold exits: 22 of 74.
- Profit factor: 1.86.
- Median post-TWAP hold: 241.5 minutes.

The new feature improves the requested objective: higher net return and fewer
max-hold exits on the same PIT window and universe.

The absolute top-return stage-4 row returned 11.63% with 22 max-hold exits, but
it had a lower Sharpe, lower profit factor, and lower minimum split return than
the selected row. The selected row gives up 0.15 percentage points of return for
better risk-adjusted behavior.

## More-Trade Research Rows

The grid included wider `top_n` values to test whether the feature generalizes:

| Top N | Best Return | Sharpe-like | Trades | Baskets | Max-hold exits | Max DD |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 11.63% | 5.40 | 74 | 74 | 22 | -3.12% |
| 3 | 8.51% | 3.24 | 122 | 83 | 58 | -4.41% |
| 5 | 8.26% | 3.08 | 130 | 83 | 64 | -4.57% |
| 10 | 6.70% | 2.43 | 140 | 83 | 71 | -4.73% |

These rows are useful for diagnostics because they increase trade count, but
they are not selected. Wider baskets dilute the edge, weaken split margins, and
still hit too many max-hold exits.

## Search Artifacts

- Search config:
  `configs/daily_close_fade.lowcap_scam_tail_stage4_time_decay_tp.yaml`
- Search report:
  `data/volume_alpha/reports/daily_close_fade_sharpe_target_full_listing_scam_tail_stage4_time_decay_tp/daily_close_fade_sharpe_target.md`
- Grid CSV:
  `data/volume_alpha/reports/daily_close_fade_sharpe_target_full_listing_scam_tail_stage4_time_decay_tp/daily_close_fade_grid_results.csv`
- Selected config:
  `configs/daily_close_fade.lowcap_scam_tail_stage4_selected.yaml`
- Exact report:
  `data/volume_alpha/reports/daily_close_fade_full_listing_scam_tail_stage4_time_decay_selected/daily_close_fade_report.md`
- All trades:
  `data/volume_alpha/reports/daily_close_fade_full_listing_scam_tail_stage4_time_decay_selected/daily_close_fade_trades.csv`
- Basket ledger:
  `data/volume_alpha/reports/daily_close_fade_full_listing_scam_tail_stage4_time_decay_selected/daily_close_fade_baskets.csv`
- Equity curve CSV:
  `data/volume_alpha/reports/daily_close_fade_full_listing_scam_tail_stage4_time_decay_selected/daily_close_fade_equity.csv`
- Equity curve SVG:
  `data/volume_alpha/reports/daily_close_fade_full_listing_scam_tail_stage4_time_decay_selected/daily_close_fade_equity_curve.svg`

## Caveats

- This is still backtest-only. The audit says `Can support promotion: False`
  until forward/demo slice-level TWAP reconciliation exists and passes.
- The alpha exclusion list is current-market-cap informed. It is conservative
  tail isolation, not causally pure historical top-30 membership.
- The selected row was chosen on the same one-year research window. It must be
  treated as a research candidate, not as evidence of production robustness.
- The edge remains primarily price-trend/top-gainer based. OI is informative
  when present, but incomplete OI coverage in the low-cap tail prevents using
  it as a hard gate in the selected config.
