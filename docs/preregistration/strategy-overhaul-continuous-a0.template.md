# TEMPLATE — Strategy Overhaul CONTINUOUS-A0 Child Contract

Template date: 2026-07-10

Status: NON-EXECUTABLE TEMPLATE

Parent contract:
docs/preregistration/strategy-overhaul-scout-2026-07-10.md

Matching analysis template:
docs/preregistration/strategy-overhaul-continuous-a0.analysis.template.json

This file is prospective scaffolding, not the canonical CONTINUOUS-A0 child
contract. It deliberately does not use the canonical child filename expected by
the planner. No feature, anchor, label, or analysis run is authorized from this
template.

The canonical child contract may be created only after every
REQUIRED_PHASE0_SUBSTITUTION below is replaced from reviewed big-PC Phase-0
artifacts, the matching JSON has no unresolved placeholder, and the planner
recognizes and content-hashes both canonical filenames into an external freeze
receipt. These mechanical conditions are necessary, not sufficient: an
independent semantic verifier must also clear registered scope, earliest-history
coverage, population/PIT/map provenance, config parity, and transitive stage
identity. A `BYTE_SNAPSHOT_ONLY` root artifact is not a canonical root receipt,
and a generic byte-binding stage receipt does not validate artifact semantics or
clear provenance blockers. A canonical file must not embed its own hash because
that would be circular.
At freeze, the canonical Markdown must say `Status: FROZEN CANONICAL CHILD`.
The canonical JSON must set `template_status` to `FROZEN_CANONICAL_CHILD`,
`canonical_child_filename_created` to `true`, and `execution_permitted` to
`true`; the planner validates those values and records both full-file hashes.

## Required Phase-0 Substitutions

<!-- REQUIRED_PHASE0_KEYS_JSON: ["run_id","canonical_contract_path","canonical_analysis_manifest_path","repository_commit","worktree_policy","patch_bundle_sha256","untracked_source_bundle_sha256","environment_lock_path","environment_lock_sha256","canonical_config_json_path","canonical_config_sha256","config_identity_json_path","config_identity_sha256","registered_scope_json_path","registered_scope_sha256","component_config_json_path","component_config_sha256","s02_config_parity_manifest_path","s02_config_parity_manifest_sha256","source_function_hashes","bybit_root_receipt","binance_root_receipt","pit_manifest_receipts","signal_feature_schema_path","signal_feature_schema_sha256","entry_anchor_schema_path","entry_anchor_schema_sha256","path_label_schema_path","path_label_schema_sha256","instrument_map_version","instrument_map_sha256","instrument_map_row_coverage","instrument_map_symbol_coverage","phase0_support_counts","resource_plan","partition_checkpoint_plan","output_root","exposure_ledger_path","label_tail_root_receipt"] -->

The canonical child must replace, without inference from this workstation:

| Field | Required value |
| --- | --- |
| run_id | REQUIRED_PHASE0_SUBSTITUTION |
| canonical_contract_path | REQUIRED_PHASE0_SUBSTITUTION |
| canonical_analysis_manifest_path | REQUIRED_PHASE0_SUBSTITUTION |
| repository_commit | REQUIRED_PHASE0_SUBSTITUTION |
| worktree_policy_and_patch_bundle_sha256 | REQUIRED_PHASE0_SUBSTITUTION |
| environment_lock_and_sha256 | REQUIRED_PHASE0_SUBSTITUTION |
| canonical_config_json_and_sha256 | REQUIRED_PHASE0_SUBSTITUTION |
| config_identity_json_and_sha256 | REQUIRED_PHASE0_SUBSTITUTION |
| registered_scope_json_and_sha256 | REQUIRED_PHASE0_SUBSTITUTION |
| component_config_json_and_sha256 | REQUIRED_PHASE0_SUBSTITUTION |
| s02_config_parity_manifest_and_sha256 | REQUIRED_PHASE0_SUBSTITUTION |
| source_function_hashes | REQUIRED_PHASE0_SUBSTITUTION |
| semantically verified Bybit root/scope/history receipt (`BYTE_SNAPSHOT_ONLY` is insufficient) | REQUIRED_PHASE0_SUBSTITUTION |
| semantically verified Binance root/scope/history receipt (`BYTE_SNAPSHOT_ONLY` is insufficient) | REQUIRED_PHASE0_SUBSTITUTION |
| PIT manifest receipts and provenance counts | REQUIRED_PHASE0_SUBSTITUTION |
| feature schema path and sha256 | REQUIRED_PHASE0_SUBSTITUTION |
| anchor schema path and sha256 | REQUIRED_PHASE0_SUBSTITUTION |
| path-label schema path and sha256 | REQUIRED_PHASE0_SUBSTITUTION |
| instrument-map version, hash, and coverage | REQUIRED_PHASE0_SUBSTITUTION |
| Phase-0 support/intersection counts | REQUIRED_PHASE0_SUBSTITUTION |
| resource, partition, checkpoint, and concurrency plan | REQUIRED_PHASE0_SUBSTITUTION |
| output root and immutable exposure-ledger path | REQUIRED_PHASE0_SUBSTITUTION |
| label-tail root receipt through 2026-07-14 exclusive | REQUIRED_PHASE0_SUBSTITUTION |

The config must be derived mechanically from
apply_continuous_demo_profile applied to ContinuousDemoCycleConfig with
strategy_profile equal to continuous_ensemble_v2 and btc_trend_gate equal to
uptrend. Manual transcription is not an identity.
The bundled S02 config-parity manifest must derive `WIRED` from every required
consumer-owned validator before S02. A historical `UNWIRED` bundle cannot be
upgraded merely by materializing or relabeling the artifact.

## Required S01 Outputs Before S02

S01 must jointly identity-bind the canonical `source_keys.jsonl`,
`expected_population.jsonl`, and `expected_population_receipt.json`. The full
reconstruction verifier must validate those files against the bound
config/root/PIT/manifest-pair/map inputs before the resulting in-memory object
may enter S02. S02 must then reproduce the registered key population exactly.
A stage-specific semantic receipt for S02 is required before S03, and the same
semantic chain must be reverified through S04. These are S01/downstream gates,
not facts supplied by the Phase-0 template itself.

The Phase-0 bundle is an outcome-blind diagnostic: it does not decode,
calculate, rank, or inspect outcome fields. That does not imply every opaque
byte used for file identity is outcome-free. Full-file byte hashes and semantic
validation are separate facts.

A dirty worktree is permitted only if the complete binary patch and every
untracked source file are bundled, hashed, reconstructable, and unchanged across
S02 through S04. Otherwise the child runner must refuse.

## Claim, Mode, And Permitted Decision

Mode is exploratory. The historical window is already spent.

The child asks whether two implemented static choices have stable conditional
associations with ideal hourly short paths:

1. whether production D9 adds association beyond current pump intensity,
   turnover intensity, and lagged maximum return; and
2. whether the current BTC-uptrend gate separates more favorable paths among
   otherwise current-static-eligible D9 pump events.

The child may emit at most two hypothesis families, C-H1 and C-H2, with each
family labeled separately by venue. It cannot label
any rule retain, remove, replace, promote, or deploy. It cannot infer a causal
marginal gate effect, net performance, execution quality, capacity, sizing, or
portfolio value. Positive or negative means only that a new prospective
falsifier is justified. Unidentified is the default.

Deployment is offline. Authorization is unauthorized. Mainnet is outside scope.

## Frozen Evidence Surface

- Signal window: 2023-04-01 00:00:00 UTC inclusive through
  2026-07-10 00:00:00 UTC exclusive.
- Causal read warmup: 2023-02-23 00:00:00 UTC inclusive. Warmup rows are never
  output decisions.
- Required path-data boundary: 2026-07-14 00:00:00 UTC exclusive.
- Venues: Bybit linear perpetual and Binance USD-M linear perpetual, analyzed
  separately.
- Row key: venue, symbol, decision_ts_ms.
- Strategy-decision key: venue, symbol, signal_ts_ms. Component membership never
  duplicates a decision row.
- Hourly source stamps are bar-open stamps. The signal bar at signal_ts_ms closes
  at signal_ts_ms plus one hour. Its decision and feature availability time are
  that close.
- For non-provisional stable RMOM[D], `rmom_data_available_ts_ms` is derived as
  `D - 1 day + 1 hour` from the frozen shift-3 forward-target
  construction. Provisional rows have null RMOM availability. This is a
  conservative offline causal-computability boundary, not an actual historical
  publication, ingestion, or operational-latency timestamp. The RMOM source day,
  explicit provisional state, and root content must still be receipt-bound.
- PIT membership date is the UTC date of the kline stamp.
- Venue-local identity comes from the substituted versioned map, which may keep
  each venue symbol distinct. Cross-venue equivalence comes only from a separately
  reviewed portability map. Raw ticker equality is never identity authority.

## Hard Stage Boundary

### S02 — Signal-Time Feature Tape

S02 reads no OHLC row after decision_ts_ms. It writes one row for every
manifest-covered symbol/hour with a fully closed signal bar. Missing and
provisional fields remain rows with explicit flags. S02 does not filter on RMOM,
q25, D9, trigger, liquidity, age, BTC state, or any stateful gate.

S02 contains no entry price, next-close return, path return, MFE, MAE,
first-passage value, cost, funding, or trade PnL.

S02 must pass:

- exact key uniqueness and timestamp-grid checks;
- PIT and availability-time checks;
- exact production q25/rank/decile parity on the parity population;
- exact p3, p4p3, and p4p5 predicate and nesting parity;
- exact signal-time static gate and first-rejection parity;
- post-decision mutation invariance;
- schema, config, source, root, and map identity locks.

No cooldown, held-book, capacity, component-order admission, sizing, or portfolio
replay belongs to A0. Those depend on prior outcomes and require a separate named
contract.

### S03 — Common Entry-Anchor Labels

S03 is outcome-bearing and may run only after S02 parity, after the canonical
contract and JSON are frozen, and after stage-specific validators establish the
canonical semantic/transitive identity chain. The generic stage-receipt utility
cannot satisfy this gate by itself.

The exact common anchor is the close of the bar immediately following the signal
bar after the implemented one-hour confirmation delay:

- entry_bar_start_ts_ms = signal_ts_ms plus one hour;
- entry_anchor_ts_ms = signal_ts_ms plus two hours;
- entry_price = close of that following bar.

Missing anchor data stays missing with a reason. S03 is a separate keyed artifact
and never mutates S02 in place.

### S04 — Minimal Path Labels

S04 may run only after S03 anchor parity and a stage-specific semantic receipt
verifies the canonical S03 inputs. It writes the finite labels below in a
separate keyed artifact. No other outcome is calculated or exposed.

### No A0 Stateful Admission Stage

There is no S05 stateful admission replay in this child. Research and live
admission semantics are not silently equated.

## Frozen S02 Feature Set

### Identity, timing, and provenance

- venue, canonical_instrument_id, symbol, signal_ts_ms, decision_ts_ms;
- signal_bar_close_ts_ms, feature_data_available_ts_ms,
  data_available_ts_ms;
- manifest_date, membership_source, membership_inferred;
- first_archive_observed_date, reported_launch_time_ms;
- root_first_bar_ts_ms, provenance limitation, and coverage state;
- age_days_reported_launch and age_days_root_first_bar;
- exact current age-source name, current_age_240_pass, and age-source
  availability.

Every field carries its dtype, unit, null meaning, and availability timestamp in
the substituted schema.

### Raw bar and per-symbol causal features

- open, high, low, close, turnover_quote;
- ret1 = close divided by prior close minus one;
- ret72 = close divided by close 72 hourly rows earlier minus one;
- ret168 = close divided by close 168 hourly rows earlier minus one;
- rv_168h = rolling sample standard deviation of ret1 over 168 observations,
  minimum 48;
- max_ret168 = rolling maximum ret1 over the production 168-observation window,
  minimum 48, including current ret1;
- prior_max_ret168_lag1 = rolling maximum of ret1 over the 168 observations
  ending one observation before the current row, minimum 48;
- min720 and max720 = rolling close minimum and maximum over 720 observations,
  minimum 168;
- vov = rolling sample standard deviation of rv_168h over 720 observations,
  minimum 168;
- dist_low = (close minus min720) divided by (max720 minus min720), null when
  denominator is non-positive;
- prior6_ret1_max and prior6_close_max from the six prior observations only;
- giveback_from_prior6_high = close divided by prior6_close_max minus one;
- prior168_turnover_mean and prior168_turnover_std from turnover shifted one
  observation, window 168, minimum 48;
- turnover_24h = rolling 24-observation turnover sum, minimum 6;
- turnover_spike_168h = turnover_quote divided by prior168_turnover_mean;
- turnover_zscore_168h = turnover_quote minus prior mean, divided by prior
  sample standard deviation, null when the denominator is non-positive.

### RMOM and ranks

- residual_momentum raw value, source day, source presence, derived
  causal-computability availability, provisional flag, provenance-declared flag,
  and stable-available flag;
- residual_momentum_rank with average ties over stable, finite peers;
- RMOM population, rankable, missing, tie, and denominator counts;
- exact current q25 pass at rank less than or equal to 0.25;
- full-population max_ret168 and turnover ranks over finite peers;
- exact production q25 max_ret168 and turnover ranks after stable RMOM and q25;
- value rank, score, score rank, decile, tie count, population count, rankable
  count, missing count, denominator count, tie method, and denominator rule for
  every rank;
- production current_q25_d9 flag.

Average ties are mandatory. Production denominator and singleton behavior must
match current source exactly. Diagnostic and production ranks are never
substituted for each other. The derived availability does not relax the
receipt-bound RMOM source/provisional provenance requirement or establish
historical refresh latency.

### Events, overlap, spells, and waves

- trigger_turn3_pop3: turnover_spike_168h at least 3 and ret1 at least 0.03;
- trigger_turn4_pop3: turnover_spike_168h at least 4 and ret1 at least 0.03;
- trigger_turn4_pop5: turnover_spike_168h at least 4 and ret1 at least 0.05;
- component bits p3=1, p4p3=2, p4p5=4;
- component mask, component tags, membership count, and implied stepped weight
  using p3=1/3, p4p3=2/9, p4p5=4/9;
- exact production D9 component membership and spell-head fields;
- per-symbol trigger spell: maximal run of trigger rows separated by exactly one
  hour;
- event wave: sort unique p3 trigger timestamps within a venue. Start a wave at
  the earliest unassigned timestamp. Add later timestamps while each adjacent
  gap is at most six hours and timestamp is strictly less than wave start plus
  72 hours. The next timestamp starts a new wave. All symbols at a timestamp
  share the wave. Non-p3 rows have null event_wave_id.

The nesting p4p5 implies p4p3 implies p3 is a hard parity assertion.

### Static current gates and finite market context

- current_liquidity_500k_pass;
- exact current age-source availability and current_age_240_pass;
- exact causal BTC-uptrend value, pass, fail, and unknown;
- BTC and ETH close returns over 1h, 24h, and 168h;
- BTC and ETH rv_168h using the same sample-standard-deviation/minimum-48 rule;
- alt_breadth_ret24_positive: finite manifest alt peers with ret24 greater than
  zero divided by finite peers, excluding BTC and ETH;
- alt_breadth_ret1_ge_3pct: finite manifest alt peers with ret1 at least 0.03
  divided by finite peers, excluding BTC and ETH;
- xs_ret1_dispersion: cross-sectional sample standard deviation of finite ret1;
- peer, missing, and denominator counts for every breadth/dispersion field.

The frozen static first-rejection diagnostic uses this order:

1. stable RMOM available;
2. q25 pass;
3. BTC gate known;
4. BTC-uptrend pass;
5. production D9;
6. turnover at least 500,000 USDT;
7. the named component trigger;
8. exact current age source available;
9. age at least 240 days;
10. static_candidate.

The component diagnostic is emitted separately for p3, p4p3, and p4p5. This is a
signal-pipeline attribution order, not a marginal-effect claim. Stateful held,
cooldown, capacity, ordering, and portfolio reasons are absent.

No OI, premium, taker flow, positioning, depth, funding, or unspecified field is
permitted.

## Frozen Analysis Bins

All numeric bins are left-closed and right-open. The final bin is unbounded above.
Missing is a separate category. Values below a declared zero floor are invalid,
not silently binned.

| Feature | Ordered bins |
| --- | --- |
| ret1 | below -5%; [-5%,0%); [0%,1%); [1%,3%); [3%,4%); [4%,5%); [5%,8%); [8%,12%); at least 12% |
| turnover_spike_168h | [0,1); [1,2); [2,3); [3,4); [4,6); [6,10); at least 10; missing |
| prior_max_ret168_lag1 | below 3%; [3%,5%); [5%,10%); [10%,20%); at least 20%; missing |
| stable RMOM rank | [0,.10); [.10,.25); [.25,.50); [.50,.75); [.75,1]; missing; provisional |
| production decile | missing; 0-6; 7; 8; 9 |
| component mask | 0; 1; 3; 7; anything else is invalid |
| turnover_quote USDT | below 100k; [100k,500k); [500k,2m); [2m,10m); [10m,50m); at least 50m; missing |
| each age variant, days | below 30; [30,90); [90,240); [240,730); at least 730; missing |
| BTC gate | pass; fail; unknown |

Focal event ret1 bins are [3%,4%), [4%,5%), [5%,8%), [8%,12%), and at least
12%. Focal turnover-spike bins are [3,4), [4,6), [6,10), and at least 10.
There is no outcome-driven bin merge, quantile edge, interaction, spline, or
alternate transform.

## Frozen S03 And S04 Labels

For horizon h in 1, 24, and 72:

- underlying_return_h = close at entry_anchor_ts_ms plus h hours divided by
  entry_price minus one;
- short_directional_return_h = negative underlying_return_h.

For h in 24 and 72:

- short_mfe_h = max(0, 1 minus minimum hourly low through h divided by
  entry_price);
- short_mae_h = max(0, maximum hourly high through h divided by entry_price
  minus 1).

Each horizon emits endpoint timestamp, observed-hour count, availability,
complete flag, missing reason, and hourly-extrema interval-censor flag. A path is
complete only when every required hourly observation is present and valid.
Partial MFE or MAE is never used in calibration or an estimand.

There is no stop/target pair in A0, so same-bar stop/target ambiguity is
not_applicable for CONTINUOUS. No first-passage, cost, funding, fill, impact,
entry alternative, TP, hold, stop, sizing, hedge, or portfolio label is allowed.

## Frozen Descriptive Panels

Every panel reports, separately by venue and bin:

- total rows, complete rows/rate, distinct canonical symbols, retained blocks,
  and event waves;
- arithmetic mean, Hyndman-Fan type-7 q10, median, and type-7 q90 of short
  directional return at 1h, 24h, and 72h;
- arithmetic mean short MFE and short MAE at 24h and 72h.

Panels and populations:

1. ret1 bins over all finite ret1 rows;
2. turnover-spike bins where ret1 is at least 3%;
3. RMOM-rank categories among p3 triggers;
4. production-decile categories among p3 triggers with stable q25;
5. component-mask categories among p3, stable-q25, D9 rows;
6. turnover, both age variants, and BTC-gate categories among p3,
   stable-q25, D9 rows.

These panels cannot receive a hypothesis label and cannot replace C-H1 or C-H2.
All attempted and empty panels are preserved.

## Frozen Dependence And Uncertainty

- Calendar blocks are consecutive 28-day UTC intervals anchored at
  2023-04-01 00:00:00 UTC.
- The final partial block is descriptive only.
- An inferential row is retained only when entry_anchor_ts_ms plus 72 hours is
  less than or equal to its block end.
- Primary estimators weight each decision equally.
- Wave-balanced sensitivity assigns each event wave total weight one, divided
  equally among its rows.
- Bootstrap samples whole retained 28-day blocks with replacement, preserving
  every symbol and wave in a sampled block.
- Replicates: 5,000.
- Seed: 20260710.
- Interval: percentile 98.75%, endpoints at 0.625% and 99.375%.
- Fewer than 4,950 finite, full-rank replicates makes the hypothesis
  unidentified.
- Bybit and Binance are never pooled. Agreement is correlated robustness, not
  independent replication.

Multiplicity is the fixed maximum family of four primary tests: two focal
hypotheses times two registered venues. The 98.75% interval is the frozen
Bonferroni treatment for family alpha 0.05 even if one venue is unavailable.

## Frozen Support And Missingness Rules

For each focal arm and venue:

- at least 100 complete decisions;
- at least 10 canonical symbols per arm and 15 overall;
- at least 8 retained 28-day blocks per arm and 12 overall;
- at least 30 event waves per arm;
- horizon-label completeness at least 90% per arm;
- arm completeness rates differ by at most 5 percentage points;
- no symbol exceeds 15% of rows;
- no block exceeds 20% of rows;
- no wave exceeds 20% of rows.

Concentration is checked before and after complete-case filtering. The OLS design
must be full rank and every coefficient finite.

The archive-observed sensitivity requires explicit pair-grain observation status
`archive_observed`; inferred and unknown rows are both excluded. It requires at
least 60 complete decisions per arm, at least 8 retained blocks overall, and the
same sign as that venue's primary estimate. Cross-venue sign agreement affects
only the separate portability status. Failure is unidentified for that venue,
not permission to loosen PIT.

Venue-local identity-map coverage must be at least 95% of focal rows and 90% of
focal symbols for a venue-scoped label. Cross-venue canonical matching is needed
only for portability. One-venue evidence may generate a venue-scoped hypothesis;
it cannot claim portability.

## Frozen Controls

### Hash pseudo-arms

For j equal to 0 and 1:

1. form the UTF-8 string
   A0-placebo-j|venue|canonical_instrument_id|signal_ts_ms;
2. compute SHA-256;
3. set Pj to bit j of the first digest byte, with bit zero the least-significant
   bit.

Each pseudo-arm must have share from 0.40 through 0.60 in every focal
population/venue. Failure does not permit a new salt.

For each focal regression, replace the real arm with Pj and run the identical
covariates, block bootstrap, PIT sensitivity, support, and fold rule. If either
pseudo-arm earns a positive or negative label on a venue, both real hypotheses
on that venue become unidentified_negative_control. Another venue is not erased
by a venue-local control failure.

### Structural controls

- A transient zero-horizon anchor divided by itself return, MFE, and MAE must be
  exactly zero and is never written as an analysis label.
- Mutating every post-decision OHLC row must leave the complete S02 artifact
  byte/hash identical.
- Duplicating, deleting, or reordering a component membership must not duplicate
  the strategy-decision key; parity failure stops the run.

## Frozen OLS Specification

Each venue is fitted separately by unweighted ordinary least squares with:

- an intercept;
- the binary focal arm;
- treatment-coded categorical columns in the exact feature and bin order listed
  by the hypothesis;
- no weights, regularization, interactions, model selection, imputation, or
  alternate model.

The first listed in-population bin is the fixed reference. An absent non-reference
level creates no column. An absent fixed reference, rank-deficient design, or
non-finite focal coefficient makes the hypothesis unidentified.

## C-H1 — D9 Incremental Association

Focal population:

- p3 trigger true;
- stable RMOM q25 pass;
- turnover_quote at least 500,000 USDT;
- exact current age source available and age at least 240 days;
- exact BTC-uptrend gate pass;
- production decile in 7, 8, or 9;
- required covariates and 24h label complete.

Arm is production D9. Comparator is production D7 or D8. D0-D6 appears only in
the descriptive panel.

Primary outcome is short_directional_return_24h.

Model:

short_directional_return_24h =
intercept + beta_D9 times D9 +
ret1 event-bin indicators +
turnover-spike event-bin indicators +
prior-max-ret168-lag1-bin indicators + error.

References are ret1 [3%,4%), turnover spike [3,4), and prior maximum below 3%.
There are no interactions.

Positive means beta_D9 is positive under the final rule. Negative is the mirror.
The estimand is adjusted association, never causal value added by D9.

## C-H2 — BTC-Uptrend Gate Association

Focal population:

- p3 trigger true;
- stable RMOM q25 pass;
- production D9;
- turnover_quote at least 500,000 USDT;
- exact current age source available and age at least 240 days;
- BTC gate known;
- required covariates and 24h label complete.

Arm is BTC-uptrend pass. Comparator is BTC-uptrend fail.

Primary outcome and OLS covariates/references are identical to C-H1, replacing
the D9 arm with the BTC-pass arm.

Positive means the current uptrend-pass state is associated with more favorable
24h short paths. Negative is the mirror. It is not a causal gate effect.

## Frozen Chronological Stability Folds

1. 2023-04-01 inclusive to 2024-05-01 exclusive;
2. 2024-05-01 inclusive to 2025-06-01 exclusive;
3. 2025-06-01 inclusive to 2026-07-10 exclusive.

A fold is supported only with at least 30 complete rows per arm, 5 canonical
symbols, 3 retained blocks, and 10 waves per arm. At least two folds per venue
must be supported, and every supported fold point estimate must share the
full-sample sign. Folds measure internal stability only and are not OOS.

## Frozen Decision Rule

For each of C-H1 and C-H2, evaluate each venue separately:

Hypothesis_positive requires all of:

1. all identity, timing, PIT, parity, schema, control, venue-local identity,
   support, missingness, concentration, and bootstrap gates pass on that venue;
2. the 98.75% interval is strictly above zero on that venue;
3. the wave-balanced point estimate is positive on that venue;
4. the chronological-fold rule passes with positive sign on that venue;
5. the archive-observed sensitivity is positive on that venue;
6. neither hash pseudo-arm earns a positive or negative label on that venue.

Hypothesis_negative is the exact sign mirror.

Every other venue result is unidentified. Venue disagreement is preserved as
heterogeneity rather than deleting both estimates. Portability is
`concordant_positive` or `concordant_negative` only when both venue labels agree
and the cross-venue map is ready; otherwise it is heterogeneous or unidentified.
A null, unsupported arm, missing venue-local identity, opposite fold, control
failure, or PIT limitation cannot be rescued by another panel, horizon, binning,
or model.

The dossier advances no more than C-H1 and C-H2. It must state effect,
uncertainty, support, missingness, concentration, attempted panels, limitations,
and one prospective falsifier per non-unidentified hypothesis. It must state
explicitly that A0 does not support a strategy change or deployment.

## Required Artifacts

The canonical child must substitute exact paths and hashes for:

- identity.json;
- root_receipts.json containing canonical semantic scope/history verification,
  not only byte snapshots;
- source_hashes.json;
- source_keys.jsonl;
- expected_population.jsonl;
- expected_population_receipt.json;
- schema_signal_features.json;
- schema_entry_anchor.json;
- schema_path_labels.json;
- signal_feature_tape partitioned by venue/month;
- signal_parity_report.json;
- entry_anchor_labels partitioned by venue/month;
- entry_anchor_parity_report.json;
- path_labels partitioned by venue/month;
- support_and_missingness.json;
- calibration_tables;
- focal_estimands.json;
- bootstrap_replicates_or_sufficient_statistics;
- negative_controls.json;
- exposure_ledger.jsonl;
- evidence_card.md and evidence_card.json;
- content-addressed stdout, stderr, command, and failure receipts, plus
  stage-specific semantic validation receipts.

Any failure or partial cell is retained. No canonical contract may delete this
template; the template remains evidence of the pre-Phase-0 design surface.
