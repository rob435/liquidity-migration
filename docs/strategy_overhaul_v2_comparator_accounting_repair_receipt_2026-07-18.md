# Strategy Overhaul V2 comparator/accounting repair receipt, 2026-07-18

## Decision

The repair cycle is **closed invalid**. The registered exact active
CONTINUOUS comparator was not established, so no gate attribution, nominated
`source_composite` treatment, holdout, profile change, or deployment change was
run. The reserved holdout path remains absent and untouched.

This result does **not** mean the entire backtesting system was wrong. It means
two stronger claims failed their declared evidence gates:

1. the rebuilt current RMOM feature could not be reconciled to the frozen
   legacy feature/input vintage; and
2. exhaustive account replay was not authorized after the repaired 200-key
   benchmark exceeded its frozen account-versus-ledger numeric tolerance.

The prior V2 candidate/path diagnostics remain exploratory hypothesis-generation
material. They are not a valid basis for qualifying or implementing a new
thesis without a newly coherent point-in-time feature/input vintage and an
exact active comparator.

## RMOM provenance gate: invalid

The single permitted build ran at code
`8a6057c8560960f6c95981cb9a7473eb176bb933` with the registered current
formula: Common4 factors, seven-day rolling sum, four-observation minimum,
three-day causal shift, `[2023-03-01, 2024-12-05)`. It wrote only the run-scoped
artifact and did not replace the shared legacy table.

- Artifact: 186,404 rows, including 186,279 stable rows; SHA-256
  `d47a82ef7f42e47a56aef5db27724aa1e45215565c2d7622743a0ca84fb4738b`.
- Comparator window rebuilt/legacy rows: 157,521 / 157,404.
- Key mismatch: 117 rebuilt-only stable keys, zero legacy-only keys. The 117
  rows are exactly three post-legacy-tail days for each of 39 symbols.
- Value mismatch on shared keys: 8,597 rows across 418 symbols and 21 dates,
  all from 2024-11-10 through 2024-11-30; maximum absolute difference
  `0.0013792661294597663` against `rtol=1e-10`, `atol=1e-12`.

The legacy feature was written 2026-06-26. Within the build's logical input
window, 302 current kline files across 31 dates from 2024-11-07 through
2024-12-07 have mtimes after that legacy feature. The three-day causal lag
aligns the first feature mismatch with 2024-11-10. This supports the inference
that the current raw root and legacy RMOM use different input vintages, but no
prior raw-file hashes exist to prove the exact historical byte changes.

The receipt pins 215,036 kline, 209,697 funding, 141,736 open-interest, and
209,649 premium files through sorted canonical aggregate hashes. Receipt
SHA-256:
`ff49e4acef2adada9b51ec5d2d6c1e4ed1edc814cee692480dc6216167bb6dd5`.

Per the preregistration, neither dropping the 117 rows nor ignoring/retuning
the November value differences was permitted. The exact comparator therefore
stopped before any strategy outcome was generated.

## Account replay gate: invalid beyond the frozen sample

The first cache repair removed history-sized event-list and event-ID-map copies.
The original frozen 100-key samples then reproduced exactly for both sleeves:
event counts, fills, final state hashes, last event hashes, strategy-event-tape
hashes, original kernel-transaction counts, source-key hashes, and gross-P&L
coverage. Sample receipt SHA-256:
`e62029e88a04591c9b2fc2a6115503deb57aca7b1190fbb358a42ce197a927ea`.

The first full attempt was stopped and preserved before ten minutes because
each transaction still deep-copied the growing historical `AccountState`.
Failure receipt SHA-256:
`81a545e08cdb6bee6364ac9e077726d1750a5c1db0fdaa9c1e2096b4b5ed103d`.

The prospectively registered recovery added a default-off, single-process
historical in-place state path. Production/default isolation and rollback
semantics remain unchanged. Deterministic fixtures and the frozen 100-key
sample reproduced exact event/hash/state identities under the recovery.

The registered 200-key-per-sleeve benchmark nevertheless found two LONG
BTCUSDT gross-P&L attributions outside the committed USD-unit `rtol=1e-12`,
`atol=1e-12` check:

| Component | Account USD | Ledger USD | Absolute difference | Relative difference |
| --- | ---: | ---: | ---: | ---: |
| `long:bybit:BTCUSDT:1673568000000` | 735.0842568683121 | 735.0842568695915 | 1.2794316717190668e-9 | 1.7405238375905607e-12 |
| `long:bybit:BTCUSDT:1686096000000` | -229.42285985809679 | -229.4228598586101 | 5.133244940225268e-10 | 2.237460095907099e-12 |

These discrepancies are economically negligible, but the unit/tolerance rule
was implemented and committed before the result. It was not relaxed or
reinterpreted afterward. The recovery contract therefore forbade the full
retry. The full 1,899 LONG / 16,745 CONTINUOUS account claim remains unverified.
Benchmark failure receipt SHA-256:
`a69a15f0a773ed64d6840d56ab5450cb55626b1493e75898c49def98d64c4a48`.

## What remains useful

- The V2 source funnel and path labels are useful for generating hypotheses
  and diagnosing where candidates are admitted or lost on their exact spent
  discovery surface.
- The barebones ledgers retain exact internal return identities and show that
  the tested source populations were negative after their modeled economics.
- The frozen 100-key account samples establish exact kernel event/hash behavior
  for those keys only.
- The `source_composite` contrast remains a lead with known late-era decay, not
  a qualified thesis and not evidence for a profile change.

Not established: exact active-runtime historical parity, accepted-decision
BTC-risk replay, manifest-backed active ranks, full-account reconciliation,
calibrated execution/slippage, independent venue replication, size/capacity
beyond the declared base scale, deployment readiness, mainnet readiness, or
real-money authority.

## Code and artifact boundary

- Parent contract commit: `78f4c78`.
- Initial repair commit: `7e1a613`.
- Outcome-blind runner commit: `8a6057c`.
- Account-state recovery contract commit: `9e2ee10`.
- Default-off account-state recovery implementation: `7be92a0`.
- No holdout path was created or read.
- No demo/paper target, external order, profile, hedge, size, capital, mainnet,
  or real-money state was changed.
