# Stage 6 - Interaction Matrix And Forward Gate

## Purpose

Individual modules can double-count the same edge. Stage 6 tests marginal value
before anything becomes a demo/paper candidate.

## Interaction Arms

Run only modules that passed their own stage:

- `I0_control`;
- `I1_best_score_entry_only`;
- `I2_best_entry_only`;
- `I3_best_exit_only`;
- `I4_best_sniper_only`;
- `I5_best_sizing_only`;
- `I6_score_plus_entry`;
- `I7_score_plus_sniper`;
- `I8_score_plus_sizing`;
- `I9_full_combined_candidate`;
- `I10_negative_control_combined`.

## Marginal Contribution Requirement

Each retained module must show positive marginal value when added to the best
already-retained stack. If a module works alone but contributes nothing after
score-entry is active, it is redundant and should be dropped.

## Pass Bar

The combined candidate must:

- beat all single-module arms on pooled MAR;
- keep return positive both venues;
- have no venue MAR delta `< -0.5` versus control;
- have no worse drawdown class;
- survive 2x costs where applicable;
- not rely on one month, one component, one venue, or one symbol bucket;
- beat the negative-control combined arm.

## Forward Gate

Historical Stage 6 can nominate a demo/paper shadow only. Forward proof requires:

- at least 30 forward days;
- overlap replay no drift;
- enough entries per book, target `>=100`;
- enough sniper fills before judging sniper;
- daily demo/paper reconciliation;
- execution lifecycle matches the backtest lifecycle;
- no Telegram approval path;
- no real-money toggle.

If the forward sample is zero or tiny, the verdict is "not enough forward
evidence", not "pass" or "fail".

## Stop Conditions

Stop and do not continue the combined program if:

- Stage 0 candidate tape fails;
- a module cannot produce reconstructable ledgers;
- full-PIT pass fails;
- forward replay drift appears;
- any result needs threshold lowering after output;
- a module requires a data source not available on both venues.
