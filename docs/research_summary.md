# Research Summary

Updated 2026-07-19 UTC. This is a decision log, not policy or deployment authority.
Apply `docs/governance.md` and inspect the named artifacts before relying on a
claim. Current host state belongs in `STATE.md`.

## Current objects

| Object | Evidence read | Operating boundary |
| --- | --- | --- |
| `continuous_ensemble_v2` | Positive corrected current-profile descriptive curves on both canonical roots; two Bybit trades and one Binance trade have incomplete venue funding coverage and the curve does not prove live-runtime parity | Demo components plus hedge; paper components only; no mainnet authority |
| `LongV11aDivWeekendVol` | Positive current full-PIT result on both venues, but still strongly dependent on take-profit winners and not validated by its tiny skewed forward sample | Demo/paper profile only; no size or mainnet authority |

Binance is a research/replay venue, not a live execution venue.

## Runtime-parity rebaseline 2026-07-19

The historical backtesting stack was not uniformly wrong, but its economic
claims were broader than the implementation evidence justified. The legacy
equity engines did not reproduce the complete account-owner runtime, accepted-
decision BTC-risk state, venue lifecycle, or canonical accounting path. A
separate comparator defect also failed open after an XRP strict-reduction
request was rejected as a sign flip, leaving replay state open. Those defects
invalidate use of the affected historical results as confirmatory alpha or
deployment evidence; they do not make every signal, fill, journal event, or
diagnostic field fictitious.

The rebaseline froze 528,560 raw files (2.496 GB) under logical SHA-256
`9fa1e3a87e813e7449464cf6b512c40cb82d0a13dbce60978e01079e688a81fe`,
rebuilt features with PIT membership applied before cross-sectional ranks and
trailing features, and completed a full 18,644-key ledger comparison with
242,372 unit-aware comparisons and no failures. Runtime repairs made strict
risk reductions fail closed, clamped staged exposure, made terminal flattening
atomic, and reconciled BTC-risk and all registered venue lifecycle events.

The clean repaired comparator at commit
`d8c9c051b4ffcb6116d4332b3244471de6f79e32` passed all 29,449 hourly cycles,
12,812 account events, 911 accepted requests, all 235 lifecycle events, exact
BTC-risk reconciliation, journal verification, zero rejected strict-reduction
batches, and a flat terminal account. Receipt file SHA-256 is
`c2bcd3ebe2e7524bf7370f8209bf471dbf75e59f2e88bf33147a476b5d348c19`;
receipt-payload SHA-256 is
`ad168fddc155a36604e4be0a1b6e002b15d5c7242e9e999508511fda00ef6cf7`.
This is structural/accounting engineering evidence, not a return or alpha
result.

The final deployment candidate at
`9a2f20d85df2cf6211abd65e6c66249865026ad4` reproduced the same frozen
structural gates. Its comparator receipt file SHA-256 is
`f9ad5a6bfcc8948f742ae9bd877b8dda0e3f79d3908d96f274967445d6431e77`;
independent Linux verification covered 87 files and 175,721,151 bytes under
logical SHA-256
`6babc66a5445d43f2559e2d6fc6838cceaf848c37cdd256398591928ed499699`,
with verification-receipt SHA-256
`bb6a8e755c2f07c7361dcb483fb46348b5806931a2027024c64659805dbb5a22`.
An earlier same-commit run completed structurally but failed its final Windows
atomic-directory publication and remains a terminated, unpromoted attempt.

The create-only forward receipt was collected at 2026-07-19 13:09:37 UTC,
with file SHA-256
`db508862314972da310404814519bd701ffc18d2be51a3d39debddee1ef79376`.
It fixes calibration to `[2026-07-19 14:00, 2026-09-02 14:00)` and validation
to `[2026-09-02 14:00, 2026-10-17 14:00)`. Two pre-publication calls failed
closed and created no receipt: one exposed privileged route-owner validation,
and the other exposed a missing paper-capture write path in the strict producer
sandbox. Both were repaired and requalified without inspecting affected
outcomes or changing the registered estimator. See
`docs/prospective_runtime_parity_forward_start_receipt_2026-07-19.md`.

Corrected PIT trade diagnostics are decision-useful for generating narrow new
mechanism hypotheses and for debugging coverage, path, selection, and
execution assumptions. Their already inspected historical surface is spent:
it cannot qualify a new thesis. Any thesis must freeze its claim, complete
tested set, economic rule, and genuinely unseen holdout or new-data boundary
before affected outcomes are read. Execution/TCA is a separate prospective
45-day calibration plus 45-day validation problem and cannot establish
strategy alpha by itself.

## Strategy Overhaul V2 cycle closed 2026-07-18

The registered Bybit discovery cycle completed across 43 months
`[2021-05-01, 2024-12-01)` and closed with **no qualifying thesis**. The
`[2025-01-01, 2026-07-06)` holdout remains untouched; no profile,
implementation, runtime, deployment, size, or capital change follows.

The candidate tape contains 5,850 admitted LONG and 219,846 admitted
CONTINUOUS labels. Equal-date 24h path means are +0.2208% for LONG (95% date
block interval [-0.3170%, +0.7686%]) and +0.3221% for CONTINUOUS ([+0.0385%,
+0.6150%]). The fixed-USD 10,000 barebones portfolios are negative after
modeled economics: LONG gross +3.28%, costs -8.55%, funding +2.04%, net -3.23%;
CONTINUOUS gross +36.60%, costs -40.89%, funding -15.94%, net -20.23%.

Every LONG 24h characteristic contrast is smaller than the frozen 45 bp
round-trip hurdle; the largest is listing age at +38.68 bps. The raw diagnostic
selector incorrectly omitted the economic-score gate and its close-location
selection is invalid; independent recomputation applies the preregistered rule
and yields no LONG thesis. CONTINUOUS `source_composite` is an exploratory lead
(+65.74 bps, interval [+24.15, +108.01]) but cannot qualify because the root
cannot construct the exact current comparator without proven residual-momentum
provenance.

The full ledger/curve is model-based. Exact production account/event/hash
verification is bounded to a prospectively frozen 100-key sample per sleeve
after full replay exceeded the compute stop; both samples verify and end flat,
but the full portfolios are not account-reconciled. The complete identities,
funnels, paths, effects, portfolio diagnostics, deviations, independent checks,
and non-conclusions are in
`docs/strategy_overhaul_v2_completion_receipt_2026-07-18.md`.

The subsequent comparator/accounting repair also closed invalid. The one
run-scoped current-formula RMOM build had 117 stable keys absent from the legacy
feature and 8,597 shared-key value mismatches, all shared-value differences
falling on 2024-11-10 through 2024-11-30. Current kline files in the influencing
window postdate the legacy feature, so the most direct explanation is an input-
vintage mismatch plus current three-day delisting-tail ownership; the prior raw
bytes were not hash-pinned, so that cause remains an evidence-backed inference.
No exact active comparator, ablation, treatment, or holdout was run.

Account-cache and state-copy repairs reproduced the exact frozen 100-key event/
hash receipts. The prospective 200-key benchmark then found two LONG BTCUSDT
gross-P&L differences of `1.2794e-9` and `5.1332e-10` USD outside the committed
USD-unit `rtol=1e-12`, `atol=1e-12` rule. The tolerance was not changed after
inspection; the full retry was not authorized. Trade diagnostics remain useful
for hypothesis generation on their exact spent surface, not for qualifying a
new thesis. See
`docs/strategy_overhaul_v2_comparator_accounting_repair_receipt_2026-07-18.md`.

## Strategy Overhaul V2 Phase-3 structural checkpoint

The first preregistered Bybit candidate-tape partition for
`[2026-07-05, 2026-07-06)` completed structurally at code `e126ecc`. It contains
1,451 unique source keys (11 LONG and 1,440 CONTINUOUS) and 211 exactly matched
path-label keys. The projector rejected and counted 956 invalid-OHLC input rows.
The manifest-file SHA-256 is
`b5d1985d636dcad2d161c81c93e00ace6d6b8307f87f3ff08c023c34dd87da38`;
the full receipt is `docs/strategy_overhaul_v2_phase3_checkpoint_2026-07-17.md`.

This is integrity evidence only. The manifest records
`outcomes_inspected=false`; no label distribution, return, MAE/MFE, P&L,
effect, characteristic ranking, portfolio curve, or thesis was inspected. The
23 pinned baseline artifacts were absent locally, so active comparison remains
disabled. No strategy, parameter, deployment, size, or capital decision follows.

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
