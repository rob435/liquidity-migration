---
name: backtest-integrity
description: Assess methodology integrity for backtests, research runs, strategy or feature changes, and result interpretation in this quant repository. Use before designing or running decision-influencing research, when judging a report, or before making an alpha, robustness, candidate, deployment, or real-money claim. Apply the claim-scoped validity policy in docs/governance.md and the failure taxonomy in docs/backtesting_errors_we_never_repeat.md; do not universalize legacy thresholds or venue rules.
---

# Assess backtest integrity

Read `docs/governance.md` and the relevant failure modes in
`docs/backtesting_errors_we_never_repeat.md`. Treat the active prospective
contract, raw artifacts, code, and data provenance as evidence; labels and old
verdicts are not authority.

## Define the claim

State the proposition, intended decision, venue, population, period, mechanism,
operating scale, study mode, and which outcomes have already been inspected.
Apply checks proportional to that claim.

## Hard validity

- **Causality:** verify decision, availability, order, fill, exit-activation, and
  state-initialization times. Stress uncertain latency.
- **Population:** require PIT membership when historical universe selection
  matters. Distinguish archive observations from current-listing inference.
- **Execution:** model feasible orders, fills, venue mechanics, capacity, and
  material costs for performance claims.
- **State:** initialize exits, cooldowns, baskets, hedges, and risk memory only
  when the forward system could know them.
- **Accounting:** reconcile positions, cash/equity, fees, funding, netting,
  flips, and lifecycle events at the granularity required by the claim.
- **Reconstruction:** retain root, code, config, tested-set, exposure, and
  artifact identities.
- **Anomalies:** stop relying on affected output until impossible prices,
  synchronization, missing rows, or forward drift are explained.

Classify validity as `valid`, `limited`, or `invalid` for the exact claim.
A useful diagnostic may survive even when a larger claim does not.

## Inference

Use a declared comparator and disclose every variant that influenced selection.
Track spent windows; renaming reused data does not restore independence. Choose
holdouts, walk-forward, purging, venues, metrics, and thresholds from the claim,
not repository folklore. A second correlated crypto venue is robustness
evidence, not automatic independence.

Report effect size, uncertainty, concentration, fragility, and practical scale.
After outcomes are viewed, keep the registered rule. Revisions are prospective
on a new evaluation surface or exploratory on spent data.

Historical artifacts may encode old metric presets. Apply one only when the
active contract prospectively names that exact rule; otherwise label it as a
diagnostic.

## Evidence card

Report:

- claim, validity, study mode, and result;
- venue/population/period/scale scope;
- comparator, tested set, exposure status, and uncertainty;
- PIT/timing/fill/cost/capacity/accounting limits;
- artifact and identity references;
- deployment mode and authorization state;
- justified next action and explicit non-conclusions.

Research quality never grants mainnet authority. Real money requires a separate
owner instruction naming the deployment, capital/risk limits, controls, and
expiry under `docs/governance.md`.
