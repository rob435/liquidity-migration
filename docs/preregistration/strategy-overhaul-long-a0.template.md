# TEMPLATE — Strategy Overhaul LONG-A0 Child Contract

Template date: 2026-07-10

Status: NON-EXECUTABLE TEMPLATE

Parent contract:
docs/preregistration/strategy-overhaul-scout-2026-07-10.md

Matching analysis template:
docs/preregistration/strategy-overhaul-long-a0.analysis.template.json

This file is prospective scaffolding, not the canonical LONG-A0 child contract.
It deliberately does not use the canonical child filename expected by the
planner. No feature, entry-policy, path, or analysis run is authorized from this
template.

The canonical child contract may be created only after every
REQUIRED_PHASE0_SUBSTITUTION below is replaced from reviewed big-PC Phase-0
artifacts, the matching JSON has no unresolved placeholder, and the planner
recognizes and content-hashes both canonical filenames into an external freeze
receipt. These mechanical conditions are necessary, not sufficient: an
independent semantic verifier must also clear registered scope, earliest-history
coverage, population/PIT/map/sidecar provenance, config parity, and transitive
stage identity. A `BYTE_SNAPSHOT_ONLY` root artifact is not a canonical root
receipt, and a generic byte-binding stage receipt does not validate artifact
semantics or clear provenance blockers. A canonical file must not embed its own
hash because that would be circular.
At freeze, the canonical Markdown must say `Status: FROZEN CANONICAL CHILD`.
The canonical JSON must set `template_status` to `FROZEN_CANONICAL_CHILD`,
`canonical_child_filename_created` to `true`, and `execution_permitted` to
`true`; the planner validates those values and records both full-file hashes.

## Required Phase-0 Substitutions

<!-- REQUIRED_PHASE0_KEYS_JSON: ["run_id","canonical_contract_path","canonical_analysis_manifest_path","repository_commit","worktree_policy","patch_bundle_sha256","untracked_source_bundle_sha256","environment_lock_path","environment_lock_sha256","canonical_config_json_path","canonical_config_sha256","config_identity_json_path","config_identity_sha256","registered_scope_json_path","registered_scope_sha256","s02_config_parity_manifest_path","s02_config_parity_manifest_sha256","source_function_hashes","bybit_root_receipt","binance_root_receipt","pit_manifest_receipts","signal_feature_schema_path","signal_feature_schema_sha256","entry_policy_schema_path","entry_policy_schema_sha256","path_label_schema_path","path_label_schema_sha256","instrument_map_version","instrument_map_sha256","instrument_map_row_coverage","instrument_map_symbol_coverage","phase0_support_counts","resource_plan","partition_checkpoint_plan","output_root","exposure_ledger_path","label_tail_root_receipt"] -->

The canonical child must replace:

| Field | Required value |
| --- | --- |
| run_id | REQUIRED_PHASE0_SUBSTITUTION |
| canonical_contract_path | REQUIRED_PHASE0_SUBSTITUTION |
| canonical_analysis_manifest_path | REQUIRED_PHASE0_SUBSTITUTION |
| repository_commit | REQUIRED_PHASE0_SUBSTITUTION |
| worktree_policy_and_patch_bundle_sha256 | REQUIRED_PHASE0_SUBSTITUTION |
| environment_lock_and_sha256 | REQUIRED_PHASE0_SUBSTITUTION |
| canonical config JSON and SHA-256 | REQUIRED_PHASE0_SUBSTITUTION |
| config identity JSON and SHA-256 | REQUIRED_PHASE0_SUBSTITUTION |
| registered scope JSON and SHA-256 | REQUIRED_PHASE0_SUBSTITUTION |
| S02 config parity manifest and SHA-256 | REQUIRED_PHASE0_SUBSTITUTION |
| source-function hashes | REQUIRED_PHASE0_SUBSTITUTION |
| semantically verified Bybit root/scope/history receipt (`BYTE_SNAPSHOT_ONLY` is insufficient) | REQUIRED_PHASE0_SUBSTITUTION |
| semantically verified Binance root/scope/history receipt (`BYTE_SNAPSHOT_ONLY` is insufficient) | REQUIRED_PHASE0_SUBSTITUTION |
| PIT manifest receipts and provenance counts | REQUIRED_PHASE0_SUBSTITUTION |
| signal feature schema path and SHA-256 | REQUIRED_PHASE0_SUBSTITUTION |
| entry-policy schema path and SHA-256 | REQUIRED_PHASE0_SUBSTITUTION |
| path-label schema path and SHA-256 | REQUIRED_PHASE0_SUBSTITUTION |
| instrument-map version, hash, and coverage | REQUIRED_PHASE0_SUBSTITUTION |
| Phase-0 support/intersection counts | REQUIRED_PHASE0_SUBSTITUTION |
| resource, partition, checkpoint, and concurrency plan | REQUIRED_PHASE0_SUBSTITUTION |
| output root and immutable exposure-ledger path | REQUIRED_PHASE0_SUBSTITUTION |
| label-tail root receipt through 2026-07-14 exclusive | REQUIRED_PHASE0_SUBSTITUTION |

The config must be derived mechanically from _v11a_long_native_config. Manual
transcription is not an identity.
The bundled S02 config-parity manifest may remain `UNWIRED`; that status blocks
stage identity parity and is not changed merely by materializing the artifact.

The Phase-0 bundle is an outcome-blind diagnostic: it does not decode,
calculate, rank, or inspect outcome fields. That does not imply every opaque
byte used for file identity is outcome-free. Full-file byte hashes and semantic
validation are separate facts.

A dirty worktree is permitted only if the complete binary patch and every
untracked source file are bundled, hashed, reconstructable, and unchanged across
S02 through S04. Otherwise the child runner must refuse.

## Claim, Mode, And Permitted Decision

Mode is exploratory. The historical window is already spent.

The child asks:

1. whether the current joint BTC and ETH regime pass has a stable conditional
   association with common-anchor 72-hour long paths among signals that pass
   every other current static FC gate; and
2. whether the implemented 1% close-retrace/six-hour-fall-through timing policy
   is associated with different 72-hour post-entry paths from a common
   next-close anchor among exact FC classifier-eligible signals.

The child may emit at most two hypothesis families, L-H1 and L-H2, with each
family labeled separately by venue. It cannot label
any rule retain, remove, replace, promote, or deploy. The entry comparison ends at
different clock times for equal post-entry horizons; it estimates the implemented
timing policy, not a pure fill-price effect. Neither hypothesis identifies a
causal marginal gate effect or net strategy performance.

Deployment is offline. Authorization is unauthorized. Mainnet is outside scope.

## Frozen Evidence Surface

- Signal window: 2023-06-15 00:00:00 UTC inclusive through
  2026-07-10 00:00:00 UTC exclusive.
- Causal read warmup: 2023-03-16 00:00:00 UTC inclusive. Warmup rows are never
  output decisions.
- Required path-data boundary: 2026-07-14 00:00:00 UTC exclusive.
- Venues: Bybit linear perpetual and Binance USD-M linear perpetual, analyzed
  separately.
- Row and strategy-decision key: venue, symbol, signal_ts_ms.
- Daily signal timestamps are signal-bar close times. Every S02 field must be
  available by signal_ts_ms.
- PIT membership date is UTC date of signal_ts_ms minus one millisecond.
- Venue-local identity comes from the substituted versioned map, which may keep
  each venue symbol distinct. Cross-venue equivalence comes only from a separately
  reviewed portability map.

## Hard Stage Boundary

### S02 — Signal-Time Feature Tape

S02 retains every manifest-covered symbol/day and reads no bar after
signal_ts_ms. It does not filter on top-50 universe, age, BTC/ETH regime,
top-ten turnover rank, trigger, close location, ATR, cooldown, or capacity.

S02 may contain signal-time ATR exit percentages and fallback state. It must not
contain:

- an h1 through h6 close or low;
- next-hour close or return;
- common or current anchor time, price, or reason;
- anchor-relative stop or target price;
- retrace/fall-through result;
- forward return, MFE, MAE, first passage, trade PnL, cost, or funding.

S02 must pass:

- exact key, timestamp, PIT, and availability checks;
- exact daily-feature and rank parity;
- exact 1d/3d/7d trigger, bitmask, static-gate, first-rejection, detector, and
  classifier parity;
- exact ATR-exit-percentage/fallback parity;
- post-signal mutation invariance;
- schema, config, source, root, and map identity locks.

No cooldown, held-book, capacity, ordering, sizing, or portfolio admission replay
belongs to A0. It depends on prior outcomes.

### S03 — Explicit Entry-Policy Labels

S03 is outcome-bearing and may run only after S02 parity, after the canonical
contract and JSON are frozen, and after stage-specific validators establish the
canonical semantic/transitive identity chain. The generic stage-receipt utility
cannot satisfy this gate by itself. S03 is a separate keyed artifact.

Common anchor:

- common_entry_ts_ms = signal_ts_ms plus one hour;
- common_entry_price = close of that hour;
- a missing hour-one bar or close makes the common anchor unavailable.

Exact current policy:

1. The ordinary hour-one entry bar must exist. If it does not, current entry is
   unavailable immediately; a later retrace cannot rescue it.
2. threshold = signal_close times 0.99.
3. Scan hourly closes at hours 1 through 6 in order.
4. The first available close less than or equal to threshold is the current
   entry, with that close as entry price and reason sniper_retrace.
5. Missing hours 2 through 5 are skipped but make prefix_complete false.
6. If no close triggers, use the hour-six close with reason
   sniper_deadline_fallthrough. A missing hour-six bar/close makes the current
   anchor unavailable.
7. The intrabar-low touch is a non-fill diagnostic over only the exact scan
   prefix through the first close trigger or deadline. It never changes entry.

S03 emits only:

- common and current anchor availability, timestamp, hour, price, and reason;
- threshold and retrace percentage;
- scan start/end, prefix completeness, and missing-hour bitmask;
- first close-trigger hour;
- non-fill intrabar-low touched, observed first hour, and prefix-authoritative
  first hour;
- entry_price_improvement = common_entry_price divided by current_entry_price
  minus one;
- current delay hours relative to the common anchor;
- current-entry-policy-available indicator and missing reason.

Raw h1 through h6 close/low arrays are not written. Alternate 0.5% and 2% retraces
are not calculated.

S03 must pass exact parity against the implemented current 1%/6h policy.

### S04 — Minimal Path Labels

S04 may run only after S03 parity and a stage-specific semantic receipt verifies
the canonical S03 inputs. It writes the finite common- and current-anchor labels
below in a separate keyed artifact. No other outcome is calculated or exposed.

### No A0 Stateful Admission Stage

There is no stateful admission replay in A0. Classifier-eligible is not executed
or state-admitted.

## Frozen S02 Feature Set

### Identity, timing, and provenance

- venue, canonical_instrument_id, symbol, signal_ts_ms;
- signal_feature_available_ts_ms and every source availability timestamp;
- manifest_date, membership_source, membership_inferred;
- first_archive_observed_date, reported_launch_time_ms, root_first_bar_ts_ms;
- provenance limitation and coverage state;
- age_days_reported_launch, age_days_root_first_bar, and current
  symbol_age_days source.

Every field carries its dtype, unit, null meaning, and availability timestamp in
the substituted schema.

### Raw and causal daily features

- Daily bars aggregate UTC-day 1h rows using first open, maximum high, minimum
  low, last close, and summed turnover. A daily row requires at least 20 hourly
  bars and is stamped at the following UTC midnight.
- log_return = log(close) minus log(close on the prior calendar day).
- simple_return_1d = exp(log_return) minus one.
- pump_3d_log = log(close divided by close three calendar days earlier);
  simple_return_3d = exp(pump_3d_log) minus one.
- pump_7d_log = log(close divided by close seven calendar days earlier);
  simple_return_7d = exp(pump_7d_log) minus one.
- intra_max_Nh_pump_log uses N=6 from the current config: on 1h bars compute
  log(close divided by close exactly six hours earlier), then take the maximum
  within symbol and UTC signal day. It uses only bars ending by signal_ts_ms.
- close_location = (close minus daily low) divided by (daily high minus daily
  low), with 0.5 when the range is at most 1e-12.
- close_loc_3d and close_loc_7d use the corresponding rolling calendar-window
  maximum high and minimum low, minimum 3 and 7 daily observations, and the same
  0.5 fallback.
- realized_vol = 30-calendar-day rolling sample standard deviation of
  log_return, minimum 30, times square root of 365.
- sigma_daily_30d = realized_vol divided by square root of 365.
- turnover_median_90d = rolling calendar median turnover over 90 days, minimum
  90.
- today_volume_rank is ordinal descending turnover rank within signal_ts_ms.
- universe_rank is ordinal descending turnover_median_90d rank within
  signal_ts_ms.
- current in_universe means universe_rank at most 50, symbol_age_days at least
  30, and finite turnover_median_90d.
- symbol_age_days is the number of calendar days since the first eligible daily
  kline row in the complete loaded root history plus one. Age is derived before
  PIT-membership gating and the causal-read floor, matching production; a
  warmup-window first row is not an age anchor. It is not silently equated to
  reported launch age.
- true_range is maximum of daily high-low, absolute high-prior-calendar-close,
  and absolute low-prior-calendar-close.
- atr_14d_pct = rolling calendar mean true_range over 14 days, minimum 7, divided
  by close.
- turnover_median_30d is rolling calendar median turnover over 30 days, minimum
  15; vol_vs_30d_median = turnover divided by that median.
- coin_30d_return and coin_60d_return;
- age variants and current in_universe flag;
- BTC and ETH regime flags are close greater than their exact 30-calendar-day
  simple moving average; trend distance is close divided by that average minus
  one. Availability is explicit.
- exact BTC-month regime value/pass, even though the current config gate is off;
- any signal-time field required by the exact current detector/classifier.

Today volume rank and universe rank use the exact source ordinal-descending tie
semantics. Diagnostic peer counts do not change production ranks.

### Exact trigger diagnostics

Current active families are 1d, 3d, and 7d only.

- threshold_1d = 2.5 times sigma_daily_30d when sigma is finite and positive,
  otherwise log(1.15);
- threshold_3d = threshold_1d times square root of 3;
- threshold_7d = threshold_1d times square root of 7;
- ratio_1d = log_return divided by threshold_1d;
- ratio_3d = pump_3d_log divided by threshold_3d;
- ratio_7d = pump_7d_log divided by threshold_7d;
- trigger 1d also requires close_location at least 0.70;
- trigger 3d and 7d also require their close location at least 0.60;
- trigger bit values are 1d=1, 3d=2, and 7d=4;
- trigger_strength_ratio is the maximum ratio among families whose
  family-specific close-location requirement is satisfied;
- active_trigger_close_location is the maximum close-location value among fired
  trigger families.

Intraday and own-quantile triggers are disabled under the exact current config.
Any enabled extra trigger is an identity failure.

### Static gates and classifier

Write independent flags for:

- BTC-month regime;
- FC enabled;
- in universe, including current top-50 trailing-turnover universe and age
  requirement;
- BTC regime;
- ETH regime;
- today volume rank at most 10;
- log-return availability;
- any active 1d/3d/7d trigger;
- every current coin-SMA, coin-return, BTC-high, ATR, own-ATR, volume,
  BTC-distance, LSR, and OI gate, including explicit disabled state;
- ATR at most 0.12;
- exact source-ordered first rejection;
- exact FC detector result;
- exact classifier pattern/result and classifier-eligible flag.

The bullets above are evaluated for first rejection in this exact order:
BTC-month regime, FC enabled, in universe, BTC regime, ETH regime, volume rank,
log-return availability, any trigger, coin-above-own-SMA, coin minimum 30d
return, BTC-not-near-high, BTC-must-be-near-high, ATR cap, own-ATR percentile,
minimum volume confirmation, maximum volume confirmation, maximum coin 60d
return, minimum BTC SMA distance, maximum BTC SMA distance, LSR, and OI rising.
Disabled gates remain explicit pass/disabled fields and are not silently removed.
The raw `global_lsr` and `oi_chg_7d` values are forced null in A0 because
positioning and open interest are Tier C and neither gate is active in the frozen
config. Their disabled gate flags remain explicit `true`; no unregistered Tier-C
value can enter a signal-time row merely because the live detector accepts an
optional input.

Signal-time exit diagnostics are:

- fc_exit_stop_pct;
- fc_exit_take_profit_pct;
- fc_exit_max_hold_hours = 72;
- fc_atr_exit_available;
- fc_atr_fallback_used;
- fc_exit_param_source.

No anchor-relative stop/target level is allowed in S02.

## Frozen Analysis Bins

All numeric bins are left-closed and right-open. The final bin is unbounded above.
Missing is a separate category.

| Feature | Ordered bins |
| --- | --- |
| trigger_strength_ratio | below .75; [.75,1); [1,1.25); [1.25,1.5); [1.5,2); [2,3); at least 3; missing |
| trigger bitmask | 0,1,2,3,4,5,6,7; anything else invalid |
| today_volume_rank | 1-3; 4-10; 11-25; 26-50; above 50; missing |
| universe_rank | 1-3; 4-10; 11-25; 26-50; above 50; missing |
| atr_14d_pct | below 3%; [3%,6%); [6%,9%); [9%,12%); [12%,20%); at least 20%; missing |
| sigma_daily_30d | below 2%; [2%,4%); [4%,6%); [6%,10%); at least 10%; missing |
| active_trigger_close_location | below .4; [.4,.6); [.6,.7); [.7,.85); [.85,1]; missing |
| each age variant, days | below 30; [30,90); [90,240); [240,730); at least 730; missing |
| regime state | BTC/ETH 00; 01; 10; 11; unknown |
| S03 entry policy | retrace hour 1,2,3,4,5,6; deadline fallthrough; unavailable/missing |

There is no outcome-driven bin merge, quantile edge, interaction, spline, or
alternate transform.

## Frozen S03 Entry Labels

The exact fields and algorithm are defined in the S03 section. Entry-policy
labels are outcomes and every revealed entry-policy table is appended to the
exposure ledger before analysis.

entry_price_improvement is positive when the current anchor buys below the common
anchor. It is descriptive and does not become an execution claim.

## Frozen S04 Path Labels

For each prefix common and current and horizon h in 1, 24, and 72:

- underlying_return_prefix_h = close at entry timestamp plus h hours divided by
  entry price minus one.

For h in 24 and 72:

- mfe_prefix_h = max(0, maximum hourly high through h divided by entry price
  minus one);
- signed_mae_prefix_h = min(0, minimum hourly low through h divided by entry
  price minus one);
- adverse_magnitude_prefix_h = negative signed_mae_prefix_h.

Each horizon emits endpoint timestamp, observed-hour count, availability,
complete flag, missing reason, and hourly-extrema interval-censor flag. A path is
complete only when every required hourly observation is present and valid.
Partial extrema are never used in calibration or an estimand.

For each anchor:

- stop_price = anchor price times 1 minus signal-time fc_exit_stop_pct;
- take_profit_price = anchor price times 1 plus signal-time
  fc_exit_take_profit_pct;
- same_bar_stop_tp_ambiguity is true if any complete-path hourly bar through 72h
  has low at or below stop and high at or above target; false only when the full
  72h path is complete and no such bar exists; null otherwise.

The ambiguity flag does not reveal first-passage order. First passage, realized
exit, costs, funding, size, portfolio, alternate retrace, stop/TP grids, and
execution models are forbidden.

## Frozen Descriptive Panels

Every panel reports, separately by venue and bin:

- total rows, complete rows/rate, distinct canonical symbols, retained blocks,
  and daily waves;
- arithmetic mean, Hyndman-Fan type-7 q10, median, and type-7 q90 of underlying
  return at 1h, 24h, and 72h;
- arithmetic mean MFE and adverse magnitude at 24h and 72h.

Panels and populations:

1. Trigger-strength ratio and trigger bitmask where FC is enabled, in_universe
   passes, today-volume rank passes, and log return is available. Trigger pass is
   not required.
2. BTC/ETH four-state regime on the L-H1 base population that passes every
   current static gate except BTC/ETH regime.
3. Today-volume rank where FC is enabled, in_universe passes, log return is
   available, any current trigger passes, and ATR cap passes. The volume-rank gate
   itself is not required.
4. Universe rank and both age variants where FC is enabled, log return is
   available, any current trigger passes, and ATR cap passes. Universe and regime
   gates are not required.
5. ATR where FC is enabled, in_universe passes, today-volume rank passes, log
   return is available, and any current trigger passes. ATR and regime gates are
   not required.
6. Active-trigger close location where FC is enabled, in_universe passes,
   today-volume rank passes, and log return is available.
7. S03 retrace hour/fallthrough among exact FC classifier-eligible signals, with
   unavailable entry policies retained as a category.

Panels 1 through 6 use common-anchor labels. Panel 7 reports common and current
labels separately and their paired differences. Every panel preserves its
declared missing category.

These panels characterize current top-50/top-10, age, ATR, overlapping trigger,
regime, and entry choices. They cannot receive a hypothesis label or replace
L-H1 or L-H2.

## Frozen Dependence And Uncertainty

- Calendar blocks are consecutive 28-day UTC intervals anchored at
  2023-06-15 00:00:00 UTC.
- The final partial block is descriptive only.
- For L-H1 an inferential row requires common_entry_ts_ms plus 72h no later than
  block end.
- For L-H2 an inferential row requires the later of common and current entry
  timestamps plus 72h no later than block end.
- LONG daily wave is signal_ts_ms. All same-day signals share the wave.
- Primary estimators weight each classifier decision equally.
- Wave-balanced sensitivity gives each daily wave total weight one, divided
  equally among its rows.
- Bootstrap samples whole retained 28-day blocks with replacement.
- Replicates: 5,000.
- Seed: 20260710.
- Interval: percentile 98.75%, endpoints at 0.625% and 99.375%.
- Fewer than 4,950 finite/full-rank replicates is unidentified.
- Venues are separate and never pooled.

Multiplicity is the fixed maximum family of four primary tests: two focal
hypotheses times two registered venues. The 98.75% interval is the frozen
Bonferroni treatment for family alpha 0.05 even if one venue is unavailable.

## Frozen Support And Missingness Rules

For the two-arm L-H1, each arm/venue requires:

- at least 100 complete decisions;
- at least 10 canonical symbols per arm and 15 overall;
- at least 8 retained blocks per arm and 12 overall;
- at least 30 daily waves per arm;
- label completeness at least 90% per arm;
- arm completeness difference at most 5 percentage points.

For paired L-H2, each venue requires:

- at least 100 complete pairs;
- at least 15 canonical symbols;
- at least 12 retained blocks;
- at least 30 daily waves;
- common and current 72h completeness each at least 90% before complete-case
  filtering;
- common/current completeness difference at most 5 percentage points.

For both:

- no symbol exceeds 15% of rows;
- no block exceeds 20%;
- no daily wave exceeds 20%;
- concentration is checked before and after complete-case filtering;
- every model/design and bootstrap coefficient must be finite and full rank
  where applicable.

Archive-observed sensitivity requires explicit pair-grain observation status
`archive_observed`; inferred and unknown rows are both excluded. L-H1 requires
at least 60 complete rows per arm and 8 retained blocks overall. L-H2 requires
at least 60 complete pairs and 8 retained blocks. The sensitivity must have the
same sign as the primary estimate on its venue. Cross-venue sign agreement
affects only the separate portability status.

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
3. set Pj to bit j of the first digest byte, bit zero least significant.

Each pseudo-arm share must be from 0.40 through 0.60 in every focal
population/venue. Failure does not permit re-salting.

For L-H1, replace the real regime arm with Pj and run the identical OLS,
dependence, PIT, support, and fold rules.

For L-H2, fit paired delta72 = intercept + theta_j times Pj and apply the
identical block, PIT, support, and fold rules to theta_j.

If either control earns a positive or negative label on a venue, both real
hypotheses on that venue become unidentified_negative_control. Another venue is
not erased by a venue-local control failure.

### Structural controls

- Transient zero-horizon anchor divided by itself return, MFE, and MAE are
  exactly zero and are not written as labels.
- Mutating all post-signal OHLC leaves S02 byte/hash identical.
- Mutating an S03 prefix may alter only rows whose exact h1 through h6 prefix
  intersects the mutation.
- Mutating bars after an early current-entry anchor cannot alter that row's S03
  current anchor or prefix outputs.
- A missing hour-one bar can never be rescued by a later retrace.

## Frozen OLS Specification

Each venue is fitted separately by unweighted OLS with intercept, binary arm, and
treatment-coded categorical columns in exact declared order. The first listed
in-population bin is fixed reference. Absent non-reference levels create no
column. Absent reference, rank deficiency, or non-finite coefficient is
unidentified.

No weighting, regularization, interaction, model selection, imputation, or
alternate model is permitted.

## L-H1 — Joint BTC/ETH Regime Association

The focal population passes every current static FC gate except BTC/ETH regime:

- FC enabled;
- in current top-50/age universe;
- today volume rank at most 10;
- log return available;
- at least one exact current 1d/3d/7d trigger;
- ATR at most 0.12;
- BTC and ETH regime known;
- common-anchor 72h label complete.

There is no other active signal gate under the frozen current config. The
coin-SMA, coin-return, BTC-high, own-ATR, volume-confirmation, BTC-distance, LSR,
OI, intraday-trigger, and own-quantile settings are disabled and do not filter
L-H1. Their flags remain in S02 for identity and diagnostics; raw LSR/OI values
remain null under the A0 boundary.

Arm is regime state 11. Comparator is any of 00, 01, or 10. The four states
remain separate in descriptive output.

Primary outcome is common underlying_return_72h.

Model:

common_return_72h =
intercept + beta_regime times joint_regime_pass +
trigger-strength-bin indicators +
trigger-bitmask indicators +
today-volume-rank-bin indicators +
ATR-bin indicators +
active-trigger-close-location-bin indicators +
current-age-bin indicators + error.

Feature order is exactly as written. Fixed references are trigger strength
[1,1.25), trigger mask 1, volume rank 1-3, ATR below 3%, close location [.7,.85),
and age [30,90).

Positive means joint regime pass is associated with more favorable 72h common
long paths. Negative is the mirror. It is not a causal regime-gate effect.

## L-H2 — Current Retrace/Fall-Through Timing Association

Population:

- exact FC classifier-eligible signal, not state-admitted;
- common and current S03 anchors available;
- both common and current 72h paths complete;
- inferential block endpoint rule passes.

Primary estimand:

delta72 = current underlying_return_72h minus common
underlying_return_72h.

The estimator is the unweighted arithmetic mean paired delta72 separately by
venue. There is no regression, covariate adjustment, subgroup selection, or
alternate pairing.

Report, but do not select on:

- paired delta at 1h and 24h;
- entry_price_improvement;
- delay hours;
- MFE and adverse-magnitude differences at 24h and 72h;
- retrace-hour and deadline-fallthrough cells.

Positive means mean delta72 is positive under the final rule. Negative is the
mirror. Equal post-entry horizons end at different calendar times, so no pure
fill-price interpretation is allowed.

## Frozen Chronological Stability Folds

1. 2023-06-15 inclusive to 2024-07-01 exclusive;
2. 2024-07-01 inclusive to 2025-07-01 exclusive;
3. 2025-07-01 inclusive to 2026-07-10 exclusive.

For L-H1 a supported fold requires at least 30 complete rows per arm, 5 canonical
symbols overall, 3 retained blocks overall, and 10 daily waves per arm.

For L-H2 a supported fold requires at least 30 complete pairs, 5 canonical
symbols, 3 retained blocks, and 10 daily waves.

At least two folds per venue must be supported. Every supported fold point
estimate must share the full-sample sign. Folds are internal stability, not OOS.

## Frozen Decision Rule

For each L-H1 and L-H2, evaluate each venue separately:

Hypothesis_positive requires:

1. all identity, timing, PIT, parity, schema, control, venue-local identity,
   support, missingness, concentration, and bootstrap gates pass on that venue;
2. the 98.75% interval is strictly above zero on that venue;
3. daily-wave-balanced point estimate is positive on that venue;
4. chronological folds pass with positive sign on that venue;
5. archive-observed sensitivity is positive on that venue;
6. neither hash control earns a positive or negative label on that venue.

Hypothesis_negative is the exact sign mirror.

Every other venue result is unidentified. Venue disagreement is preserved as
heterogeneity rather than deleting both estimates. Portability is
`concordant_positive` or `concordant_negative` only when both venue labels agree
and the cross-venue map is ready; otherwise it is heterogeneous or unidentified.
A null, unsupported population, missing venue-local identity, fold disagreement,
control failure, or PIT limitation cannot be rescued by another panel, horizon,
retrace, binning, or model.

The dossier advances no more than L-H1 and L-H2. It must report effect,
uncertainty, support, missingness, concentration, all descriptive panels,
limitations, and one prospective falsifier per non-unidentified hypothesis. It
must state explicitly that A0 authorizes no strategy or deployment change.

## Required Artifacts

The canonical child must substitute exact paths and hashes for:

- identity.json;
- root_receipts.json containing canonical semantic scope/history verification,
  not only byte snapshots;
- source_hashes.json;
- schema_signal_features.json;
- schema_entry_policy.json;
- schema_path_labels.json;
- signal_feature_tape partitioned by venue/month;
- signal_parity_report.json;
- entry_policy_labels partitioned by venue/month;
- entry_policy_parity_report.json;
- path_labels partitioned by venue/month;
- support_and_missingness.json;
- calibration_tables;
- focal_estimands.json;
- bootstrap replicates or sufficient statistics;
- negative_controls.json;
- exposure_ledger.jsonl;
- evidence_card.md and evidence_card.json;
- content-addressed command, stdout, stderr, partial-cell, and failure receipts,
  plus stage-specific semantic validation receipts.

Any failure or partial cell is retained. No canonical contract may delete this
template.
