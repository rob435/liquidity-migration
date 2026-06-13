# Stage 2 - Entry Execution Alpha

## Question

Once a signal is accepted, is there alpha in where and how the order enters?

This stage separates execution alpha from filter alpha. Missing a trade has an
opportunity cost and must be measured.

## Control

`E0_control`: current base entry lifecycle from the frozen continuous control.

## Arms

- `E1_next_bar_market`: strict causal market entry at the next executable bar.
- `E2_adverse_touch_entry`: enter only after a predeclared adverse move against
  the fade, else fall through to market at expiry.
- `E3_passive_postonly_shadow`: fixed passive level with expiry, no maker rebate
  unless validated by forward/depth evidence.
- `E4_score_conditioned_entry_style`: choose market/touch/passive by locked
  Stage 1 score bucket, using only predeclared buckets.
- `E5_negative_control_entry_style`: choose entry style by hash bucket.

## Fill Rules

Every arm declares:

- order submit timestamp;
- limit/market price rule;
- expiry timestamp;
- whether no-fill falls through or cancels;
- fill window;
- same-bar handling;
- slippage and fee assumption.

No same-bar favorable-path credit. If the bar can both fill and take profit,
use the conservative path or mark the row ambiguous and exclude it from
candidate evidence.

## Metrics

- fill rate;
- missed-trade opportunity cost;
- adverse selection after touch;
- entry slippage bps;
- net bps per accepted signal;
- return/MAR/DD;
- R1 robustness;
- fill ambiguity count;
- capacity/depth stress where data exists.

## Pass Bar

An entry arm advances only if:

- positive return both venues;
- pooled MAR delta `> +0.1`;
- no venue MAR delta `< -0.5`;
- missed-trade opportunity cost is included;
- no-fill behavior is not the source of the edge;
- negative-control entry style is weaker.

## Falsifier

Reject as entry alpha if improvement comes from silently skipping losers rather
than improving execution on accepted signals.
