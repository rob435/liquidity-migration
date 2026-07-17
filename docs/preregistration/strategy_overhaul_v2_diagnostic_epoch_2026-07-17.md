# Strategy Overhaul V2 Diagnostic Epoch, 2026-07-17

## Prospective contract

- **ID / owner / registered time / study mode:**
  `strategy-overhaul-v2-diagnostic-epoch-2026-07-17`; repository owner with
  Codex operator; 2026-07-17 19:30 UTC; exploratory diagnostic read. This
  window is already historically exposed. It is not confirmation or OOS.
- **Pre-outcome correction:** at 2026-07-17 20:04 UTC, before any real-source
  candidate or label generation, the contract made the active stablecoin
  exclusion explicit and replaced a proposed 240-day raw-kline warm-up with a
  120-day feature warm-up plus the manifest's conservative archive-age lower
  bound. No real path value had been generated or inspected; this corrected
  text is the frozen contract consumed by the first partition.
- **Claim and intended decision:** build a causal, pre-alpha-gate population
  for the active LONG and CONTINUOUS sleeves, measure attrition and path
  characteristics, and later use that evidence to nominate at most one thesis
  per sleeve. This epoch cannot change a strategy rule, profile, deployment,
  size, or capital boundary.
- **Prior exposure:** the current profiles and `[2023-07-17, 2026-07-17)`
  history have already influenced repository decisions. The corrected active
  benchmark metrics and failure history are known. No new barebones tape or
  future-label value from this contract had been generated or inspected when
  this section was frozen.
- **Initial bounded partition:** Bybit USDT linear perpetuals from the existing
  read-only root `C:\Users\user\SHARED_DATA\bybit_full_pit`; source signals in
  `[2026-07-05, 2026-07-06)` UTC, with input reads limited to the causal
  120-calendar-day feature warm-up and the frozen 72-hour label tail (the exact
  read interval is `[2026-03-07, 2026-07-10)` UTC). The observed local kline and
  manifest boundary at registration is `2026-07-10` exclusive. This is one
  engineering/diagnostic partition, not the full epoch.
- **Operational roots:** no account journal, market-capture root, private
  execution stream, or venue mutation is used by this historical partition.
  Their hash boundaries are therefore not applicable. A later execution-TCA
  epoch must register its exact account/capture roots and journal boundary
  separately.
- **Sample units:** the source unit is unique
  `(sleeve, venue, symbol, signal_ts_ms)`. `component_scope` records active
  component gate context but never creates an independent source observation.
  Simultaneous wave is `(sleeve, venue, decision_ts_ms)`; the calendar block is
  UTC signal date. Candidate-level inference uses source rows, not selected
  portfolio trades.
- **Stopping rule / complete tested set:** first build and structurally validate
  exactly the one bounded partition above. The only strategy representations
  are the hash-pinned active reference by link and one barebones arm per sleeve.
  There is no filter ladder, threshold sweep, symbol exception, alternate
  window, or third arm. Do not append another partition or inspect path/PnL
  distributions until the first partition passes the integrity checks below.

## Frozen source populations

All timestamps are UTC milliseconds. Source and gate features must be available
no later than `decision_ts_ms`. PIT membership is joined independently from the
manifest; kline presence does not self-certify population membership.
Both sleeves retain their hash-pinned active `exclude_symbols` instrument
boundary (the configured stablecoin-perpetual exclusions); “all PIT-tradable”
below means all rows inside that frozen instrument scope, not a silent
post-outcome symbol exception.

### LONG

The pre-gate source is each closed daily feature row whose causal pump family
fires before PIT, regime, rank, close-location, or ATR filters. PIT is evaluated
independently as the first gate so a kline/manifest disagreement remains visible:

- one-day: `log_return >= 2.5 * sigma_daily_30d`, falling back to
  `log(1.15)` only when the sigma is unavailable or non-positive;
- three-day: `pump_3d_log >= threshold_1d * sqrt(3)`;
- seven-day: `pump_7d_log >= threshold_1d * sqrt(7)`.

The source key uses the daily end stamp. PIT membership uses
`date(signal_ts_ms - 1ms)`. The barebones required gates, in first-rejection
order, are:

1. `pit_tradable`: the exact manifest date-symbol row exists;
2. `history_floor`: at least 90 causal daily observations are available, so the
   trailing 90-day liquidity statistic is defined;
3. `liquidity_floor`: trailing 90-day median daily quote turnover is at least
   USD 500,000;
4. `pump_trigger`: at least one frozen pump condition above passes;
5. `entry_anchor`: from one through six hours after the signal, use the first
   hourly close at or below `0.99 * signal_close`; otherwise use the six-hour
   close; missing bars remain missing;
6. `signal_freshness`: the chosen anchor is no more than 24 hours after the
   signal.

The active BTC and ETH regimes, top-10 same-day volume rank, applicable
one/three/seven-day close-location test, positive ATR and 12% ATR cap, weekend
multiplier, realized-volatility sizing, BTC-volatility sizing, cooldown,
existing exposure, capacity, owner health, unresolved target, terminal entry
attempt, account risk, and publication are recorded as separate gates or
characteristics. They do not filter the barebones tape. Dynamic gates that are
not evaluated in the historical source partition are `not_applicable`, never
silently passed.

### CONTINUOUS

The pre-gate source is each PIT-manifested hourly row in source decile 9. Source
deciles are computed across all PIT-tradable rows with a finite causal
`max_ret168` feature at that timestamp. They are deliberately computed before
the active residual-momentum-quartile, 240-day age, and component event-subtype
filters. Stable residual momentum and its cross-sectional rank are joined for
recording; missing residual momentum does not remove a source row.

Registration preflight found that this local root's
`residual_momentum.parquet` lacks the required `is_provisional` provenance
column. Its values therefore cannot be called stable or consumed even as active
gate evidence. For the first partition, residual momentum and the active RMOM
quartile gate are explicitly `missing`; the file identity is retained in the
manifest as rejected input. The barebones source decile does not depend on it.

The barebones required gates, in first-rejection order, are:

1. `pit_tradable`: the manifest contains the hourly bar's UTC date-symbol;
2. `history_floor`: the manifest's causal `first_archive_observed_date` proves
   at least 30 calendar days of age and the causal source feature is finite;
3. `source_decile_9`: the frozen all-PIT source decile equals 9;
4. `liquidity_floor`: signal-bar quote turnover is at least USD 500,000;
5. `one_hour_confirmation`: the decision/entry anchor is exactly one full hour
   after the source bar closes (`signal_ts_ms + 2h`) and that close exists.

The active residual-momentum bottom quartile, BTC prior-30-day trend, 240-day
age, `turn3_pop3`, `turn4_pop3`, `turn4_pop5`, crowding, inverse-volatility and
BTC-risk sizing, adverse pause, cooldown/re-entry, existing exposure, capacity,
owner health, unresolved target, terminal entry attempt, account risk, and
publication are recorded but do not filter the barebones tape. Component gate
states are columns on the shared source row. `first_archive_observed_date` is an
archive-observation lower bound, not a claim about the venue's original listing
instant: the 240-day gate passes when that causal lower bound is at least 240
days and is `missing`, not failed, when it cannot establish the threshold.

## Gate and writer semantics

- Gate values are exactly `pass`, `fail`, `missing`, or `not_applicable`.
  Required missing data is a rejection, not a zero or pass.
- `first_rejection` is the earliest required gate in the frozen sleeve order
  whose state is `fail` or `missing`. `barebones_accepted` is true only when all
  required gates pass. `active_reference_accepted` is descriptive gate context
  only and cannot affect active code.
- An immutable source event is emitted once per source key. Re-evaluation emits
  a transition only when the canonical gate-state hash changes. The projection
  retains first/last evaluation times, evaluation count, first rejection, final
  disposition, and all applicable rejection keys. Duplicate source keys or a
  transition without its source fail closed.
- The writer is an observer. Enabling it must not change active decision keys,
  order, targets, lifecycle/accounting events, or numerical values. Writer
  failures are surfaced as diagnostic failures but cannot alter or suppress an
  active target. Deterministic writer-on/off fixture equivalence is required
  before an outcome-bearing partition is emitted.

## Frozen characteristics, labels, and portfolio rule

The complete characteristic families are: signal strength; close location;
volatility/ATR; turnover/liquidity; listing age; BTC/ETH regime; residual
momentum; event subtype; and available modeled execution-cost inputs. No other
split may influence thesis selection in this epoch without a prospective
amendment.

Future values stay out of the funnel payload. From the causal entry anchor,
the label payload records side-signed close returns at 1h, 6h, 24h, and 72h;
actual observation horizons; and side-signed MAE/MFE through 72h. A label uses
the first complete hourly bar ending at or after the target, with at most one
hour of lag. Otherwise the value is null with a reason. LONG is signed long and
CONTINUOUS is signed short.

The later barebones portfolio, not this first partition, uses fixed USD 10,000
notional per admitted source against fixed USD 1,000,000 capital. It keeps one
open position per symbol, closes due positions before admitting a same-time
wave, sorts simultaneous candidates by descending frozen source strength then
symbol, and caps LONG at 10 and CONTINUOUS at 25 concurrent positions. Sleeves
do not net each other. LONG retains the active ATR 1.5x stop, ATR 4x take profit,
and three-day maximum hold; CONTINUOUS retains TP12 and a 24-hour maximum hold.
Both use the active exact-settlement funding, fill timing, lifecycle/account
kernel, and accounting paths. LONG uses the pinned cost file with the active 3x
cost multiplier; CONTINUOUS uses 5.5 bps taker fee per side, 2.5 bps spread per
side, and `50 * sqrt(notional / signal_turnover)` bps impact per side. These
rules cannot be changed after path values are read.

The collision strength is also frozen: LONG uses the maximum of the one-,
three-, and seven-day pump value divided by its applicable pump threshold;
CONTINUOUS uses the all-PIT `max_ret168` cross-sectional percentile
(`source_composite`).

## Artifact and integrity contract

The run budget remains at most four claim-bearing payloads:

```text
manifest.json
execution_tca.parquet       omitted for this historical source partition
decision_funnel.parquet
path_labels.parquet
```

Resumable working partitions/checkpoints are not claim-bearing exports and are
deleted or retained as explicitly labelled working state. The first partition
must validate, without summarizing label values:

- exact root, code, contract, cost/config, input-partition, and output hashes;
- unique and independently recomputed source keys;
- PIT/source-key completeness for the declared population;
- causal `feature_ts_ms <= data_available_ts_ms <= decision_ts_ms <= entry_ts_ms`;
- valid gate states, first-rejection ordering, and transition folding;
- explicit missing reasons and no missing-as-zero conversion;
- duplicate suppression and one label row per accepted source key.

Only counts, schema/null counts, identities, and validation failures may be read
before this checkpoint is accepted. Returns, excursions, best/worst symbols,
splits, and portfolio outcomes remain uninspected.

## Baseline preflight and reconstruction identities

The immutable reference remains
`docs/strategy_overhaul_v2_baseline_2026-07-17.md` at code
`b095d5ce0274d094147d1f63262bb6f6606f3e7d`. A single local preflight at
2026-07-17 19:24:52 UTC checked the 23 pinned run/artifact paths and found all
23 missing, with zero hash mismatches among present files. The pinned commit is
available and is an ancestor of the registration checkout
`230572f35ef1`; the active profile source files are unchanged across that
range. This is a failed local comparison preflight. It prohibits active-result
comparison here and does not authorize regeneration, copying, refresh, or a
replacement backtest. The compact diagnostic partition may proceed as a new
exploratory artifact whose manifest retains this limitation.

The registration-time cost file is
`configs/volume_alpha.default.yaml`; its native-Windows worktree SHA-256 is
`cc0cd0c651c207c45bc9856691021167634c91b20c40fc67a2987a7a1f8dd24c`.
The partition manifest must pin content hashes for the contract, code commit,
effective LONG config, each effective CONTINUOUS component config, residual
momentum input, and every read manifest/kline partition. Line-ending-sensitive
worktree hashes must record the platform and cannot be substituted for the
baseline's absent Linux-generated files.

## Decision rule and non-conclusions

This first partition has no alpha pass threshold. A structural failure stops
expansion; a structurally valid partition authorizes only append of the
remaining preregistered diagnostic partitions. Missing PIT membership,
noncausal availability, unexplained duplicate keys, writer inequivalence, or a
changed shared strategy identity makes the affected partition invalid. Sparse
rows or missing labels may make it limited but are not repaired by changing the
rules after inspection.

This epoch cannot establish alpha, independent cross-venue replication,
runtime parity, calibrated fills, capacity beyond the fixed assumptions,
promotion, deployment readiness, mainnet readiness, or real-money authority.
