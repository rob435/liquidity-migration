# Retired Strategy-Overhaul Lessons

Updated 2026-07-16. This is a retirement and transfer record, not an active
research contract. Evidence policy remains `docs/governance.md`.

## Outcome Boundary

The overhaul produced **no strategy result**. It emitted no completed S02
signal-time tape, S03 entry-anchor artifact, S04 path labels, focal estimate,
PnL, or confirmatory verdict. It authorizes no alpha, sizing, promotion,
deployment, or real-money conclusion.

The work was retired because its verification surface became the main product.
At the final local audit snapshot, the dirty worktree contained 43
`strategy_overhaul*` production modules (about 61,900 lines), 205 internal
imports, and a 14-module strongly connected component. Changed and untracked
work together added roughly 113,000 lines. A focused suite passed 113 tests,
but the broader 740-test overhaul selection did not finish within the audit
window. Those facts show substantial engineering effort, not decision value.

Do not merge or reconstruct that module graph. New research should start from
current code, a small claim-specific contract, and the minimum artifacts needed
to falsify the claim.

## What The Runs Actually Established

Two Phase-0 receipts must remain distinct:

- `strategy-overhaul-phase0-bccefdfc38ae9fda3c17` returned `NOT_READY`, read
  no outcomes, and is already recorded in `docs/research_summary.md`.
- A later current-source structural bundle,
  `strategy-overhaul-phase0-8d6314fec8717954be9a`, returned `READY` while
  retaining `outcome_run_authorized=false`. Its bundle ID is
  `strategy-overhaul-phase0-8d6314fec8717954be9a--bundle-fc981166d3288f913d44`;
  the receipt SHA-256 is
  `93308a9661a8dc4b2520afccbdda05f67d12ab5b7116655b8514c614bc74a8fa`.
  `READY` meant that structural prerequisites passed under that captured dirty
  source snapshot. It was not a result or authorization.

The latest Phase-B run was
`strategy-overhaul-phase-b-ebcfdad2b9cd2809ccefc7e2`. It wrote events 00
through 04 of a nine-step plan: acquisition/materialization verification, root
snapshots, root/PIT checkpoints, independent population-key replay, and the
venue-local instrument map. It stopped before canonical expected-population
checkpoints, LONG sidecars, S02 descriptor inputs, or a completion receipt.
There is no terminal supervisor receipt. Partial infrastructure must not be
described as a completed phase.

The frozen label-tail staging log recorded:

- Binance: 812 symbols, 3,248 symbol-day jobs, 75,121 hourly rows, 116 archive
  404s, and no hard fetch failures;
- Bybit: 624 symbols and 2,480 manifest rows.

No stage-specific semantic tail receipt or outcome artifact followed. These
counts are acquisition diagnostics only.

## Retained Empirical Diagnostics

### The CONTINUOUS components are mostly repeated decisions

The historical component ledgers contained 2,279 Bybit and 2,152 Binance rows,
but only 896 and 872 unique `(symbol, signal_ts)` decisions. Respectively
93.90% and 92.89% of component rows shared a decision with another component;
626 Bybit and 561 Binance decisions appeared in all three components.

The effective sample unit is the unique decision, then the simultaneous wave or
calendar block appropriate to the claim. Component-row count must not be used as
independent sample size, and component weights must not be presented as three
independent alpha sources.

### The old fill-cost model was not calibrated

Four clean post-reset Bybit component pairs, covering two symbols on one entry
day, showed mean/median/worst adverse entry-price drift of 129.80/141.65/170.73
bps, recorded entry fees of 5.50--11.00 bps (6.88 bps mean), and 148.7--165.5
seconds from cycle start to venue fill. The sample is far too small to estimate
a replacement slippage model, but it is enough to reject the old roughly
20--23 bps modeled round trip as calibrated.

Paper remains explicitly `integration_only_uncalibrated`. Forward execution
observations support only their exact order/fill/fee/reconciliation claims.

## Lessons Already Carried Into Current Code

| Lesson | Current owner |
| --- | --- |
| Aggregate same-symbol component/sleeve targets before venue submission | Account kernel and `docs/account_execution.md` |
| Start lifecycle/protection clocks from attributable confirmed fills | Account strategy state and `docs/account_execution.md` |
| Stable RMOM is immutable; provisional tail rows remain explicit and replaceable | `scripts/precompute_residual_momentum.py` and `continuous_events.py` |
| An unmeasured LONG MAE/MFE is missing, not zero | `long_native.py` and finite-only trade-lifecycle aggregation |
| Paper fill behavior is an integration simulator, not performance calibration | `docs/account_execution.md` |
| Valid Unicode Binance USD-M symbols need strict identity validation, URL quoting, and reversible partition encoding | `liquidity_migration/symbol_codec.py`, storage, and Binance Vision acquisition |

The Binance tail contained three such canonical symbols:
`币安人生USDT`, `我踏马来了USDT`, and `龙虾USDT`. Silently filtering them or
letting their downloads fail inside a tolerated error ratio creates a population
hole.

## Research Invariants Worth Keeping

- A historical-universe claim needs PIT membership with launches, delistings,
  renames, migrations, and explicit provenance limits.
- Raw capture keys, materialized partition keys, and replayed expected keys must
  agree. A mismatch is a failed population claim, not a warning to waive.
- Rolling features must split at data gaps; they may not bridge missing bars.
- Candidate observability belongs before alpha gates. Preserve each row's first
  rejection and missingness so attrition is auditable.
- Keep signal-time features, entry anchors/policies, and future path labels in
  separate keyed stages. Signal-time data may not contain post-decision values.
- Do not synthesize missing granular paths from hourly aggregates, and do not
  convert unmeasured values to favorable zeros.
- Receipt volume is not evidence quality. Every artifact must be necessary for
  a stated claim, consumer, or reconstruction step; otherwise delete the
  machinery and keep the lesson.

## Untested Backlog, Not Findings

The retired contracts named four focal questions but never evaluated them:

- whether CONTINUOUS D9 adds association beyond the current pump/turnover state;
- whether the CONTINUOUS BTC-uptrend gate separates paths among otherwise
  eligible events;
- whether LONG's joint BTC+ETH regime separates paths from any-off states; and
- whether LONG's exact 1%/six-hour retrace policy improves on a common
  next-close anchor.

They are untested ideas, not positive, negative, or inconclusive results. Any
revisit needs a new current-code registration and a fresh exposure ledger.

## External Tombstone

The last-observed host-local archive root is
`C:\Users\user\SHARED_DATA\strategy_overhaul_prospective_2026-07-11`.
It is external to Git and its path is not a portability guarantee. Preserve the
receipt IDs and hashes above when moving or deleting it; do not promote its
partial products into the repository as current research objects.
