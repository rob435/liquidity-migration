# Strategy Overhaul V2 — Diagnostic-First Plan

Status: closed on 2026-07-18 with no qualifying thesis. The completion receipt
is `docs/strategy_overhaul_v2_completion_receipt_2026-07-18.md`; the reserved
holdout remains untouched and no confirmatory experiment or implementation is
active.

Phases 0--4 are closed. Phases 5--6 were not activated because no thesis passed
the frozen economic/comparator gates. The corrected benchmark and diagnostic
cycle are historical inputs, not jobs to rerun.

The objective is to generate a small number of mechanism-based strategy theses
from causal trade diagnostics, then test them prospectively. The objective is
not to rebuild the retired overhaul, produce a large parameter atlas, or make
the existing curves look better.

## Non-negotiable boundaries

- Reuse the current strategy producers, account service/kernel, journal,
  sequence-aware market recorder, PIT builders, and lifecycle/accounting code.
- Do not create a parallel order lifecycle, artifact registry, receipt graph,
  data-lineage framework, or research supervisor.
- Paper remains integration-only. Demo observations are exact forward execution
  evidence, not automatic alpha evidence. Mainnet remains unauthorized.
- Signal-time features, entry anchors, execution facts, and future labels stay
  separately keyed until the analytical join.
- A strategy change may alter one declared mechanism at a time. Safety controls
  are evaluated as safety controls, not forced to improve an alpha metric.
- Treat the corrected 2026-07-17 benchmark as immutable, consumption-only
  evidence. Verify its files and hashes; do not reproduce it as housekeeping.
- Diagnostic writers are observers. With the writer enabled or disabled, active
  decision keys, ordering, targets, lifecycle, accounting, and numerical outputs
  must remain unchanged.

## Engineering and artifact stop-losses

These are budgets for this overhaul, not universal repository policy:

- Before the first claim-bearing diagnostic table, add no more than two
  production modules, one read-only entry point, and 1,500 net production lines.
- A run retains no more than the four payloads in
  `docs/trade_diagnostics.md` unless a preregistration names the extra consumer.
- Smoke work uses a bounded synthetic fixture or at most 100 commands/decisions.
  The next step is one resumable venue/time partition, not the full history.
- Baseline preflight is limited to identity/hash verification. A missing or
  mismatched pinned artifact stops the comparison; it does not authorize a
  replacement benchmark run.
- If a stage produces no decision-useful row within two measured compute hours,
  stop, preserve the failure, and reduce the claim or repair the blocker. Do not
  add orchestration around an unidentified result.
- Checkpoint at existing natural partitions. A failed broad run remains visible;
  a narrowed rerun is labelled as a different scope.

Crossing a stop-loss requires an explicit plan amendment before more code or
compute, including why the additional surface is necessary.

### Engineering-budget amendment — 2026-07-17

Phase-3 accounting found that the literal cumulative pre-table budget had
already been crossed by the closed Phase-2 implementation without a recorded
amendment. Phase 1 contributed 1,186 net package/developer-script lines and
Phase 2 contributed another 929, for 2,115 before Phase 3. That inherited breach
cannot be made prospective after the fact and must not be described as within
the original 1,500-line limit.

The Phase-3 user instruction requires the still-missing shared funnel contract
and one bounded, resumable candidate-tape export. Reusing the execution-TCA
module for strategy-source causality would merge distinct ownership and does
not remove the exporter. The prospective ceiling through the first candidate
table is therefore amended to three production modules, two read-only entry
points, and 3,850 cumulative net production lines across Phases 1--3. No fourth
module, third entry point, runner telemetry, cache framework, or generalized
research orchestration is authorized. The two-hour compute limit, one-partition
limit, payload limit, and failed-baseline comparison boundary are unchanged.

### Completion-cycle amendment — 2026-07-17

The first bounded partition passed its structural stop, and the owner has now
asked to complete the full plan without recreating the absent baseline. The
prospective continuation is
`docs/preregistration/strategy_overhaul_v2_completion_cycle_2026-07-17.md`.
It replaces the one-partition stop with 43 sequential calendar-month discovery
partitions, freezes a chronologically later holdout before any path value is
read, and names one aggregate analysis/evaluator entry point. It does not relax
the two-hour stage stop, four-payload analysis budget, immutable-baseline
boundary, PIT/timing/accounting constraints, or deployment authority.

The prospective engineering ceiling through the completed research cycle is
one additional read-only entry point and 5,500 cumulative net production lines
across Phases 1--6. The added surface must directly produce the diagnostic,
portfolio, thesis-test, or parity artifacts; no runner framework, artifact
registry, cache subsystem, or supervisor is authorized. The exact final line
accounting belongs in the completion receipt.

### Phase-3 replay recovery amendment — 2026-07-18

The first full evaluator passed every input identity but projected beyond the
two-hour stage stop because the Windows research adapter flushed every account
transaction. Its partial journal is preserved and no performance value was
inspected. The prospective recovery boundary is
`docs/preregistration/strategy_overhaul_v2_phase3_replay_recovery_2026-07-18.md`:
one exact retry may remove only those crash-durability flushes, must retain and
verify the complete account event/hash chain, and has a 45-minute stop so total
Phase-3 analysis compute remains below two hours.

The no-`fsync` retry remained too slow in dense periods and was stopped before
its cap without inspecting performance. A synthetic stress check then exposed
superlinear production state/hash cost that cannot fit the remaining stop. The
final prospective recovery is
`docs/preregistration/strategy_overhaul_v2_phase3_buffered_replay_recovery_2026-07-18.md`:
it freezes a key-only 100-trade sample per sleeve, buffers those exact kernel
transactions, and materializes their canonical files/projection once before
full verification. Full portfolio values remain model-based rather than
account-reconciled. Its 24-minute stop keeps all Phase-3 analysis attempts
cumulatively below two hours; no fourth attempt is authorized.

## Phase 0 — Rebaseline (closed)

Deliverable: `docs/research_rebaseline_2026-07-16.md`.

Exit criteria:

- current Git/code, selected Python, dependency lock, test discovery, active
  strategy/account ownership, deployed-commit boundary, local evidence, and
  research exposure are stated from primary repository sources;
- old Big-PC outputs are not treated as results;
- the missing diagnostic layer is distinguished from already solved runtime
  architecture.

Status: complete for repository planning. Deployment parity remains a separate
operational task. Do not rerun Phase 0 from this plan.

## Phase 1 — Execution diagnostic MVP

Build a read-only projector from verified journal transactions plus exact
decision books into one row per canonical venue command. Preserve partial fills
in the source journal and aggregate them by absolute quantity for command-level
TCA.

Required first slice:

- lineage through decision/target/risk/command/ACK/fill/status;
- spread, depth, imbalance, microprice, order/depth, and visible book-walk VWAP;
- fill ratio, VWAP, fees and fee provenance;
- clock-domain-safe lifecycle timings;
- arrival shortfall, effective spread, predicted walk, and residual cost;
- null reasons and source identities.

Also retain the documented Bybit execution fields currently discarded by the
normalizer: maker state, fee rate/currency, execution type/value, order/leaves
quantity, and private sequence.

Exit criteria:

- deterministic synthetic journal/capture fixtures reproduce exact formulas;
- corrupt journals, duplicate/mismatched contexts, crossed/gapped books,
  inconsistent fill directions, and mixed-clock metrics fail or stay explicitly
  unavailable;
- the output is a single deterministic command table plus one manifest;
- focused tests, Ruff, and mypy pass.

Status: complete in the current `main` implementation. The command table
remains diagnostic only and has not been built from a registered real epoch.
From the rebaseline base through the Phase-1 commit, package and developer-script
changes were
1,218 additions and 32 deletions, or 1,186 net, inside the 1,500-line pre-table
stop-loss.

## Phase 2 — Bounded forward observability

Add only the observations the canonical sources cannot currently answer:

1. bounded post-fill book contexts at 1 s, 15 s, 1 min, and 5 min, recording the
   requested and actual horizon;
2. one pre-gate decision-funnel row per declared source-population key, with
   first rejection and missingness;
3. separate path labels at the horizons required by a named sleeve question.

Do not turn bulk raw-market persistence back on merely to obtain four markouts.
A bounded sampler may skip safely when the owner is restarting, the book is
unhealthy, the symbol is unsubscribed, or the observation is late; each miss is
data, not a zero.

Exit criteria:

- capture overhead is measured on a synthetic/bounded workload;
- owner health, memory bounds, order ownership, and account mutation paths are
  unchanged;
- every observed/missing mark is reconstructable and no strategy target depends
  on a future label;
- a deployment is not implied; operational authorization remains separate.

Status: the bounded demo markout path, command-table join, and observer-only
pre-gate funnel/path-label projector are implemented in the current `main`
implementation. They preserve actual lag and explicit
missingness, keep capture I/O off the private fill-accounting path, and retain
symbols only while
bounded tasks are pending. Per-owner-loop registration work and per-book-update
mark writes are each capped at 128. The funnel is not deployed and no runtime
writer is configured. Nothing in this phase is deployed.

Bounded engineering measurement, not an SLA: five local Python 3.13.5 runs on
2026-07-16 registered 100 synthetic fills and persisted 100 schedules plus 400
markout records with raw-market persistence disabled and
`fsync_every_records=250`. Median deferred registration time was 19.7 ms,
median capture time across all four horizon batches was 79.6 ms, median total
time was 98.8 ms, median bytes written was 363.1 KiB, and maximum traced Python
allocation was 0.60 MB. Every run emitted all 400 marks and left no symbol
pending. This measures local code/storage overhead only; it says nothing about
venue delivery, production load, or deployment safety.

## Hash-pinned historical comparison baseline

The current-profile reference is
`docs/strategy_overhaul_v2_baseline_2026-07-17.md`. It fixes the four Bybit and
Binance LONG/CONTINUOUS curves, reports, and complete trade ledgers for
`[2023-07-17, 2026-07-17)` by exact artifact hash after the exact-settlement
funding, stable-RMOM-key, and chronological terminal-tape corrections. The
2026-07-16 baseline is a superseded historical receipt because its CONTINUOUS
net returns used invalid modal-cadence funding. This is the historical
comparison target for V2, not a promotion gate or a claim that the exposed
window is confirmatory.

Phase 3 consumes this baseline in place. Its preflight verifies the pinned
commit, config identities, file presence, and hashes; it does not invoke
`research-refresh`, `equity_curves.sh`, or either active strategy backtest. The
existing active ledgers and curves are the reference side of the exploratory
comparison.

The new funnel/barebones implementation may be compared with that reference
only while it is observer-only. Focused tests must prove that enabling the
diagnostic writer leaves active decision keys, ordering, targets, lifecycle,
accounting, and continuous numerical outputs unchanged on deterministic
fixtures. The implementation diff must not change the PIT universe, profile,
component configs, take-profit, timing, fills, costs, funding, capacity,
accounting, modeled exposure, or presentation.

If shared strategy semantics or any baseline identity changes, stop and amend
the plan before viewing affected outcomes. First attempt focused equivalence;
only an unresolved affected cell may justify a targeted control replay. A
routine four-cell benchmark refresh is not a Phase-3 prerequisite and must not
be used as a comfort check.

## Phase 3 — Exploratory diagnostic read

Before pulling outcomes, register the exact account/capture roots, journal hash
boundary, time interval, prior exposure, tables, diagnostic splits, and artifact
paths. This phase is explicitly exploratory and spends its data.

Read in this order:

1. **Integrity:** lineage coverage, context coverage, clock ordering,
   missingness, sequence health, and accounting agreement.
2. **Selection:** source population, gate attrition, repeated components,
   capacity/risk rejection, and concentration by unique decision and wave.
3. **Execution:** shortfall decomposition, fill/reject behavior, latency, depth
   consumption, markouts, and calibration residuals by predeclared liquidity,
   size, side, and urgency buckets.
4. **Path:** MAE/MFE and fixed-horizon outcomes joined only after signal-time and
   entry tables are frozen.
5. **Portfolio:** synchronized losses, symbol/day concentration, component
   overlap, hedge interaction, funding, and tail contribution.

The first diagnostic read has exactly two strategy representations per sleeve:

1. **Active reference:** the already generated, hash-pinned current-profile
   ledgers and curves above. Read and link them; do not regenerate or copy them.
2. **Barebones source-population comparator:** one fixed, minimally filtered
   causal candidate family designed to expose enough observations to measure
   gate attrition and trade characteristics. It is a diagnostic population, not
   a candidate strategy or deployment proposal.

The barebones representation produces two distinct artifacts. An unweighted
candidate tape retains every causal source event and its gate pass/fail states,
first rejection, missingness, fixed-horizon outcomes, MAE, and MFE. A separate
portfolio curve applies a deterministic collision/capacity rule, fixed sizing,
and the same lifecycle, cost, funding, fill, and accounting model as the active
reference. Candidate-level inference must use the tape; capacity-selected
portfolio trades are not a substitute for the larger source population.

Before outcomes are read, the diagnostic-epoch contract must freeze the exact
barebones rules. The intended starting definitions are:

- **CONTINUOUS:** retain PIT tradability, the closed-bar decile-9 short source
  event, one-hour causal confirmation, and a predeclared executable-liquidity
  floor. Record but do not gate on the BTC trend, residual-momentum quartile,
  240-day/event subtype, or component-specialization filters.
- **LONG:** retain PIT tradability, the active pump trigger, causal
  retrace/deadline entry anchor, signal freshness, and a predeclared
  executable-liquidity/history floor. Record but do not gate on BTC/ETH regime,
  top-volume rank, close location, ATR cap, weekend multiplier, or adaptive
  volatility/BTC sizing filters.

Safety and feasibility constraints are not relabelled as alpha filters. If a
constraint is required for an executable portfolio, it remains in the
portfolio curve and is still recorded on the unrestricted candidate tape. The
liquidity/history floors, collision rule, fixed size, path horizons, and exact
gate list must be selected from causal/execution requirements and written into
the epoch contract before any barebones outcome is inspected.

There is one barebones arm per sleeve: no filter ladder, threshold sweep, or
post-result redefinition. Predeclared characteristic families are signal
strength, close location, volatility/ATR, turnover/liquidity, listing age,
BTC/ETH regime, residual momentum, event subtype, and available execution-cost
features. Analysis reports unique-decision, simultaneous-wave, and calendar
block support with block-aware uncertainty. Naive row counts, overlapping
components, or two venues are not treated as independent samples. Any ranking
of “best” characteristics on this already exposed window is exploratory and
must be tested on a prospectively untouched temporal surface before a strategy
claim.

The old untested `C-H1`, `C-H2`, `L-H1`, and `L-H2` estimands are priors only.
They receive no privileged slot. Diagnostics may support revisiting one, refute
its premise, or motivate a different mechanism.

Exit criteria: a compact evidence card lists baseline hash verification,
referenced active counts, new barebones counts, the full gate funnel, observed
failure modes, effect sizes, block-level uncertainty, concentration, all
inspected variants, and explicit non-conclusions. It publishes the new
candidate tape, barebones ledger, and barebones curve, and links the existing
active curves/ledgers without regenerating them. No rule changes occur in this
phase.

Status: complete. The 43-month Bybit discovery, four-payload analysis, and
independent checks are recorded in
`docs/strategy_overhaul_v2_completion_receipt_2026-07-18.md`. Active comparison
remained disabled because all 23 pinned payloads were absent. Full portfolio
values are model-based; exact account/event verification is the prospectively
bounded 100-key sample per sleeve after exhaustive replay crossed the compute
stop. The reserved holdout remains untouched.

## Phase 4 — Thesis selection

Select at most one thesis per sleeve and at most two total in the first cycle.
A thesis qualifies only if it:

- names a causal or economic mechanism, not a symbol/time blacklist;
- changes one lever relative to the current profile;
- has sufficient source-population support at the unique-decision/wave grain;
- can be represented with information available at decision time;
- has a plausible executable benefit after observed costs/capacity;
- states a result that would cause us not to implement it.

Rank candidates by expected decision value, identification quality, and cost of
testing—not by the best exploratory return or t-statistic. Record every
candidate considered so the tested set cannot disappear later.

Status: complete with no selection. Every LONG 24h effect is below the frozen
45 bp cost hurdle. CONTINUOUS has exploratory leads but lacks the exact active
comparator because residual-momentum provenance is invalid. The artifact's raw
LONG eligibility flag omitted the economic gate; the completion receipt applies
the registered rule and records the implementation correction.

## Phase 5 — Prospective test

Create one compact preregistration per selected thesis before inspecting its
evaluation outcome. It must freeze:

- exact claim, intended code/runtime action, current-profile comparator, and
  complete variant set;
- venue/population/root, `[start, end)` or sequential stopping rule, prior
  exposure, and untouched surface;
- unique decision, simultaneous wave, and calendar/event block definitions;
- primary effect, uncertainty method, guardrails, multiplicity treatment, and
  supports/contradicts/inconclusive rule;
- PIT, timing, fill, cost, funding, capacity, accounting, and reconstruction
  requirements;
- the minimal artifact set and compute/checkpoint plan.

Bybit and Binance are separate correlated robustness surfaces when the claim
needs both; they are never pooled as independent replications. A historical
diagnostic cannot silently become forward confirmation.

Exit criteria: apply the frozen rule once, append every completed/failed/aborted
cell, and issue the evidence card. An inconclusive result stays inconclusive.

Status: not activated. No thesis-specific contract was created and no holdout
label was generated or read.

## Phase 6 — Runtime parity, only after support

For a supported thesis, implement the smallest profile change and compare
decision keys, targets, lifecycle, and accounting against the registered model.
Phase 6A is offline/shadow parity and belongs to this research cycle. Phase 6B
is a forward demo/paper execution epoch and remains a separately authorized
operational contract naming deployment and risk boundaries. A negative,
inconclusive, or no-qualifying-thesis cycle has no implementation to parity-test.
Promotion, sizing, and mainnet require their own evidence and owner authority.

Status: not applicable. There is no supported thesis to implement or
parity-test; Phase 6B remains separately unauthorized.

## Deferred infrastructure work — not a Phase-3 prerequisite

The corrected four-cell refresh took 9,163 seconds of measured stage time, or
about 2.55 hours. CONTINUOUS reconstruction dominated at 2,392 seconds on
Bybit and 2,448 seconds on Binance. Full RMOM rewrites took 627 and 608 seconds;
ancillary tails took 308 and 981 seconds. These are measurements of this local
run, not universal budgets.

General runner telemetry, shared-panel optimization, RMOM partition benchmarks,
ancillary negative-cache work, and concurrency changes are deferred. Do not
implement them while the first diagnostic table is still missing. Reopen only
the specific bottleneck if the new candidate/funnel path reaches the two-hour
stop-loss, and preserve exact decision keys and declared numerical tolerances.

Generated research payloads stay under ignored run-scoped `reports/`; their
small manifests and hashes are the durable control. The obsolete Big-PC branch
is not a performance cache: its 1.4 GB audit bundle and Windows adapters are
documented in `docs/account_kernel_binance_combined_audit_2026-07-17.md` and
must not be merged into `main`.

## Completion record

The former immediate queue is complete. No baseline, market data, RMOM, active
backtest, or equity artifact was regenerated. The cycle produced the registered
candidate tapes and four-payload analysis, applied the selection rule once,
found no qualifying thesis, and left the holdout untouched.

Future work is a new cycle, not a continuation of this queue. The most useful
lead is a prospective repair of CONTINUOUS residual-momentum provenance before
constructing an exact current-profile comparator. Cost/funding mechanisms may
also be investigated on genuinely untouched data. A real demo/paper epoch
still requires separate owner authorization; mainnet remains unauthorized.
