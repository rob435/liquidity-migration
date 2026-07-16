# Strategy Overhaul V2 — Diagnostic-First Plan

Status: active engineering/research plan; no alpha thesis or confirmatory
experiment is active yet.

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

## Engineering and artifact stop-losses

These are budgets for this overhaul, not universal repository policy:

- Before the first claim-bearing diagnostic table, add no more than two
  production modules, one read-only entry point, and 1,500 net production lines.
- A run retains no more than the four payloads in
  `docs/trade_diagnostics.md` unless a preregistration names the extra consumer.
- Smoke work uses a bounded synthetic fixture or at most 100 commands/decisions.
  The next step is one resumable venue/time partition, not the full history.
- If a stage produces no decision-useful row within two measured compute hours,
  stop, preserve the failure, and reduce the claim or repair the blocker. Do not
  add orchestration around an unidentified result.
- Checkpoint at existing natural partitions. A failed broad run remains visible;
  a narrowed rerun is labelled as a different scope.

Crossing a stop-loss requires an explicit plan amendment before more code or
compute, including why the additional surface is necessary.

## Phase 0 — Rebaseline

Deliverable: `docs/research_rebaseline_2026-07-16.md`.

Exit criteria:

- current Git/code, selected Python, dependency lock, test discovery, active
  strategy/account ownership, deployed-commit boundary, local evidence, and
  research exposure are stated from primary repository sources;
- old Big-PC outputs are not treated as results;
- the missing diagnostic layer is distinguished from already solved runtime
  architecture.

Status: complete for repository planning. Deployment parity remains a separate
operational task.

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

The old untested `C-H1`, `C-H2`, `L-H1`, and `L-H2` estimands are priors only.
They receive no privileged slot. Diagnostics may support revisiting one, refute
its premise, or motivate a different mechanism.

Exit criteria: a compact evidence card lists observed failure modes, effect
sizes, block-level uncertainty, concentration, all inspected variants, and
explicit non-conclusions. No rule changes occur in this phase.

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

## Phase 6 — Runtime parity, only after support

For a supported thesis, implement the smallest profile change and compare
decision keys, targets, lifecycle, and accounting against the registered model.
Use offline/shadow first, then a separately authorized demo/paper epoch for
execution agreement. Promotion, sizing, and mainnet require their own evidence
and owner authority.

## Immediate work queue

1. Finish the Phase-1 command-level projector and Bybit metadata retention.
2. Run it only on deterministic fixtures until a real diagnostic-epoch contract
   binds the VPS roots and exposure boundary.
3. Design the bounded markout sampler and pre-gate funnel at their existing
   owners; reject any design that requires a parallel event platform.
4. Register the first exploratory epoch, export canonical read-only evidence,
   and produce the diagnostic evidence card.
5. Only then write the first alpha thesis preregistration.
