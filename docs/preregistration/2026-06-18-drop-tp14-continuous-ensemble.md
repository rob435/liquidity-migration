# Pre-registration: drop the `age210tp14` leg from the continuous ensemble

**Date:** 2026-06-18
**Author:** rob435 / karlwitney183 (operator-directed)
**Stage:** accepted (operator override; in-sample basis — NOT a forward-gate pass)

## What's changing

Remove the `age210tp14` component (the `entry_event_trigger="none"`, age210, TP14 leg) from
the deployed continuous-fade ensemble and renormalize the remaining three triggers:

| component | before | after (÷0.90) |
|---|---|---|
| `turn3p3` (p3) | 0.30 | 0.3333… |
| `turn4p3` (p4p3) | 0.20 | 0.2222… |
| `turn4p5` (p4p5) | 0.40 | 0.4444… |
| `age210tp14` (tp14) | 0.10 | **removed** |

Edited everywhere the weights are a source of truth: `continuous_demo.py` (the LIVE demo
daemon's `ensemble_components`), `continuous_forward_replay.FROZEN_FORWARD_CONFIG["weights"]`,
and `WINNER_WEIGHTS` in `continuous_deployed_equity_refresh.py` +
`regenerate_hedge_warmstart.py`, plus the pinned tests. **Demo/paper only;
`REAL_MONEY` stays false.**

## Hypothesis

`age210tp14` is a persistent drag, not a diversifier. In-sample (2023-04…2026-05), as a
single component under the deployed rebalance rule it posts **MAR ≈ 0.97 on bybit and ≈ −0.04
(negative) on binance** — the worst of the four by a wide margin and the only one that loses
on a venue. It is the one leg with **no turn/pop catalyst** (`trigger="none"`), so it fades on
the base spell-entry rather than a volume/pop squeeze, which is where the ensemble's edge
concentrates. Dropping it should leave return ~unchanged while removing dead weight.

## Predicted direction + magnitude

- Ensemble MAR/Sharpe: **≈ flat to slightly up** on both venues (removing a low/negative-MAR,
  ~uncorrelated-drag leg from a vol-targeted blend). It will NOT meaningfully raise return
  (tp14 was only 10% weight).
- Failure mode that would falsify: ensemble MAR/Sharpe drops materially on either venue after
  removal (i.e. tp14 was a genuine diversifier whose low standalone MAR understated its
  portfolio contribution). The forward ledger is the arbiter.

## Roots that will be touched

- [x] bybit_full_pit / binance_full_pit — only via the reconstruction/equity tooling (read-only).
- [x] **forward demo/paper** — YES: changing `FROZEN_FORWARD_CONFIG` changes `frozen_config_hash`,
  which **voids the accumulated continuous forward ledger** (since 2026-06-09); on deploy the
  daemon starts a fresh forward clock (archive the old state dir) and the hedge warmstart must be
  regenerated. This is the deliberate, operator-accepted cost of the change.

## Decision rule (a priori)

"This is an operator-override simplification on in-sample evidence, applied to demo/paper only.
The forward arbiter still governs: after deploy, if the 3-component book's fresh forward MAR is
materially below the (archived) 4-component forward trajectory over a comparable window, revert.
No real-money implication either way."

## Run command

Code change only (no sweep). Equivalence/▸behaviour checks:
```bash
.venv/bin/python -m ruff check liquidity_migration tests scripts
.venv/bin/python -m pytest -q tests/test_continuous_ensemble_profile.py \
  tests/test_continuous_forward_replay.py tests/test_continuous_deployed_equity_refresh.py
```
On deploy (operator-gated, separate): archive the old continuous forward state dir, regenerate
the hedge warmstart, restart the forward clock.

## Post-run results

Implemented in the current worktree: the continuous demo profile,
`FROZEN_FORWARD_CONFIG`, deployed-equity refresh, hedge warmstart, and component
source registry now use the 3-component p3/p4p3/p4p5 object. The old research scout
surface was removed. No commit SHA exists yet in this unstaged worktree.

## Verdict

Operator override accepted; forward ledger reset acknowledged. Demo/paper only.
