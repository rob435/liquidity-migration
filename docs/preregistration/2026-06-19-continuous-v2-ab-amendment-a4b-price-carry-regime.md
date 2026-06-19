# Amendment: A4B Price/Carry Regime Hedge-Intensity

**Date:** 2026-06-19
**Parent plan:** `docs/preregistration/2026-06-18-continuous-v2-ab-research-plan.md`
**Scope:** CONTINUOUS demo/paper research only. No real-money claim.
**Run label cap:** `exploratory` until a full lifecycle ledger, null control, robustness, and forward/demo evidence exist.

## Why This Amendment Exists

The original preferred first arm, `A4_REGIME_HEDGE_INTENSITY`, is blocked as
written. The fresh almanac under
`backtest-runs/continuous_v2_feature_almanac_2026-06-19/` shows:

- funding and premium are causal and covered on both venues;
- OI and taker-flow are not full-window, both-venue admissible;
- `flow_resid_return` and `flow_squeeze` are not value-built.

Therefore the full multifactor A4 score cannot be run honestly. This amendment
creates a narrower arm with a new id. It must not be reported as `A4`.

## New Arms

### `A4B_PRICE_CARRY_REGIME_HEDGE_INTENSITY`

Mechanism:

- Keep entries, component ledgers, inverse-vol sizing, max4 rebalance, TP/24h
  exits, adverse-entry breaker, and BTC+ETH hedge structure unchanged.
- Multiply the existing BTC-vol hedge overlay by a second causal, mean-1,
  daily price/carry squeeze-risk intensity.
- The intervention acts only through hedge intensity. It does not size entries
  up or down and it does not drop trades.

Allowed inputs:

- BTC realized-vol percentile and BTC drawdown.
- Market breadth, market dispersion, and alt-minus-BTC return.
- Aggregate funding level/change.
- Aggregate premium level/change.

Excluded inputs:

- OI.
- Taker flow.
- Residualized flow.
- Flow squeeze.
- Long/short, liquidations, and depth.

Predeclared score:

1. Build daily feature values from only information available before the hedge
   decision day:
   - BTC/market features use prior closed daily bars.
   - funding/premium use prior-day candidate-universe aggregate values.
2. Convert each feature to an expanding z-score using only prior observations.
3. Squeeze-risk score is the equal-weight mean of:
   - `btc_vol_percentile_250d`
   - `market_dispersion_1d`
   - `market_breadth_1d`
   - `alt_minus_btc_1d`
   - `funding_level`
   - `funding_change`
   - `premium_level`
   - `premium_change`
   - negative `btc_drawdown_30d` (deeper drawdown lowers long-hedge need)
4. Map score to intensity:
   `extra_intensity = clip(1 + 0.15 * score, 0.70, 1.30)`.
5. Normalize the extra-intensity series to mean 1 over the ledger days.
6. Final hedge intensity is:
   `existing_btcvol_intensity * extra_intensity`.

This map is deliberately mild. If it only works at a larger multiplier, that is
a new parameter and needs another amendment.

### `A4B_PRICE_CARRY_HASH_CONTROL`

Same control ledger and same marginal distribution of extra-intensity, but the
daily extra-intensity values are deterministically permuted by calendar hash.
This is a discovery null for the hedge-intensity intervention.

## Success And Falsifiers

Primary metric:

- Delta MAR versus `V2_CONTROL`, both venues and pooled.

Falsifiers:

- The hash control matches or beats the real score.
- The arm improves headline return while worsening max drawdown enough to lower
  MAR.
- One venue carries the entire result.
- The hedge-intensity change mostly changes a single month or sub-period.
- The result depends on OI/taker-flow fields; those are explicitly excluded.

## Required Outputs

Use `scripts/continuous_v2_ab_research_runner.py --mode ab` and write the normal
artifact set:

- per-arm/per-venue configs, hashes, trades, MTM, equity, monthly, splits,
  summaries, `run_report.md`, and checkpoint;
- pooled `ab_table.csv`, `pooled_ab_table.csv`, and `decision_rule_input.csv`;
- a receipt update with control/arm deltas and the hash-control comparison.

No promotion, deployment, or real-money claim is allowed from this amendment.
