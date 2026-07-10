# Strategy Overhaul Population Scout

Date: 2026-07-10
Mode: exploratory
Owner: strategy research
Status: S00 tooling/templates and outcome-blind stage hardening implemented and
synthetically validated; one historical local-root S00 returned `NOT_READY`,
while current repaired-root and big-PC S00 evidence remains unrun, and this is
**not yet an executable outcome contract**

Planner/inventory: `scripts/strategy_overhaul_scout_2026_07_10.py --plan` and
`--phase0-inventory`

## Claim And Permitted Decision

The selected-trade ledgers cannot characterize the populations from which LONG and
CONTINUOUS select. They delete most gates before candidate emission, count heavily
overlapping component rows, omit important paths, and mix alpha, execution, state,
and risk assumptions.

A pre-filter population tape can expose support, missingness, overlap, conditional
associations, path shape, and exact current-rule reconstruction. It **cannot** by
itself identify a gate's causal marginal contribution. This is especially true for
cooldown, capacity, crowding, component order, sizing, and portfolio state, where
changing one rule changes later eligibility.

The only permitted output is a bounded hypothesis dossier and a new prospective
experiment contract. The scout cannot:

- label a rule retain, remove, replace, or promote;
- select a threshold, model, entry, exit, or portfolio;
- increase size or reset a forward clock;
- amend or reinterpret another active experiment;
- deploy or enable real money.

A null or unstable conditional association defaults to `unidentified`; it does not
prove that a gate has no value. A positive association generates a hypothesis, not
a trading rule.

## Why The Existing Evidence Is Insufficient

The archived CONTINUOUS candidate tapes start after stable-RMOM joining, q-filtering,
conditional decile construction, event triggering, freshness, and liquidity. Across
four old stage-0 cells they cover only 6,625 unique Bybit and 8,236 unique Binance
symbol/timestamp decisions. They cannot answer what the upstream population did.

The selected CONTINUOUS object has 2,279/2,152 component rows but only 896/872
unique Bybit/Binance decisions; 626/561 decisions appear in all three components.
The predicates are nested: `turn4_pop5` implies `turn4_pop3`, which implies
`turn3_pop3`. With weights 4/9, 2/9, and 1/3, this is substantially one event with
notional steps of 1/3, 5/9, and 1.0, not three independent alphas.

The active score uses only `max_ret168`, which includes the current one-hour pump;
the trigger thresholds the current pump again. Whether production D9 adds
information beyond event strength and turnover remains unidentified.

LONG retains roughly 190 executed FC trades per venue. About 26 take-profit exits
per venue materially affect the result, while MAE/MFE are explicitly unmeasured.
The selected ledger cannot show gate attrition, common-anchor entry economics, or
hourly first-passage geometry for rejected events.

## Active-Contract Isolation

This program is separate from both active CONTINUOUS experiments:

- `continuous-tail-survival-2026-07-10.md` remains the frozen budget-only matrix.
  The scout cannot add cells, rescue a result, change its decision rule, or use the
  scout's exit views to reinterpret it.
- `continuous-granular-adverse-risk-2026-07-10.md` owns the registered OI, premium,
  taker-flow, adverse-pulse, null-tape, and granular-readiness study. Tier-C fields
  remain outside the initial population scout.

Every revealed scout view is appended to the exposure ledger before a later
contract is written. Tail-survival remains independently interpretable under its
own frozen rule.

## Evidence Surface

- CONTINUOUS signal window: `2023-04-01` through `2026-07-09` UTC inclusive.
- CONTINUOUS causal read warmup begins `2023-02-23` (888 hourly observations);
  warmup rows are never output decisions.
- LONG signal window: `2023-06-15` through `2026-07-09` UTC inclusive.
- LONG causal read warmup begins `2023-03-16` (90 prior calendar days plus the
  signal-day observation); warmup rows are never output decisions.
- Signal end: `2026-07-10` exclusive.
- Proposed 72-hour label boundary: `2026-07-14` exclusive. This boundary is not
  authorized until the child label contracts are frozen.
- Prior exposure: the entire historical window is spent. Cross-fitting measures
  internal stability only; it does not manufacture OOS evidence.
- Row key: `(venue, symbol, decision_ts_ms)`.
- Strategy-decision key: `(venue, symbol, signal_ts_ms)` after component
  de-duplication.
- CONTINUOUS dependence: event wave plus overlapping calendar block and symbol.
- LONG dependence: daily signal close plus overlapping calendar block and symbol.
- Venue-scoped rows require a versioned venue-local identity map and may keep
  symbols distinct across venues. Cross-venue matching requires a separately
  reviewed portability map. Raw ticker equality is not authority across
  migrations, multipliers, or contract lifecycle changes.

## Data Tiers

Readiness is claim-specific and independently stamped:

- **A0 — primary scout:** daily PIT membership/provenance, causal 1h OHLCV, feature
  history, stable-RMOM provenance, and ideal price paths. Missing RMOM stays as a
  flagged row. A0 does not require funding.
- **A1 — costed outcomes:** A0 plus complete settlement-level funding. Decision-time
  funding means only the latest rate published by the decision; subsequently
  realized funding is an outcome.
- **B — path refinement:** complete 5m/1m price data. Existing sources do not provide
  a complete historical mark/index plane; do not request or imply one.
- **C — ancillary state:** fixed-schema OI, premium, taker flow, positioning, or
  depth fields only under a separate registered contract. No “any available field”
  menu is permitted.
- **D — forward execution:** actual order-book and lifecycle telemetry under a
  separate fixed forward epoch.

Missing A1-D data limits only the affected claim. Sub-hour fields are never
synthesized from 1h bars.

## PIT And Timing Semantics

The archive manifest is not universal hourly-tradability authority. Required
provenance fields are:

- `manifest_date`, `membership_source`, and `membership_inferred`;
- `first_archive_observed_date`;
- `reported_launch_time`, when present;
- source-specific limitation and coverage state.

LONG daily membership keys use the trading day represented by
`date(signal_ts_ms - 1ms)`. Hourly CONTINUOUS membership uses the kline-stamp date.
Listing-age variants are reported separately; root-first-bar and venue-reported
launch are not silently equated. LONG `symbol_age_days` is derived from the
first eligible daily kline row in the complete supplied root-key history before
the PIT-membership gate and causal-read floor, matching the production feature
builder. A warmup-window first row is not an acceptable age anchor.

Production and diagnostic ranks remain distinct:

- full-population rank is a new diagnostic with its own rankable-peer denominator;
- production rank joins stable RMOM, drops missing/provisional rows, applies q25,
  and ranks `max_ret168` inside that surviving population.

Every rank emits peer count, missing count, tie rule, and denominator. A singleton
guard is not evidence of adequate cross-sectional support.

With 1h OHLC, a threshold touch is interval-censored to a bar. Exact first-passage
time and within-bar ordering require Tier B. LONG's current retrace fires only when
an hourly **close** is below the threshold and uses that close as entry. Emit
`close_trigger`, `intrabar_low_touch`, and `current_engine_entry` separately.

## Phase 0 — Outcome-Blind Feasibility

Before any forward label is calculated or inspected, run only:

1. root/manifest/feature availability and provenance counts;
2. complete proposed S02/S03/S04 schemas, dtypes, units, null semantics, key and
   timestamp conventions, plus an explicit builder-mismatch ledger;
3. estimated rows, storage, peak memory, runtime, and partition/checkpoint plan;
4. current LONG and CONTINUOUS object/config/source inventory;
5. exact signal-time candidate/classifier/component and first-rejection
   reconstruction design; stateful admission and post-signal entry are separate;
6. proposed support thresholds and dependence blocks based only on counts;
7. a versioned venue-local identity-map coverage report plus a separate
   cross-venue portability-readiness status;
8. deterministic inputs for instantiating the separately reviewed
   CONTINUOUS-A0 and LONG-A0 templates in S01.

Phase 0 must not reveal future returns, MFE/MAE, first passage, trade PnL, or a gate
ranking. Its output can change feasibility/schema choices without spending outcome
labels.

### Current Synthetic Implementation Boundary

The following code behavior is implemented and covered by focused synthetic
checks; none of it represents a real-root artifact:

- The CONTINUOUS raw builder accepts only the raw hourly OHLCV projection plus
  caller-supplied venue-local identity fields that it validates locally, and
  restarts every row-window feature after an
  interior hourly gap. The diagnostic S02 wrapper requires one independently
  supplied exact source-key inventory covering warmup plus signal rows and a
  second exact retained signal-window population inventory, together with an
  exact stable-RMOM source carrying source day and explicit provisional state,
  plus normalized manifest/map inputs. For non-provisional RMOM[D], S02 derives
  `rmom_data_available_ts_ms` as `D - 1 day + 1 hour` from the frozen
  shift-3 forward-target construction; provisional rows remain null and
  unavailable. Stable/rankable RMOM without explicit non-provisional provenance,
  or whose derived causal-computability time is after the decision, fails closed.
  This is an offline causal information boundary, not an actual historical
  publication, ingestion, or operational-latency timestamp. The inventories and
  RMOM source/root provenance are not yet authoritative or receipt-bound.
- CONTINUOUS S03 and S04 are separate exact registered projections. S03 reads
  only the frozen next-close anchor; S04 verifies that anchor against the hourly
  grid and emits only the minimal frozen returns, excursions, and explicit
  completeness/censoring fields.
- Every LONG stage requires the mechanically derived runtime v11a config.
  Signal keys are canonical and grid-checked; hourly keys are validated
  globally; consumed OHLC is finite, positive, and geometrically valid; the
  daily close must match the exact signal-hour close; and S03/S04 re-derive
  upstream exit/anchor geometry so mutated stage values fail closed. The
  registered S04 path accepts only the frozen horizons; arbitrary horizons use
  a distinct exploratory interface and schema version. The S02 orchestrator
  validates independently supplied population/age and PIT/map inputs. A separate
  exact outcome-blind builder mechanically derives availability, BTC/ETH regime,
  and configured BTC-month sidecars from raw hourly OHLC, preserves unavailable
  context, and parity-checks production fallbacks. The orchestrator recomputes
  rank metadata and emits the exact 138-field projection. S03 and S04 are separately materializable
  exact 30- and 71-field projections; S04 joins only the S02 geometry needed to
  reconstruct and verify S03 before labeling.
- The shared projector enforces registered column order, dtypes,
  non-nullability, and unique/non-null keys. Proposed registry v4 distinguishes
  `builder`, `passthrough`, `adapter`, `projection`, `missing`, and
  `semantic_mismatch`; no current field remains in the last two states. Six
  receipt/provenance blockers remain explicit. The registry remains proposed,
  not canonical.

The remaining blocking debts are semantic identity and provenance, not missing
projection code. Supporting builders can now produce config, map, PIT, RMOM,
and LONG-sidecar artifacts plus canonical sorted `source_keys.jsonl` and
`expected_population.jsonl` with a strict receipt. Only the full-reconstruction
result, after rechecking the receipt-bound config/root/PIT/full manifest-pair/map
content, can enter either S02 builder. A separate Parquet/Arrow semantic verifier
checks the current registry, registered scope, config exclusions, exact S02
population and LONG ages, selected S02-S04 invariants, and transitive parent
identities. No real run has yet proved that complete, registered inputs flowed
transitively into any stage.

The root-snapshot utility is explicitly `BYTE_SNAPSHOT_ONLY`: it hashes selected
file bytes but does not verify registered scope, earliest-history coverage,
Phase-0 semantics, or S01 readiness. The diagnostic stage byte-binding utility
allows S00 to bind only config/source/environment before future S01 inputs exist.
S01 adds root/PIT/map/population identities and begins the stable downstream run
identity. Artifact schema, row count, key hash, and outcome blindness in that
generic receipt remain explicitly unverified caller declarations; non-config
identity JSON semantics are also unverified. Construction does require exact
equality with the repository-derived canonical config identity. Archival byte
verification uses the recorded schema/config identities and does not reinterpret
them through the mutable current registry or factories. The generic utility does
not validate key/window semantics or transitive contents and is not a canonical
semantic receipt or an immutable S00-S04 evidence chain.

The derived RMOM time proves only when the frozen formula is causally computable;
it does not prove which root row was consumed or measure refresh latency. The
wrappers and semantic verifier validate supplied values, formulas, and selected
relations but cannot authenticate who produced every input. Stage-specific
validators now exist prospectively, but a real canonical transitive identity
chain and the six registered provenance debts must still be resolved before a
canonical S01 freeze or outcome-bearing stage; synthetic fail-closed behavior
and self-consistent hashes cannot substitute for them.

## Child Contract Requirements

Each sleeve gets a separate run ID and contract before feature or outcome execution.
The child contract must freeze:

- canonical config JSON and SHA-256, component configs, gate order, source-function
  hashes, feature definitions, costs where relevant, and state/admission semantics;
- clean-worktree policy, environment lock, semantically verified root/scope/history
  receipts, run-ID construction, and exact artifact inventory; a
  `BYTE_SNAPSHOT_ONLY` precursor cannot fill this requirement;
- full schema/dtypes/null semantics and every availability timestamp, including
  the derived RMOM causal-computability semantic and its explicit non-claim about
  publication, ingestion, and operational latency;
- strict root-adapter input and output projections. Unknown/pass-through columns
  are refused, so a caller cannot smuggle an outcome into a pure annotation
  primitive;
- population key, PIT/membership key, rank/tie rules, spell/cluster algorithms, and
  duplicate handling;
- exact signal-time candidate/classifier/component and first-rejection
  reconstruction tests; separate entry-policy parity where applicable;
- label anchors and the finite label set;
- every analysis formula, feature, transform, bin edge, interaction, subgroup,
  time block, purge/embargo, bootstrap/cluster rule, negative control, and seed;
- minimum row/symbol/independent-block support and missingness limits;
- a hard cap of at most two advanced hypotheses per sleeve and the selection rule.

Any undefined “stable enough,” “adequate support,” or optional model family leaves
the child contract unready.

## CONTINUOUS-A0 Feature Tape

Retain every manifest-covered symbol/hour with a fully closed bar and explicit
feature availability. Do not filter on RMOM, q25, D9, current component trigger,
liquidity, age, BTC state, crowding, cooldown, capacity, or sizing.

Required feature groups are finite:

- provenance and timestamps from the PIT/timing section;
- `ret1`, `ret72`, `ret168`, `rv_168h`, `max_ret168`, `vov`, `dist_low`, prior-six
  maximum return/close, giveback, turnover 1h/24h, turnover spike, and z-score;
- stable RMOM value/source day/provisional state, derived causal-computability
  availability, and peer counts; actual publication/ingestion time is not claimed;
- full-population diagnostic ranks and exact production q25/D9 ranks;
- exact p3/p4p3/p4p5 predicates, component bitmask, stepped implied weight,
  production spell head, and explicitly defined pump/event-wave cluster IDs;
- static current gate flags available at the decision;
- no cooldown/capacity/held-book admission replay in the signal-time tape. Any
  later state parity must name either the research or live object, freeze prior
  exit-path semantics, and remain a parity diagnostic rather than a gate-effect
  estimand;
- causal BTC/ETH returns/volatility, alt breadth, and dispersion from fixed Tier-A0
  fields only.

Feature-only output is partitioned by venue/month. Missing RMOM or features remain
visible. No unit return or path label is written until feature parity passes.

## LONG-A0 Feature Tape

Retain every manifest-covered symbol/day with explicit feature availability. Do not
filter on top-50 universe, age, BTC/ETH regime, top-ten turnover, sigma trigger,
close location, ATR, cooldown, or capacity.

Required feature groups are finite:

- PIT/timing provenance;
- raw 1d/3d/7d/intraday return, daily sigma, standardized pump, 1d/3d/7d close
  location, turnover/rank, ATR, realized vol, listing-age variants, coin returns,
  and BTC/ETH trend distances;
- independent static gate flags, source-ordered first rejection, overlapping
  1d/3d/7d trigger identity, and exact current classifier result;
- signal-time ATR exit percentages and fallback state, without anchor-relative
  stop/target price levels.

No bar after the signal timestamp is read or written by the feature stage. The
exact current 1% close-based scan and anchors are outcome-bearing entry-policy
labels, not feature inputs. Alternate 0.5%/2% retraces remain deferred rather than
being smuggled into either stage.

## Minimal Label Contracts

After signal-feature parity, each child contract may append only the initial
minimal labels. LONG first writes a separate entry-policy table containing the
common next-close anchor and exact current 1% close-trigger/six-hour-fall-through
anchor, reason, hour, price, prefix completeness/missing hours, and clearly
non-fill intrabar-low touch. It must not expose the raw h1..h6 close/low arrays.
Only after entry-policy parity may the path-label stage append:

- common next-executable-close underlying return at 1h, 24h, and 72h;
- MFE and MAE through 24h and 72h, with 1h interval censoring and completeness;
- for LONG only, the same labels from the exact current close-based retrace/
  fall-through anchor and a paired entry-policy indicator;
- OHLC same-bar ambiguity and missing-path reason.

Signal features, entry-policy labels, and path labels are separate keyed artifacts;
they are never silently appended in place. No costs, funding, 280-cell
TP/hold/cost surface, alternate ATR grid, sizing,
execution model, hedge, or portfolio projection belongs to the initial label pass.
Those require separate contracts after the first hypotheses are logged.

## Finite Initial Analysis

The exact analysis manifest is written outcome-blind in the child contracts. Its
scope may include only:

1. support/missingness and gate/intersection attrition;
2. exact signal-time candidate/classifier/component and first-rejection
   reconstruction, followed by separate LONG entry-policy parity;
3. CONTINUOUS component overlap and unique-decision de-duplication;
4. a small enumerated set of univariate calibration plots with frozen bins;
5. LONG common-anchor versus current entry-policy selection;
6. fixed negative controls;
7. calendar-block and symbol/event-wave dependence-aware uncertainty.

Same-hour clustering alone is insufficient for overlapping 72-hour labels and
persistent regimes. The child contract must freeze calendar-block length or a
multiway dependence method before labels. Venues are correlated robustness surfaces,
not independent replications.

All attempted views and null results are preserved. The dossier may advance at
most two hypothesis families per sleeve. Each family is labeled separately by
venue, using:

- `hypothesis_positive`;
- `hypothesis_negative`;
- `unidentified` (default).

Each advanced hypothesis records its estimand, association rather than causal claim,
uncertainty, support, missingness, failure modes, attempted views, and one prospective
falsifier. Capital-preservation rules are evaluated separately and do not need an
alpha label.

This creates a fixed maximum family of four primary tests per sleeve (two
hypotheses times two venues), controlled with a 98.75% interval at family alpha
0.05. One supported venue may generate a venue-scoped hypothesis. Cross-venue
agreement is a separate portability status and requires both venue labels plus
the canonical map; disagreement is retained as heterogeneity rather than erasing
both venue estimates.

The four focal questions are already bounded; Phase 0 may determine feasibility
but may not replace them:

- `C-H1`: production D9 versus D7/D8 conditional association after fixed
  pump/turnover/prior-maximum adjustment;
- `C-H2`: current BTC-uptrend gate pass versus fail among otherwise statically
  eligible CONTINUOUS events;
- `L-H1`: both BTC+ETH regimes on versus any-off using a common next-close
  anchor among otherwise statically eligible LONG events;
- `L-H2`: paired exact current 1%/six-hour entry-policy path versus the common
  next-close anchor for classifier-eligible LONG signals.

Age, liquidity, ATR, trigger tiers, entry hour, and other gate panels are
descriptive. They cannot replace a failed focal question or generate an extra
hypothesis label.

## Deferred Contracts

The following are useful but explicitly outside the initial scout:

- the exhaustive CONTINUOUS TP/hold/cost atlas;
- LONG stop/TP parameter surfaces;
- inverse-vol/BTC-risk/sizing calibration;
- granular adverse-state research already owned by the active granular contract;
- a fixed forward execution epoch with instrumentation schema, clock/change ledger,
  event-count or calendar stopping rule, and fixed slippage model family;
- cross-sleeve portfolio, margin, netting, heat, hedge, and liquidation studies;
- any deployment or prospective evaluation.

A cross-sleeve tape cannot infer margin or portfolio risk from raw candidates. Its
future contract must freeze capital allocation, admission, netting, hedge evolution,
funding, margin/liquidation, and same-symbol offset policy.

## Planner, Identity, And Artifacts

The current planner is read-only:

```bash
PYTHONPATH=. .venv/bin/python \
  scripts/strategy_overhaul_scout_2026_07_10.py --plan \
  --bybit-root "$HOME/SHARED_DATA/bybit_full_pit" \
  --binance-root "$HOME/SHARED_DATA/binance_full_pit" \
  --write-plan "$HOME/SHARED_DATA/strategy_overhaul_scout_2026-07-10/phase0/plan.json"
```

The actual S00 inventory decodes only Parquet schemas/footers and selected
identity/provenance columns. It writes a content-addressed bundle whose own files
are hash-verified and never calls either feature or label builder. That verifies
bundle integrity, not canonical Phase-0 semantics or S01 readiness:

```bash
PYTHONPATH=. .venv/bin/python \
  scripts/strategy_overhaul_scout_2026_07_10.py --phase0-inventory \
  --bybit-root "$HOME/SHARED_DATA/bybit_full_pit" \
  --binance-root "$HOME/SHARED_DATA/binance_full_pit" \
  --output-root "$HOME/SHARED_DATA/strategy_overhaul_scout_2026-07-10/phase0"
```

Exit `2` with a written bundle means `PARTIAL`/`NOT_READY`, not lost work. The
receipt remains diagnostic and identifies the exact missing venue/dataset.
`--deep-root-hash` is deliberately refused inside S00 because full-file hashes
depend on all encoded values. A separate `BYTE_SNAPSHOT_ONLY` precursor may hash
opaque whole-file bytes—including bytes that encode numeric or later-used outcome
data—without decoding them. Thus outcome-blind means no outcome field is decoded,
calculated, ranked, or inspected; it does not mean that every hashed byte is
outcome-free. Neither projected identity hashes nor the byte snapshot establish
registered scope, earliest history, or semantic provenance by themselves.

On the big PC, run only the canonical resumable builder for a venue whose
`phase0_source_ready` is false, with the signal boundary pinned rather than derived
from the wall clock:

```bash
export PYTHON_BIN="$PWD/.venv/bin/python"
export BYBIT_FULL_ROOT="$HOME/SHARED_DATA/bybit_full_pit"
export BINANCE_FULL_ROOT="$HOME/SHARED_DATA/binance_full_pit"

# Run only the missing venue(s), then rerun --phase0-inventory above.
BYBIT_END=2026-07-10 bash scripts/build_full_pit_bybit.sh
BINANCE_END=2026-07-10 bash scripts/build_full_pit_binance.sh
```

The 2026-07-10 shallow local preflight identified Binance 2026-07-03 through
2026-07-09 as the partition-readiness gap while Bybit covered the signal window.
The later exact local Phase-0 run confirmed that gap and is recorded below. No
big-PC S00 bundle has run. The big-PC inventory is still required to observe the
intended roots, but its machine location does not confer authority; it must pass
the same bundle-integrity and internal re-execution checks. A
deterministic manifest-derived venue-local map can satisfy local identity after
the root refresh. External JSON, its `review_status`, and its product/multiplier
fields are untrusted diagnostics; portability requires a separate trusted
reviewer-bound receipt plus semantic product/lifecycle/unit validation. Other
reported S01 substitutions remain separate blockers.

`--plan` reports source/config identities, root coverage, dirty state, registered
builder paths, unresolved implementation debt, and proposed stages.
`--phase0-inventory` adds field/schema drift, exact
storage-key and hourly-grid checks, pair-grain PIT provenance, manifest↔kline
coverage, RMOM identity/provisional coverage, exact signal-window identity support,
resource/checkpoint/concurrency estimates, six strict proposed stage schemas with
blocking builder mismatches, and instrument-map coverage. Neither is a population
or outcome runner. LONG support dates use
`membership_date = UTC date(signal_ts_ms - 1ms)`, not the signal timestamp's
calendar date.

The inventory runner preserves one content-addressed `PARTIAL`/`NOT_READY`
bundle containing per-venue diagnostics while a root or required lineage input
is stale; this is useful data-quality evidence but does not complete S00 or
establish S01 readiness. Phase 0
is complete only when both roots cover their causal warmup and the signal window
through 2026-07-09. It does not need to wait for the proposed label tail. No
outcome-bearing child run is permitted before the child contracts/manifests exist
and the later label boundary and canonical semantic root receipts pass.

The final child runners must provide `--plan`, refuse undefined schemas or parity
failures, checkpoint by venue/month, and bind commit, exact roots, config/source
hashes, environment, contract, and analysis manifest into one run ID.

Phase-0 artifacts under
`strategy_overhaul_scout_2026-07-10/phase0/<phase0_id>/` are:

- `receipt.json`, `command_plan.json`, `identity.json`, and the complete
  `phase0_inventory.json`;
- `field_availability.json`, `pit_provenance.json`, and
  `manifest_kline_coverage.json`;
- `rmom_population_coverage.json` with day-grain identity/provisional coverage
  and numeric validity explicitly deferred;
- `root_lineage.json` with canonical physical-root identities, per-required-
  dataset venue/source-label compatibility, and explicit unsigned/missing
  root-build receipt limitations; compatible strings reject obvious swaps but
  do not authenticate upstream archives;
- `resource_estimate.json` with the partition/checkpoint/concurrency plan, a
  conservative runtime range, and a warm-cache reference that is not presented
  as a cold-cache/big-PC benchmark;
- `proposed_schemas.json` with all six strict stage dtypes/units/null semantics,
  and `child_schema_registry.json` with proposed registry-v4 implementation
  vocabulary, per-field `source_columns`, and blocking mismatches;
- `instrument_map_coverage.json` (portable readiness remains false without a
  separate trusted reviewer receipt) and `outcome_blind_audit.json`;
- `registered_child_designs.json`, `support_design_and_counts.json`, and
  `s01_template_input_status.json`, preserving the finite designs, outcome-blind
  counts, resolved substitutions, and explicit blockers.

The repo holds non-executable LONG/CONTINUOUS child-contract and analysis-manifest
templates. S01 may substitute content-identified Phase-0 identities/counts only
after independent semantic review clears the registered scope, history,
population, PIT/map, config/parity, and sidecar requirements. A
`BYTE_SNAPSHOT_ONLY` root artifact or generic stage receipt cannot satisfy that
review. Only then may S01 create the canonical child filenames. The inventory
runner never self-approves or freezes its own downstream contract.

Child-run artifacts are specified only in those contracts. Any root, timing, PIT,
duplicate-key, schema, or selected-decision parity failure stops the affected
sleeve/venue. One-venue output may support only a venue-scoped exploratory
hypothesis; it cannot support a cross-venue portability label.

## Results

### Local Phase-0 evidence card

- **Claim and decision:** determine whether the two local full-PIT roots and the
  registered software inputs are structurally ready to freeze either A0 child.
  This was a readiness diagnostic, not a test of strategy merit.
- **Study mode/result:** outcome-blind diagnostic; `NOT_READY` (process exit 2).
  No decision rule was applied to returns or labels.
- **Identity:**
  `strategy-overhaul-phase0-bccefdfc38ae9fda3c17`; `receipt.json` file SHA-256
  `ed5fb3687280db691dcda5e32e00005a8dd48dd2fb403c2f48fe6cb69a81bb03`;
  inventory payload identity
  `6a29029c9c4341cbe713970587425830a04dc6021fa0eb8e95039c2be0eb6a47`.
  The bundle is under
  `$HOME/SHARED_DATA/strategy_overhaul_scout_2026-07-10/phase0/` on the local
  workstation. An immediate strict verifier call completed internal
  re-execution successfully; its reporting wrapper then failed while trying to
  deep-copy immutable `mappingproxy` fields, so no separate verifier receipt was
  persisted.
- **Scope:** local Bybit/Binance roots; inventory read window 2023-02-23 through
  2026-07-10 exclusive; registered CONTINUOUS and LONG signal windows only.
  The big-PC roots were not accessed.
- **Validity:** valid for the narrow structural/key/provenance-label readiness
  claim. Limited for source and environment reconstruction. Invalid for source
  authenticity, upstream completeness, canonical root lineage, numeric feature
  validity, outcomes, alpha, execution, portfolio risk, or deployment.
- **Observed blockers:** Binance manifest and `klines_1h` are both missing the
  seven daily partitions 2026-07-03..09; all 471,321 Binance manifest membership
  pairs have unknown observation provenance; Binance RMOM lacks
  `is_provisional`; its RMOM identity coverage is 448,022/471,321 (~95.06%).
  Bybit manifest/kline coverage spans all 1,233 required dates and its RMOM
  identity coverage is 484,134/511,482 (~94.65%), including 1,632 provisional
  symbol-days, but 360 Bybit kline rows have no source label. Neither root has a
  canonical authenticated root-build receipt. The partial auto-derived map was
  bundled but its zero entries were consumed. S02 config parity was `UNWIRED` in
  this exact source snapshot.
- **Prospective software defect:** the run's Bybit source-label sanity registry
  omitted `bybit_public_trades` and `bybit_rest`, although current ingestion and
  downloader code emits them. Correcting that registry cannot retroactively
  change this bundle; a corrected run receives a new identity. Null source rows
  and unauthenticated lineage remain blockers after that correction.
- **Outcome blindness/authorization:** OHLCV and residual-momentum numeric
  values, returns, ranks, labels, MAE/MFE, PnL, and outcomes were not read or
  calculated. Deployment state is `offline` and authorization is `unauthorized`;
  `outcome_run_authorized=false`. No S01 freeze, outcome run, deployment, or
  real-money action is authorized.
- **Next justified action:** correct the prospective registry/parity software
  defects, refresh and provenance-enrich the Binance root, repair or explain
  missing Bybit source labels, establish canonical root lineage, then produce a
  new big-PC Phase-0 bundle. Do not instantiate canonical children from this
  receipt.

After this receipt and its strict re-execution, the local Binance root was
prospectively repaired with the pinned daily-tail builder for 2026-07-03..10
exclusive. It checked 5,628 archive jobs, appended 129,088 hourly rows, recorded
245 genuine 404s, had zero hard failures, and rewrote a 593,757-row
coverage-derived manifest with non-null `binance_vision_archive` source and
membership provenance. A narrow post-build audit found all seven date
directories, zero duplicate `(symbol,date,url)` keys, and zero null source
labels. This mutation does not upgrade the historical receipt above; it makes a
new Phase-0 identity mandatory.

The engine, content-addressed bundle writer, non-executable child templates, gap-safe
CONTINUOUS raw builder, exact diagnostic CONTINUOUS S02/S03/S04 paths, exact
LONG S02/S03/S04 boundaries, mechanically derived LONG context sidecars, shared
projector, canonical expected-population/full-reconstruction path,
non-authoritative root-byte snapshot precursor, generic diagnostic byte-binding
stage-receipt utility, separate Parquet/Arrow semantic verifier, and registry-v4
leakage/refusal checks are synthetically validated. This is limited software
evidence plus the narrow local readiness diagnostic above. No real S02 feature
construction, real S03 entry/anchor artifact, real S04 path label, or outcome
analysis has run. Existing narrow
candidate/selected ledgers and current source were inspected only to diagnose
missing population and dependence; no forward label from the proposed
population has been calculated or inspected. Consumer-owned config wiring is now
prospectively `WIRED` for 11/11 targets, but authenticated RMOM source/root/PIT
provenance, independently inventoried population completeness, semantic
canonical-ID rederivation, and transitive LONG-sidecar/canonical-chain debts keep
both canonical child contracts absent and support no alpha, gate, promotion,
sizing, or deployment conclusion.
