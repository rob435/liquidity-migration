# Stage 1 - Composite Score As Entry Priority

## Question

Can score improve entry priority at constant breadth?

This is not a filter stage. It must keep the same decision opportunities and
comparable trade count. A rule that only looks better by dropping trades is not
entry-score alpha.

## Control

`A0_frozen_control`: current `continuous_ensemble_v1` selection and ledgers.

## Arms

All arms use the Stage 0 candidate tape and the same maximum active positions.

- `A1_current_score_priority`: prioritize by current composite score only.
- `A2_path_neutralized_priority`: prioritize by residualized `pre_24h_return`,
  neutralized by venue, component, symbol bucket, and month.
- `A3_blended_priority`: fixed walk-forward blend of current composite rank and
  neutralized `pre_24h_return`.
- `A4_vol_path_priority`: fixed walk-forward blend of current rank plus
  neutralized `pre_24h_realized_vol`.
- `A5_negative_control_priority`: symbol hash / month hash score.

No arm may change the number of allowed entries after seeing results.

## Neutralization

Residualization must be fit only inside the relevant training fold:

- regress candidate feature against symbol bucket, component, and month;
- compute residual score;
- freeze transformation for validation/test fold;
- do not train on future candidate outcomes.

## Metrics

Per arm and venue:

- selected trade count;
- same-cycle replacement count vs control;
- average score by selected bucket;
- return;
- MAR;
- max drawdown;
- worst day;
- bps per trade;
- rank IC;
- top-vs-bottom candidate return spread;
- R1 pooled MAR delta;
- bootstrap and leave-one-month-out fragility;
- component attribution.

## Required Ledgers

Write R1-compatible directories:

`~/SHARED_DATA/{venue}_full_pit/reports/w5_continuous_stage1_score_entry_YYYY-MM-DD/{arm}/`

Each arm writes:

- `ensemble_hedged_ledger.csv`;
- `volume_event_best_monthly.csv`;
- `volume_event_research_report.json`;
- selected-entry CSV;
- replacement audit CSV.

## Pass Bar

An arm can advance to Stage 6 interaction testing only if:

- return positive both venues;
- pooled MAR delta `> +0.1`;
- neither venue MAR delta `< -0.5`;
- negative control materially weaker;
- trade count is not reduced by more than the preregistered tolerance;
- at least two chronological thirds have the same pooled direction.

## Falsifier

Reject this exact score-entry mechanism if:

- it only works on one venue;
- it reduces breadth materially;
- negative control matches it;
- it fails same-count reconstruction;
- it depends on post-entry or future information.
