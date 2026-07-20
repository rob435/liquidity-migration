# Hedge model-prior refresh policy — replacing "frozen forever"

Adopted 2026-07-20. This replaces the implicit policy that the hedge model
prior (`deploy/hedge_warmstart/bybit_warmstart.csv`) is regenerated only ad
hoc. The prior stays **immutable between refreshes** — the runtime never
extends it with live returns, for the documented reason that the live account
path cannot reconstruct the regression's per-unit book return
(`liquidity_migration/continuous_hedge_manager.py`,
`docs/account_execution.md`). What changes is that refreshes are now
scheduled, bounded, and shrunk instead of indefinitely deferred.

## Why

The deployed betas are plain rolling OLS on the trailing 90 ledger days of a
200-row CSV whose data ends 2026-07-09. Two failure modes follow from
"frozen forever": estimation error from a ~90-observation window is locked
in permanently, and the window's world drifts away from the live world one
day per day. Two failure modes follow from naive refreshing: a slid window
can jump the betas across a regime break (resizing the live hedge sharply on
no new information about the *book*), and each refresh re-rolls the same
estimation noise. Shrinkage plus a coefficient-drift gate addresses all
four.

## The policy

1. **Cadence.** Regenerate the prior with
   `scripts/regenerate_hedge_warmstart.py` after each standard research
   refresh of the continuous equity pipeline, targeting at least one refresh
   per calendar quarter. The regeneration inputs remain the code-defined
   TP12 component ledgers — same estimator, same validation, no live-book
   contamination.
2. **Shrinkage.** `ContinuousHedgeRule` now supports
   `shrinkage_weight` / `prior_beta_1` / `prior_beta_2`:
   `beta = (1−w)·OLS + w·prior`. On each refresh, the registered prior
   vector is the *previous* vintage's estimated betas, with
   **w = 0.3**. This bounds refresh-to-refresh coefficient jumps to 70% of
   the raw OLS move and shrinks per-window estimation noise, while the
   prior vector itself converges to the data over successive refreshes.
   The default `shrinkage_weight = 0.0` keeps current deployed behavior
   bit-identical until a refresh is deliberately promoted; enabling it is a
   registered config change with its own commit-dated record and the normal
   deploy flow.
3. **Guardrails** (all fail closed, `--force` is the only escape and
   requires a written review):
   - existing: input drift (`--max-unit-drift`), row-count monotonicity,
     date-overlap requirement, ≥60-observation minimum, collinearity
     fallback, per-leg and total hedge caps;
   - new: **coefficient drift** — regeneration is refused when the
     runtime-identical trailing betas move more than
     `MAX_PRIOR_BETA_DRIFT = 0.25` (in hedge-ratio units, a quarter of the
     per-leg cap) between the deployed and regenerated vintages. A move
     that large is a regime statement, not bookkeeping, and gets a human
     decision.
4. **No blind priors.** Shrinkage applies only to coefficients the window
   can actually estimate. Insufficient-sample and degenerate windows still
   produce a zero (unhedged) beta, never the prior alone — a hedge sized
   entirely by an old vintage with no current corroboration is worse than
   no hedge.
5. **Attestation unchanged.** Every published hedge target keeps
   `model_prior_live_extension=false` and the full provenance stamp; a
   refresh changes the artifact SHA and is therefore visible in every
   downstream receipt, and requires the normal operational re-authorization.

## Status

- Estimator support and the drift gate are implemented and tested
  (2026-07-20); deployed behavior is unchanged (`shrinkage_weight = 0`).
- First refresh under this policy: due with the next continuous equity
  pipeline rerun; it will carry the current vintage's betas as the prior
  vector, `w = 0.3`, and its own five-line note.
