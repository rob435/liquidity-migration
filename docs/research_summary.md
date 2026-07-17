# Research Summary

Updated 2026-07-17 UTC. This is a decision log, not policy or deployment authority.
Apply `docs/governance.md` and inspect the named artifacts before relying on a
claim. Current host state belongs in `STATE.md`.

## Current objects

| Object | Evidence read | Operating boundary |
| --- | --- | --- |
| `continuous_ensemble_v2` | Positive corrected current-profile descriptive curves on both canonical roots; two Bybit trades and one Binance trade have incomplete venue funding coverage and the curve does not prove live-runtime parity | Demo components plus hedge; paper components only; no mainnet authority |
| `LongV11aDivWeekendVol` | Positive current full-PIT result on both venues, but still strongly dependent on take-profit winners and not validated by its tiny skewed forward sample | Demo/paper profile only; no size or mainnet authority |

Binance is a research/replay venue, not a live execution venue.

## Corrected canonical benchmark completed 2026-07-17

The current descriptive benchmark uses `[2023-07-17, 2026-07-17)` at 1x
modeled exposure and 1x chart presentation, ending on completed UTC day
2026-07-16. It ran at code commit `b095d5c` after exact-settlement funding,
stable aged-out RMOM keys, and chronological terminal-tape closure. It is
exploratory, historically exposed evidence with no pass threshold and
authorizes no strategy, parameter, deployment, sizing, or capital change.

| Profile | Venue | Trades | Return | Max drawdown | Sharpe-like | MAR | Funding |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| LONG | Bybit | 183 | +35.07% | -3.27% | 2.20 | n/a | modeled, 100%; 1,521 events |
| LONG | Binance | 183 | +29.37% | -3.07% | 1.66 | n/a | modeled, 100%; 1,536 events |
| CONTINUOUS (`turn3p3/turn4p3/turn4p5`) | Bybit | 786 / 739 / 659 | +20.65% | -1.30% | 2.66 | 5.31 | partial: two trades per component |
| CONTINUOUS (`turn3p3/turn4p3/turn4p5`) | Binance | 766 / 703 / 611 | +16.55% | -1.56% | 2.36 | 3.54 | partial: one trade per component |

Bybit CONTINUOUS has 807 union trade IDs across 2,184 component rows; Binance
has 787 across 2,080. The partial Bybit rows are `PENGUSDT` on 2025-01-30 and
`1000MUMUUSDT` on 2025-06-11, repeated across components. The partial Binance
rows are `HOOKUSDT` on 2026-03-23, also repeated. Each affected trade has 1%
component notional. Fresh narrow venue queries returned exactly the same rows
already in the canonical roots, so no missing settlement can be safely appended
or inferred. All other component rows are modeled.

Both LONG reports are `full_pit_universe`, untainted, warning-free, and have
zero missing required manifest date-symbols. CONTINUOUS remains an
`exploratory_historical_equity` reconstruction and does not replay the complete
demo account lifecycle or accepted-decision BTC-risk state. No frozen demo and
paper account snapshots were available, so the automated three-way step
correctly recorded `skipped_no_account_snapshots`; no parity claim exists.

The hash-pinned curves, equity CSVs, reports, and complete ledgers are indexed
in `docs/strategy_overhaul_v2_baseline_2026-07-17.md`. The run summary snapshot
is `1a50d9fcaaa8064ca82b897d33cab0f44026c001b7d4d93195c57bbc6b540537`.
The superseded 2026-07-16 baseline remains a historical receipt only.

### Funding correction attribution

The same-window run `funding-correction-same-window-2026-07-17` held
`[2023-07-16, 2026-07-16)`, strategy decisions, exits, weights, costs, and
presentation fixed. All six LONG/component trade-ID sets and all non-funding
fields were unchanged. Exact settlements reduced CONTINUOUS return from the
invalid 21.06% to 20.25% on Bybit and from 17.35% to 16.58% on Binance. The
rolling benchmark cannot be subtracted from the old baseline as a pure funding
effect because its window, RMOM keys, and terminal lifecycle also changed.

The owner-supplied Bybit paste supplied settlement evidence, not a full account
ledger: all 27 funding rows matched the public canonical timestamps and rates
after BST/sign normalization, but shortened identifiers and the absence of
wallet, position, order, and closed-PnL series prevent account reconciliation.

### Big-PC branch discrepancy

The obsolete `codex/account-kernel-binance-combined` branch compared TP10 pre
outputs with TP12 post outputs. Across 816, 743, and 614 rows whose interval and
weight stayed fixed, funding event count and funding return matched exactly;
there were zero mismatches. Aggregate funding changed because TP12 changed
exits and holding time. Both old checkouts shared the same flawed modal-cadence
funding blob, so neither old funding result was authoritative.

That branch did correctly diagnose missing/competing active marks in historical
account-kernel replay. Main commit `be6367f` independently contains equivalent
pending-exit mark retention and current-decision price precedence. Current
`main` additionally has the exact-funding, RMOM, and terminal-tape fixes. The
full preserved diagnosis is
`docs/account_kernel_binance_combined_audit_2026-07-17.md`; the branch's 1.4 GB
artifact bundle and Windows adapters are not merge material.

## Continuous v2

The active component runtime has three components (`p3`, `p4p3`, `p4p5`), stable
causal RMOM q25, inverse-vol sizing, a prior-day BTC uptrend gate, TP12, and a
24-hour max hold. It also applies the stateful live
`CTRL_BTC_RISK_70_90_35` size overlay. The separate demo hedge uses BTC+ETH and
the BTC-vol regime; daily rebalance is disabled.

The standard historical curve reconstructs the base components and hedge but
does not apply the accepted-decision BTC-risk state. It also does not establish
manifest-backed historical membership merely by reading a `full_pit` root.
The current canonical builders and separate validation receipts improve data
provenance, but the curves remain limited diagnostics rather than complete
replay of the active runtime or decision-grade historical-population evidence.

The retained controls below were produced by the retired receipt-derived TP10
reconstruction. The active code now reconstructs TP12 to match runtime, so
these numbers are not reproducible under the current profile and must not be
cited as current-profile performance.

Historical TP10 exploratory controls:

| Run | Venue | Return | Max drawdown | MAR |
| --- | --- | ---: | ---: | ---: |
| Retired TP10, 2026-06-27 | Bybit | +26.64% | -1.13% | 7.33 |
| Retired TP10, 2026-06-27 | Binance | +18.84% | -1.02% | 5.72 |
| Retired TP10 stable-only, through 2026-07-10 exclusive | Bybit | +24.36% | -1.20% | 6.22 |

These runs are controls, not untouched confirmation, live-size approval, or
proof that the daemon/account lifecycle exactly matches historical execution.
Funding coverage differed by run and must be rechecked for any new net-return
claim.

The 2026-07-10 1000TAGUSDT demo incident produced account-authoritative Closed
PnL of `-$87.69678926`, or 0.873502% of entry equity. The retired demo-only
sniper adds contributed part of the loss and had neither paper nor
decision-grade historical support. The full receipt is
`docs/incidents/2026-07-10-1000tag.md`.

Durable mechanism reads:

| Mechanism | Current conclusion |
| --- | --- |
| 20%/40%/80% fixed stops | Contradicted as improvements: each reduced MAR on both tested venues. This does not prove no executable safety control can help. |
| +1h/+2h entry delay | Rejected by the tested full component+hedge replays. |
| +1% adverse-limit entry | Useful path diagnostic, contradicted by the tested full replay; runtime add-on retired. |
| Daily volatility rebalance | Keep disabled; the tested version mostly saturated leverage and worsened registered risk metrics. |
| BTC gate removal/non-30d retunes | Contradicted on the tested grid; retain the 30-day prior-day control. |
| BTC-risk tail hard skip (separate from live 0.35x sizing) | Mixed venue result and rejected under its registered rule. |
| Conditional scale-in | Higher return but worse MAR/drawdown on both tested overlays; no runtime add-on. |
| Signal-invalidation exits | Sparse or zero-hit evidence; no retained exit. |
| Upper-wick sizing | Retracted after duplicate-counting and implementation-agreement audit. |
| Symbol/time blacklist | No deployable common arm under the tested contract. |

## Long v11a

Current descriptive refresh through the 2026-07-16 signal day:

| Venue | Trades | Return | Max drawdown | Sharpe-like |
| --- | ---: | ---: | ---: | ---: |
| Bybit | 183 | +35.07% | -3.27% | 2.20 |
| Binance | 183 | +29.37% | -3.07% | 1.66 |

The July refresh did not rerun the mechanism ablations. Earlier work remained
positive after best-month removal, 2x/3x cost stress, worst-12-month windows,
and a matched-symbol null on both venues. Removing the take-profit bucket
changed Bybit/Binance returns to -0.92%/-5.99%, so the result remains TP-tail
dependent. A single forward ADA pair had roughly 9.47 hours of entry skew and
34,091.786 seconds of exit skew; that is execution disagreement, not
validation.

## Execution evidence

Retired demo probes produced no decision-grade execution model. Paper operation
therefore uses a visibly labelled `integration_only_uncalibrated` twin and is
not performance evidence. Demo/account receipts can support claims only about
their exact observed order, reconciliation, fee, funding, and recovery paths.

## Cancelled and retired work

The following prospective studies were cancelled without a treatment run and
therefore produced **no result**: account natural replay, continuous tail
survival/loss-budget, granular adverse-risk, BTC-month regime, and
time/symbol-risk variants. Their runners and verbose contracts were removed.
Do not describe cancellation as empirical rejection.

The strategy-overhaul Phase-0 diagnostic bundle
`strategy-overhaul-phase0-bccefdfc38ae9fda3c17` has receipt SHA-256
`ed5fb3687280db691dcda5e32e00005a8dd48dd2fb403c2f48fe6cb69a81bb03`.
It returned `NOT_READY`, inspected no outcomes, and established only limited
internal reconstruction under its captured environment.

A later current-source structural Phase-0 bundle,
`strategy-overhaul-phase0-8d6314fec8717954be9a`, returned `READY` with
`outcome_run_authorized=false`; its receipt SHA-256 is
`93308a9661a8dc4b2520afccbdda05f67d12ab5b7116655b8514c614bc74a8fa`.
The subsequent Phase-B run
`strategy-overhaul-phase-b-ebcfdad2b9cd2809ccefc7e2` completed only events
00--04 of nine and published no completion receipt, S02 feature tape, S03 entry
artifact, S04 labels, or outcome. The workflow and code were then retired. Both
Phase-0 receipts and the partial Phase-B directory are tombstones; they support
no population, alpha, promotion, deployment, or real-money conclusion. Durable
diagnostics and reusable constraints are recorded in
`docs/strategy_overhaul_lessons.md`.

No confirmatory research contract is active. New decision-influencing work must
register a new claim, exposure boundary, tested set, rule, and artifact plan
before outcomes are inspected.
