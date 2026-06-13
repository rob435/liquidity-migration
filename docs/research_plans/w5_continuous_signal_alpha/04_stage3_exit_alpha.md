# Stage 3 - Exit Lifecycle Alpha

## Question

Can exit timing improve lifecycle without repeating the failed W4 stop overlay?

W4 Stage 1 rejected the exact capped 25% disaster stop plus failed-fade and
breakeven overlay. Do not rerun that mechanism as-is.

## Required Open-Position State Tape

One row per open trade per decision timestamp:

- venue;
- component;
- trade id;
- symbol;
- entry timestamp;
- current age;
- current unrealized return;
- MFE so far;
- MAE so far;
- current causal composite/rank;
- current BTC regime;
- funding accrued;
- current crowding;
- live-realistic exit eligibility state;
- hold-vs-exit forward realized return for measurement only.

## Arms

- `X0_fixed_tp24_control`: current fixed TP/24h lifecycle.
- `X1_score_decay_exit`: exit when causal score/rank decays below a locked
  threshold.
- `X2_hold_value_exit`: walk-forward model predicts negative hold value.
- `X3_score_conditioned_time_cap`: hold duration varies by locked score bucket.
- `X4_time_decay_take_profit`: fixed TP decays by age bucket.
- `X5_negative_control_exit`: hash-state exit.

## State Integrity

Every exit arm must be warm-started from state the live system would have had
at `exit_activation_ts`. No using future MFE/MAE, future rank, or end-of-trade
labels to initialize state.

## Metrics

- return;
- MAR;
- drawdown;
- worst day;
- average hold time;
- exit reason distribution;
- avoided loss vs foregone winner;
- per-component attribution;
- monthly/third stability;
- R1 robustness.

## Pass Bar

An exit arm advances only if:

- return positive both venues;
- pooled MAR delta `> +0.1`;
- drawdown or worst-day improves without destroying return;
- no venue MAR delta `< -0.5`;
- negative-control exit is weaker;
- live warm-start state is reconstructable.

## Falsifier

Reject if the result requires post-entry future path, impossible same-bar
ordering, or state unavailable to the live executor.
