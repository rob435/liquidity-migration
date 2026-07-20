# Research Summary

Updated 2026-07-20 UTC. This is a decision log, not policy or deployment authority.
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
execution assumptions. Their already inspected historical surface shapes
ideas rather than grading them: under the Progressive Evidence Model a new
thesis is committed as a config and graded on the rolling run of forward
days it predates (an untouched historical reserve is an optional
accelerant). Execution/TCA models likewise accumulate rolling forward
evidence; execution quality by itself does not establish strategy alpha.

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

## Strategy Research V3 (exploratory, 2026-07-19)

Lane-1 execution of `docs/preregistration/DRAFT_strategy_research_v3_2026-07-19.md`
on the spent V2 discovery surface (per-thesis evidence cards, grids, and
manifests under `reports/strategy-research-v3/`). The `[2025-01-01,
2026-07-06)` label-level holdout stays unread. None of this is alpha,
robustness, or promotion evidence; prototypes advance only through the forward
rolling ledger.

| Thesis | Exploratory outcome |
| --- | --- |
| T-B funding floor | Floor rarely binds at the barebones 12% TP shape (23–83 of 16,745 trades). Strictly-PIT prev-rate variant is not era-stable (+0.95pp early, −0.42pp late). Advance-known next-rate variant improves both eras (up to +3.1pp full, funding saved 3.6% vs gross forgone 0.7%) but its PIT status requires verifying Bybit fixes the next settlement's rate at interval start. Drain-exit rule refuted everywhere (−5 to −11pp; early exits forfeit more gross than the funding they save). |
| T-C pump deceleration | Premise refuted on this ledger: adverse excursion concentrates in decelerating entries (MAE<−10% share 31.4%) not accelerating ones (23.9%), era-stable. Delay-until-deceleration is neutral-to-worse; skip-accelerating improves totals only by shrinking a negative-mean book while worsening per-trade net and deep-MAE share. |
| T-D funding forecast | Cumulative funding is modestly predictable beyond persistence, concentrated in tails and 48–72h horizons (up to −17% tail MAE via short-half-life EWMA / mean-reversion). The pre-declared Stage-2 bar (≥10% on both overall and q95-tail MAE at 24h) was not met; the T-B floor substitution did not run. |
| T-A regime-gate ablation | Thesis refuted on paired full-history renders (2023-04→2026-07): removing the BTC uptrend gate doubles entries (2,300→4,019) yet loses ~1.0pp total return, takes ~5× the max drawdown (−6.35% vs −1.30%), and nearly doubles negative common-loss tail days (28 vs 15). Early era alone favors removal on return; the late era decisively favors the gate. Gate-off fails the declared mean+tail test; no prototype advances. |

## Strategy Research V4 (exploratory, 2026-07-19)

Lane-1 execution of `docs/preregistration/DRAFT_strategy_research_v4_2026-07-19.md`
(evidence cards, grids, and manifests under `reports/strategy-research-v3/t-e`
… `t-i/`). All five theses closed; **no forward-ledger prototype advanced.**
The `[2025-01-01, 2026-07-06)` label-level holdout stays unread (render-book
diagnostics touch already-rendered T-A outputs only, the boundary T-A
declared). None of this is alpha, robustness, or promotion evidence.

**Program-level finding (the owner-directed double-verification rule did its
job):** every entry-quality cut that works on the barebones ledger inverts on
the deployed-shape render books. Fresh-high and deep-negative-funding
conditioning are large, era-stable improvements on the barebones surface, yet
the same conditions mark the render books' *best* trades under both gate
states — the barebones 12%-TP/24h-hold shape, not the entry, is what those
cuts measure. Future entry-conditioning work needs render-native surfaces.

| Thesis | Exploratory outcome |
| --- | --- |
| T-E fresh-high conditioning | At-high (≤1h since 168h high) bucket is positive in both eras and every year; skip rules gain up to +28pp on the barebones ledger (mostly cost/funding savings) but only skip_h6 clears both eras, and all skip rules would *forfeit* net-positive mass on both render books (>24h entries are +15.0% net on the gate-on book). Real ranking signal, non-transferable hard filter; no prototype. |
| T-G funding-state conditioner | Only fully era-stable ledger family: all 9 cells positive in both eras; best is combo (skip deep-neg only when the T-D meanrev forecast agrees, +7.99pp; the forecast rescues 296 trades worth +2.08pp). Sign inverts on both render books — deep-neg entries are their best funding bucket (+16.5/+19.6% net, 40%+ TP rates). Shape artifact; no prototype. Bybit funding-timing verified: next-settlement rate is only final at settlement since 2022-06-30/07-05, so T-B's "next-rate" floor is not registrable; the `prev` convention stays valid. |
| T-F MFE give-back ladder | Re-simulator reproduces all 16,745 recorded exits exactly, then no cell survives: tight arms forfeit more TP completions than the give-back they capture (−148pp vs +144pp flows at A=4%/R=0.7); best cell nets +1.2pp as the residue of ±54pp opposing flows and inverts (−1.25pp) on the T-E-filtered axis. Adaptive exits now closed on both 1m (2026-06-20) and 1h granularities. |
| T-H expected-net ranker | Refuted on its own declared tests: drop-bottom-decile (+4.7pp) and quintile sizing (+2.6pp, worse maxDD) lose to the simple T-E∧T-G conditioner (+41.1pp) by an order of magnitude; 4 of 9 ridge coefficients flip sign across the 10 walk-forward refits; decile ranking is non-monotone mid-distribution and only extreme-bottom toxicity transfers to the render books (n=51). |
| T-I regime intensity | No member advances under the registered MAR+tail rule. The linear intensity member Pareto-dominates the binary gate on every risk dimension (equal net, −5pp maxDD, better tail everywhere) but loses the MAR comparison because MAR is ill-posed at negative net — recorded as a decision-metric lesson for any future registration. Two-sided intensity decisively refuted (retains 155/156 negative tail days). |

## Strategy Research V5 — deployed-book conditioning (exploratory, 2026-07-20)

Owner-directed iteration after V4 moved the discovery surface to the
deployed-shape render books (`reports/strategy-research-v3/t-j/2026-07-20/`).
Three candidates fell to their controls: the exit-geometry hypothesis died on
anatomy (render books carry the identical TP-12%/24h shape; the deployed edge
on deep-neg entries is selection, 41% vs 26% TP completion), the deep-neg gate
override died on the barebones cross-check (deep-neg ∩ downtrend negative in
both eras), and the freshness sizing tilt died on the deployed book itself —
at-high entries are already ~62% of its notional, so the tilt lands inside the
label-permutation noise band (75th percentile) and is not component-consistent.
Program-level conclusion: the deployed CONTINUOUS sleeve is at a local optimum
for every coarse 1h-bar observable measured across V4+V5 and the prior
closures; entry/exit/sizing conditioning at this granularity is mined out on
spent surfaces.

One lead survives and is frozen as a Lane-2 prototype
(`t-j/2026-07-20/prototype_freshness_gate_override.json`): admit an
otherwise-BTC-gate-blocked entry iff `hours_since_high_168h ≤ 1` at the entry
bar close. Support: +5.72% (component-summed) / +2.40% (single-counted) over
2024-11 → 2026-07, 74.7% win rate, 158 symbols, all three components positive,
13/18 months — but the support is one era of one surface, with a stated
failure mode (2025-03/04 −17.7pp) and −2.36pp on tail-day trades. Its evidence
is exclusively post-commit forward days; promotion or any runtime change
remains an owner decision.

## Breadth funnel replay T-K (exploratory, 2026-07-20)

The Lane-1 prerequisite for `continuous_breadth_v1`
(`reports/strategy-research-v3/t-k/2026-07-20/`): the live CONTINUOUS
admission funnel replayed over 2023-04 → 2026-07 (683 gate-open days) with
the candidate knobs, under deployed constraints (BTC gate, max_active,
adverse-exit circuit breaker). The two numbers: **7.30 admitted
bets/gate-open day** at the candidate 250k/cap-10 knobs (baseline 6.55;
7.70 at 100k; the ≥8–10 target is not reached — the cap binds on <2% of
cycles and the event triggers are the true bottleneck at 20% pass), and
**ρ̂ ≈ 0.21** (pooled same-day pairwise estimator). The decisive third
number: measured per-bet vol ≈ **1,000 bps**, not the power table's assumed
300 — with measured inputs, a 15/25 bps edge needs ≈15.6/5.6 years at any
achievable breadth, and the candidate knobs cut days-to-significance by
only ~9% versus baseline. Admission breadth alone cannot deliver the
promised learning rate; the operative levers are per-bet vol (trade shape),
edge size ≥40–50 bps, or decorrelated sources. Config commitment remains an
owner decision; if committed, its forward record accrues correctly but
cannot adjudicate a 15–25 bps edge on a useful horizon.

## P2.1 squeeze-state feature set built with per-feature PIT audit (2026-07-20)

Claim: the R2 governor's raw material now exists as causally-lagged hourly
features from fields none of the 29 prior families used — a build, not a
result; no outcome column was read or joined. Groups over the spent
discovery window [2021-05-01, 2024-12-01) on `bybit_full_pit` (receipt with
hashes: `reports/tail-risk-program/p21-squeeze-features-2026-07-20/`): OI
change/acceleration (4.53M rows, 296 symbols), premium level/24h-change-z
(6.53M, 497), funding level/jump/extreme-share (878k, 497), melt-up/crash
breadth over the PIT-manifest universe (31,440 book-hours), taker-buy
imbalance (31k symbol-hours — sparse local 5m coverage from 2023-04, a
data-coverage fact P2.2 must respect). `positioning_lsr` is data-gated
(absent from the root; acquisition is a separate task). PIT policy enforced
uniformly (one-bar availability lag; funding strictly-after settlement;
breadth universe manifest-gated) and tested per group with future-mutation
invariance + same-bar leak tests. Non-conclusions: no squeeze-index design,
no relationship to outcomes examined, no governor claim — that is P2.2 on
the spent window, then the R2 registration path.

## R1 risk intensity: T-I revived under tail metrics, registered as shadow A/B (2026-07-20)

Claim: one monotone gross multiplier (linear trend ramp × monotonized
BTC-risk band, `linear10_ramp`) is a well-posed *priced-insurance* candidate
for the CONTINUOUS book — informing the P1.1 registration decision, not a
deployment. Data that shaped: V2 barebones ledger + T-A render books +
BTCUSDT klines (all seen; Lane-1); grading data: none yet — the forward
record starts at the registration commit. Scope: Bybit CONTINUOUS shape,
2021-05→2024-12 (barebones) and 2023-04→2026-07 (render), ledger-level
weighting (capacity proxy error bounded at −0.28pp by the gate_off×binary
vs rendered-gate-on check). Effects (full grids in
`reports/tail-risk-program/p11-r1-intensity-lane1-2026-07-20/`): on the
discovery surface the T-I Pareto story reproduces exactly under tail metrics
(equal net, maxDD −0.359→−0.308, ES95 −0.0164→−0.0146, tail-day losses
−1.13→−0.93); on the deployed-era render book it becomes a tradeoff —
**~3.8pp/yr net premium buys era-stable tail relief** (ES95 −23%, ES99 −19%,
native-tail-day losses −33%, maxDD unchanged; premium concentrated in the
late bull half: −1.4pp early vs −10.9pp late). The discrete-0.35 overlay is
nearly inert post-2023; the operative axis is binary→linear on trend.
Registered: `r1_intensity_v1` (shadow A/B vs deployed weights; hash-chained
daily scorer; kill criteria R1-K1 premium-runaway / R1-K2 tail-failure /
R1-K3 insufficient-divergence pre-committed). Non-conclusions: no alpha or
promotion claim; sixth-generation T-I descent priors apply; the linear
member's true render-arm interaction (capacity/admission) is untested; the
one-shot G1/G2/G3 slice grades remain unopened until after this commit.

## R3a daily loss budget: insurance profile confirmed on seen data; frozen A/B registered (2026-07-20)

Claim: a −1.5%-of-capital realized-day loss budget (entry-side block, UTC
reset) behaves like well-priced insurance on both seen surfaces — informing
the P1.2 registration, not an activation. Grading is item-27 insurance
metrics, never return improvement. Data that shaped: V2 barebones book
(LONG+CONT) + T-A gate_on render book. Effects (all 18 declared cells in
`reports/tail-risk-program/p12-r3a-loss-budget-lane1-2026-07-20/`): on
barebones-late — the bleeding regime — 30 triggers (16.7/yr), **3.3%
false-trip**, blocked entries went on to lose 2.4× what they forfeited
(−6.24% avoided vs +2.64% forgone); on the deployed-shape render book the
layer is nearly dormant (2.8 trips/yr, 6 blocked entries in 3.26y, ~0.18%/yr
pure premium, zero avoided in the bull window, ES essentially unchanged —
including a small honest negative: two render-late trigger days got
marginally redder). Sensitivity flankers (−1.0%/−2.0%) reported, not
selected. Registered: frozen A/B activation design (UTC-date-ordinal parity,
odd=A off / even=B on; experiment kill rules X1 premium-runaway / X2
trigger-famine / X3 integrity) in
`docs/preregistration/r3a_loss_budget_experiment_2026-07-20.md`; shadow
governor implemented + tested + staged only. Non-conclusions: no activation,
no return claim; realized-at-exit replay is an approximation of live
venue-time cash flow (the shadow phase measures the live analogue); render
surface lacks LONG.

## R3b cluster cap: zero-premium tail-day insurance — but only where the book stacks (2026-07-20)

Claim: the correlated-squeeze cap the 2026-06-20 study recommended is
well-posed structural insurance, and its binding regime does not exist on
today's deployed book — informing the P1.3 registration as a dormant guard.
Data that shaped: T-A gate_on render book (declared surface) + V2 barebones
ledger (labelled supplementary stacking surface; registered cell ρ≥0.7/K=3
was fixed before either ran). Method: trailing-720h hourly log-return ρ vs
open positions at entry (≥240 overlap; zero un-correlatable pairs on both
surfaces). Effects (grids + per-veto detail in
`reports/tail-risk-program/p13-r3b-cluster-caps-lane1-2026-07-20*/`): on the
deployed-shape book the cluster state ~never occurs (mean 1.11 open at
entry; 2/2,300 entries with ≥2 correlated opens; registered cap binds 0×) —
free but dormant. On the stacked book (mean 15.35 open): 2.66% veto rate,
forgone +0.1441 vs avoided −0.1440 (≈zero net premium over 3.6y), with
vetoed-entry losses concentrated exactly on common-loss days (−0.036 native
tail set / −0.021 registered set); era-stable tail concentration. Registered:
frozen cell + per-entry hash-parity A/B (A=shadow-veto, B=veto) + kill rules
Y1–Y3 including an explicit dormancy closure; decision layer staged. The
value case activates precisely under the breadth expansion the T-K
workstream wants. Non-conclusions: no deployment (wiring needs an operator
go); pairwise-to-open ρ is a crude cluster proxy; no capacity backfill in
counterfactuals; seen-data only.

## P0.2 slice provenance: D1's "untouched" claim corrected (2026-07-20)

Claim: of the two "never-opened" pre-2021-05 slices, only **Binance
[2021-01-01, 2021-05-01) is pristine** (no outcome or feature read by any
program, live or dead; 80→111 listed perps). Corrections found by
git-history archaeology (receipts quoted in
`docs/preregistration/untouched_slice_provenance_2026-07-20.md`): (a)
Binance [2020-01-01, 2021-01-01) **was outcome-graded on 2026-05-24** as the
V1 momentum-factor tri-root `binance_OOS_2020` gate (two preset baselines
recorded, Sharpe 5.68/6.38, plus creative-gate hypotheses; artifacts purged
in the 2026-05-27 reset; family killed, descends into nothing deployed); (b)
the Bybit slice is outcome-unread but **feature-touched end-to-end** (the V2
discovery `read_window` began 2021-01-01) with a 5–8-symbol universe that
in-slice warm-up mostly consumes. Grading windows are now frozen: G1
Binance-pristine (entries [2021-01-01→2021-04-30) CONT / 04-28 LONG), G2
Binance-2020 (dead-family caveat permanent), G3 Bybit (regime-evidence only,
never standalone). Commit-before-open ordering unchanged; the hypothesis
ledger carries the correction. Non-conclusions: no statement about the
reserved V2 label-level holdout (unread); no power claim — three thin months
under item-29 accounting is regime evidence, not per-name evidence.

## P0.1 1m re-simulation harness passes the exact-reproduction bar (2026-07-20)

Claim: a render-native 1m re-simulator now exists that reproduces recorded
CONTINUOUS exits exactly, so future registered variant work has a
granularity-honest instrument (the T-F standard extended to 1m). Result:
walking `bybit_render_1m` (2023-03-26→2026-07-09), **0 harness mismatches
across 23,064 recorded trades** — T-A `render_gate_on` 2,297/2,300 exact,
`render_gate_off` 4,008/4,019, V2 barebones 10,609 of the 10,614 in-window —
with 0.0 exit-price and mae/mfe diffs on every match. The 16 non-reproduced
paths are venue-surface divergences at listing/delisting edges, mechanically
attributed by bar-for-bar 1m-vs-raw-1h comparison (12 delisting tails where
the 1m tape ends hours before the 1h tape; 4 feed-content divergences, e.g.
SAHARAUSDT's first-day hour closing 0.012474 on 1m vs 0.013045 on 1h) —
quarantined from any variant analysis. 6,131 barebones trades predate the 1m
window or its render-universe symbols (enumerated, not walked). Data that
shaped: all inputs are seen surfaces (Lane-1 tooling; no new outcome surface
opened). Artifacts: `scripts/research_v3/resim_1m.py`,
`tests/test_resim_1m.py` (oracle regression, no-lookahead property, item-14
ambiguity policy, item-15 warm-state), receipt
`reports/tail-risk-program/p01-resim-1m-2026-07-20/`, scope/limits
`docs/resim_1m_harness_2026-07-20.md`. Non-conclusions: no exit variant is
thereby licensed (closed lines stand); no execution-cost model for intrabar
fills exists yet; Bybit 1m-vs-1h feed divergence at tape edges is a data
finding to respect in any future 1m work.

## P0.4 Binance backward backfill: frontier already reached (2026-07-20)

Claim: `binance_full_pit` cannot be extended backward through the canonical
builder — every dataset it owns already sits at its upstream origin. Probes
(HEAD + S3 listing + REST-earliest + local first rows, receipt with hashes at
`reports/tail-risk-program/p04-backfill-frontier-2026-07-20/`): Vision monthly
USD-M klines begin 2020-01 and the local root begins 2020-01-01; local funding
begins at the venue's first-ever settlement (2019-09-10 08:00 UTC); local
mark/index/premium first rows equal their REST-origin timestamps (2019-12-23 /
2019-12-24). The only unheld pre-2020 window is REST-only trade klines
2019-09-08→2020-01 (essentially BTCUSDT-only), outside the Vision-only kline
provenance contract; acquiring it would need a separate labelled root. Scope:
acquisition/coverage verification only — no outcome surface opened, no
statement about slice untouched-ness (P0.2) or strategy performance. The
proposal's D2 assumption ("backfill deeper") is closed as already-complete;
the tail-event library grows only via new fields (D4), a third venue (D5), or
forward days (D6).

## T-L conditional listing study, Bybit: calendar arm flips at 2024/2025; one conditional survivor (2026-07-20)

Claim: the unconditional new-listing short (d1/d2→d7) is dead — +226/+263
bp/trade net pre-2025 flips to −180/−335 bp post-2025, with funding turning
against shorts (−92 to −117 bp/trade; post-2025 listings carry negative
rates — the listing short is crowded). One declared conditioning cell
survives all robustness checks: the **turnover-collapse short** (entry-day
per-hour turnover < 30% of listing-day's), era-stable at both entry days
(d2: e2122 +247 n=9, e2324 +246 n=114, e2526 +510 n=116; ±169/±241
week-cluster s.e.), stable across the 0.2–0.4 threshold band, with a
mechanism-consistent post-2025 gradient (sustained-turnover shorts −520 to
−887). Permutation control: nominal p=0.015 vs random same-size cells, but
a random cell clears the raw +40 bp admission bar 26% of the time — every
other bar-clearing cell is recorded as scanned, not banked. Execution
reality (1m, 278 paired symbols, optimistic coverage): listing-week
rel-range 2.18× mature; 45 bp round trip realistic at demo scale; the cell
is not dead symbols ($4.8M/$17.8M median entry-day turnover). Shaped and
graded on the same seen data (Lane-1, 30th family); G1/G2/G3 and the
reserved holdout unread — event floor 2021-05-01 keeps them available to
grade a committed config later. Tail honesty: worst cell trade −195%;
implementation would need the book-level sizing/insurance layers.
**Cross-venue verdict: the cell failed the Binance pass** (same design,
659 events): negative in all three eras at d2 (−415/−41/−290 bp) with no
stable threshold region — only the population-level 2024/2025 flip
replicates across venues (Binance pre-2025 +105/+135 → post-2025
negative, funding −80/−96 bp against shorts). **T-L closed, no Lane-2
candidate** — the Bybit cell is a selected fluke or venue quirk; this
study cannot distinguish and neither is admissible. Artifacts:
`reports/strategy-research-v3/t-l/2026-07-20/` (evidence_card.md,
manifest.json, `binance/divergence_note.md`). Non-conclusions: long
mirror is noise (s.e. ~400+ bp); ≥5 bets/day bar unreachable (~45
events/yr); no Binance 1m cost read.

## T-M funding-extreme carry: inventory built; every carry arm fails the bar (2026-07-20)

Claim tested: extreme-funding episodes (settled 8h-equiv rate ≥ {0.15, 0.30,
0.50}%/8h, either sign; cadence derived from observed spacing, never the
stale `funding_interval_min` label) support a BTC-hedged carry clearing the
admission bar. Verdict: **no arm is era-stable ≥ +40 bp hedged on either
venue — family closed below bar.** Episodes resolve in hours (mean holds
2.5–16 h; the 14-day cap never binds), so collected funding (+12–145 bp
depending on sign) cannot amortize 45 bp/leg; at negative extremes the alt
price keeps falling while you collect (the paying crowd is directionally
right short-term). Binance pre-2025 hedged positives (+47/+86 bp at
negative extremes) die post-2025 AND have negative Bybit twins; two
Binance unhedged bar-clearing cells (n=28/n=3 pre-2025) fail the Bybit
cross-check — scanned, not banked. Durable deliverable: the episode
inventory (Bybit 84,761 / Binance 41,357 episodes, era × age × persistence
tables) quantifies the **post-2025 negative-funding regime shift**
(Bybit ≤−0.15%/8h: 3.5k/2.9k/28.3k episodes by era; Binance replicates) —
context for R2 squeeze-state design, concentrated in 30d+ symbols, not a
listing-week artifact. Entries floored at 2021-05-01 both venues to keep
G1/G2 unread (the queue's 2019-09→ Binance span was deliberately narrowed;
stated as a non-conclusion). Lane-1, 31st family. Artifacts:
`reports/strategy-research-v3/t-m/2026-07-20/` (evidence_card.md,
manifest.json, binance/). Non-conclusions: no intraday path stats
(missing, not zero); maker execution and perp-vs-perp basis untested.

## Tail-risk program adopted as main focus (2026-07-20)

By operator instruction the repository's main research focus is the book-level
tail-risk program. Per-trade price-exit research is closed on the spent
surface under both grading styles — alpha metrics (mechanism table above,
2026-06-18 exit-cause ablation) and tail metrics (2026-06-20 disaster-stop
study, commit `1fa7045`, receipt pruned/to be re-anchored) — and tail control
moves to the book level: continuous risk intensity (T-I revival under
ES-based metrics), a squeeze-state governor built from fields unused by the
29 prior hypothesis families (earmarked for the reserved `[2025-01-01,
2026-07-06)` holdout as the first non-descended family), and structural
insurance graded as insurance. The data program opens never-read pre-2021-05
slices, builds a 1m re-simulation harness on already-local paths, starts
forward liquidation/depth recording, and extends the backfill. Rationale and
receipts: `docs/tail_risk_overhaul_proposal_2026-07-20.md`. Execution state
and next actions: `docs/tail_risk_program.md`. The adoption itself authorizes
no runtime change.

## Progressive model adopted (2026-07-19)

By owner direction the repository operates under the Progressive Evidence
Model (`docs/governance.md`, rewritten this date): continuous Lane-1
exploration on seen data, rolling Lane-2 forward scoring where a config's
git commit is its registration, five-line promotion notes with recorded
change points, and no one-shot confirmatory ceremonies. Historical
registrations and receipts above remain unrewritten provenance. The
real-money owner boundary is unchanged. New decision-influencing work
follows `docs/parameter_pre_registration.md` (commit-and-note mechanics).
