# Continuous V2 C-Book Flow Overlay + Screen Construction (Pre-Registration)

Date: 2026-06-19

Parent plan: `docs/preregistration/2026-06-18-continuous-v2-ab-research-plan.md`
Amendment authorizing the branch:
`docs/preregistration/2026-06-19-continuous-v2-ab-amendment-binance-only-flow.md`
Data receipt:
`docs/preregistration/2026-06-19-continuous-v2-data-topup-flow-blockers.md`

Scope: CONTINUOUS demo/paper research only. `claimed_venue_scope=binance_only_flow_exploratory`.
Run label `exploratory`; **no Tier-2 candidate pass is possible** for any arm here.
No real-money claim.

## Purpose

Pre-register, before running, the exact causal construction of the Binance-only
Problem Book C flow features and overlay arms now wired into
`scripts/continuous_v2_ab_research_runner.py`. This fixes the parameters so the
runs cannot be retro-fitted after seeing results.

## Feature construction (value-built in the feature almanac)

All features are attached to the per-component entry-candidate tape at
`decision_ts` (component signal bar close + 1h), causal.

- `market_flow` (already value-built, admissible on Binance): turnover/− cross-sectional
  mean of hourly taker imbalance from `binance_usdm_metrics_5m`, asof-joined at
  `available_ts = hour_close + 1h`.
- `idiosyncratic_flow` (already value-built): symbol `taker_imbalance_1h` minus
  `market_flow`.
- `flow_squeeze` (NEW, value-built Binance-only): per-symbol mean of **expanding
  prior** z-scores (strictly prior finite rows, min 10 obs) of
  `{oi_change_24h, funding_level, taker_imbalance_24h}` — OI build-up + positive
  funding + aggressive taker buy. Gated on a finite `taker_imbalance_24h` at the
  row, so a venue without value-built taker flow (Bybit) produces NULL, never a
  funding-only pseudo-squeeze.
- `flow_resid_return` (NEW, value-built Binance-only): 24h taker imbalance
  residualized by a **causal expanding per-symbol OLS** (slope/intercept from
  strictly prior rows, min 20 pairs) against `path_max_ret168` — the recent
  run-up the D9 fade targets. This is a pragmatic translation of the order-flow
  paper's lagged-return reversal control to this short-fade lifecycle: it asks
  whether residual taker buying beyond what the recent pump explains predicts the
  short's outcome. Honesty note: this is residualized against the run-up feature,
  not a same-horizon 24h-return residual (the tape has no clean prior-24h return
  column); it is a screen feature, not a same-horizon econometric residual.

Causality is unit-tested (`_expanding_resid_series`: future y cannot move an
earlier residual; warm-up returns None so coverage is honest).

## C0 — discovery screen (run first)

`C0_ORDERFLOW_SCREEN_BINANCE_ONLY` is a screen, not a lifecycle A/B. Run:

```
--mode screen --venues binance --almanac-root <flow almanac> --ab-root <control A/B>
```

It reports, per flow feature, rank-IC vs the control short net_return and the
daily basket return, the within-symbol (demeaned) rank-IC, top-minus-bottom
spread, the tail target, and `null_max_abs_rank_ic` over
{symbol_hash, calendar_hash, shuffled_within_symbol, shuffled_within_day}.

C0 pass condition (to justify spending a serious overlay arm): a flow feature
(preferably `flow_resid_return` or `idiosyncratic_flow`) shows a stable,
economically-signed within-symbol rank-IC that clearly exceeds the null-max, and
the sign is consistent across the time split and liquidity buckets. If the
within-symbol IC is at or below the null-max, or only the cross-symbol IC is
nonzero, C0 fails and the C-flow branch is closed with a negative receipt.

## C2 / C3 — hedge-intensity overlays (run only if C0 passes)

Both reuse the same-run `V2_CONTROL` component ledgers and only multiply the
frozen BTC-vol hedge intensity by a causal mean-1 daily score (entries
unchanged). Construction (identical to the A4B overlay machinery):

- Daily feature aggregate = mean of the feature over that day's entry candidates.
- Score = mean of **expanding-prior** z-scores of the daily aggregate(s),
  lagged one day (`intensity_day_ts = day_ts + 1d`).
- Extra multiplier = `clip(1 + 0.15 * score, 0.70, 1.30)`, renormalized to mean-1.
- Final hedge intensity = base BTC-vol intensity × extra.

Arms:

- `C2_MARKET_FLOW_HEDGE_INTENSITY_BINANCE_ONLY`: feature `market_flow`
  (more hedge when market-wide taker buying signals squeeze risk for the shorts).
- `C3_FLOW_SQUEEZE_HEDGE_INTENSITY_BINANCE_ONLY`: feature `flow_squeeze`
  (more hedge when the active book's aggregate squeeze score is high).
- `C7_FLOW_HASH_CONTROL_BINANCE_ONLY`: C2's `market_flow` distribution,
  calendar-hash permuted by day (the negative control).

Preferred first overlay is C2 (market-wide flow is a clean single daily series).

## Falsifiers (any one closes the exact mechanism)

- The real flow score is matched or beaten by the C7 hash control on the
  Binance MAR delta.
- Binance MAR delta ≤ 0, or drawdown worsens faster than return improves.
- The result is carried by one month, one liquidity bucket, or one component.
- Residualized flow is not incremental after lagged returns/composite (C0).
- The mechanism cannot be expressed causally / is data-coverage gated.

## Evidence limits

Every C-arm report stamps `claimed_venue_scope=binance_only_flow_exploratory`,
run label `exploratory`, and a forced robustness verdict
`EXPLORATORY single-venue flow (no Tier-2 candidate pass)`. A positive result can
only justify a Bybit full-market taker-flow archive build or a later venue-policy
amendment — never Bybit demo/paper wiring, cross-venue alpha, or real-money
evidence.
