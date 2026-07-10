---
name: backtest-integrity
description: Assess methodology integrity for backtests, research runs, strategy or feature changes, and result interpretation in this quant repository. Use before designing or running decision-influencing research, when judging a report, or before making an alpha, robustness, candidate, deployment, or real-money claim. Apply the claim-scoped validity policy in docs/governance.md and the failure taxonomy in docs/backtesting_errors_we_never_repeat.md; do not universalize legacy thresholds or venue rules.
---

# Assess backtest integrity

Read `docs/governance.md` and the relevant parts of
`docs/backtesting_errors_we_never_repeat.md` before acting. Treat the active
experiment contract, raw artifacts, code, and data provenance as evidence;
never treat a label or prior verdict as authority.

## Start with the claim

State:

- the exact proposition and intended decision;
- venue, population, period, mechanism, and operating scale;
- study mode: exploratory, confirmatory, or forward execution;
- which outcomes have already been inspected.

Apply only checks relevant to that claim. A feature-timestamp audit, an
entry-agreement reconciliation, and a net-performance backtest need different
artifacts and cost assumptions.

## Check hard validity

- **Causality:** verify decision, availability, order, fill, exit-activation,
  and state-initialization times. Stress uncertain latency.
- **Population:** require PIT membership when historical universe selection
  matters. Inspect manifest provenance; a current-listing-derived row is not an
  archive observation.
- **Execution:** model feasible orders, fills, venue mechanics, capacity, and
  material costs for performance claims.
- **State:** initialize adaptive exits, cooldowns, baskets, hedges, and risk
  memory when the forward system could first know them.
- **Accounting:** reconcile positions, cash/equity, fees, funding, netting,
  flips, and lifecycle events at the granularity the claim needs.
- **Reconstruction:** retain data/code/config identity, tested variants,
  effective sample unit, exposure history, and source artifacts.
- **Anomalies:** stop relying on affected output until impossible prices,
  synchronization, missing rows, or forward drift are explained.

Classify the affected claim as `valid`, `limited`, or `invalid`. Keep useful
diagnostics even when they cannot support the larger claim.

## Check inference

- Compare against a declared control or counterfactual.
- Disclose the full tested set, repeated peeks, dependence, and effective trials.
- Track which windows are spent; do not relabel reused data OOS.
- Use holdouts, walk-forward, purging/embargo, cross-venue tests, or forward
  epochs only when they fit the claim.
- Treat a second correlated venue as robustness evidence, not automatic
  independence. Require it when portability or the experiment contract does.
- Report effect sizes, uncertainty, concentration, fragility, and practical
  scale—not only a threshold verdict.
- Keep the preregistered rule after viewing outcomes. Any revised rule is
  prospective or exploratory on the spent data.

Legacy analyzers such as `scripts/r1_robustness.py` and
`scripts/apply_decision_rule.py` implement historical policies. Use them as a
binding verdict only when the selected experiment explicitly names that preset.

## Report an evidence card

Return or write:

- claim;
- validity and reasons;
- study mode;
- result: supports, contradicts, or inconclusive;
- scope and non-generalizable boundaries;
- deployment mode and authorization state;
- effect size/uncertainty and material debts;
- artifact and identity references;
- justified next action and explicit non-conclusions.

Compatibility run labels (`invalid`, `exploratory`, `biased_benchmark`,
`candidate`, `paper_ready`) may still appear in artifacts. Report them verbatim,
but do not let one label collapse the evidence card or authorize deployment.

## Preserve the safety boundary

Never infer mainnet authority from a research result or broad repository
permission. Real-money work needs the exact evidence, controls, code/config,
limits, expiry, and separate owner authorization required by
`docs/governance.md`.
