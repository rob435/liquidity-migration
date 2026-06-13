# Stage 4 - Sniper Alpha

## Current Fact Pattern

W4 Stage 2 found the fixed live sniper form historically supportive for forward
watch only:

- level: `entry * 1.08`;
- size: quarter of base notional;
- attached 25% stop;
- exit with base lifecycle;
- historical fill rate: 37.2% Bybit, 33.4% Binance;
- R1 pooled MAR delta: `+0.14`;
- Binance bootstrap MAR weak;
- live fills still zero.

## Question

Does sniper have standalone execution alpha, and can it be trusted
operationally?

## Arms

- `S0_no_sniper`: frozen control.
- `S1_fixed_x8_b25`: current live form.
- `S2_fixed_x8_b25_depth_validated`: same form, but constrained by depth and
  forward fill feasibility.
- `S3_score_conditioned_enable`: enable sniper only in predeclared Stage 1
  score buckets.
- `S4_score_conditioned_size`: vary sniper size by locked score bucket while
  base size stays unchanged.
- `S5_negative_control_sniper`: hash-bucket enable/size.

No new wick search. No adaptive fitting on the spent window.

## Required Rows

- base entry row;
- sniper order row;
- touch/no-touch;
- touch/no-fill if forward/depth exists;
- fill timestamp and price;
- stop-loss activation;
- cancel/expiry row;
- base-exit row;
- add-on PnL row;
- demo reconciliation row when forward fills exist.

## Metrics

- eligible order count;
- touch rate;
- fill-valid rate;
- stop-hit rate;
- average filled-add-on bps;
- add-on return contribution;
- stop-loss return loss vs non-stop profit;
- portfolio MAR delta;
- R1 robustness;
- forward realized fill/slippage once available.

## Pass Bar

Sniper may remain or become a demo-watch module only if:

- filled-add-on bps positive both venues;
- stop-hit losses do not exceed non-stop profits;
- pooled MAR delta positive;
- no venue MAR delta `< -0.5`;
- forward fills eventually clear minimum sample size;
- depth/capacity does not invalidate the fills.

## Falsifier

Reject this sniper mechanism if historical alpha disappears under conservative
fill assumptions, or if forward fills show adverse selection not present in the
historical bar model.
