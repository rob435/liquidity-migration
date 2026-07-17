# Strategy Overhaul V2 completion cycle, 2026-07-17

## Prospective contract

- **ID / owner / registered time / study mode:**
  `strategy-overhaul-v2-completion-cycle-2026-07-17`; repository owner with
  Codex operator; 2026-07-17 21:30 UTC. Phase 3 is exploratory. Any Phase-5
  thesis test is confirmatory only under a later thesis-specific addendum
  frozen before its holdout outcomes are generated or read.
- **State at registration:** no value from `decision_funnel.parquet` or
  `path_labels.parquet` has been opened. The first partition's manifest,
  schemas, counts, null counts, hashes, and structural checks are known. Its
  `outcomes_inspected=false` boundary remains true. The immutable active
  benchmark receipt is known, but all 23 pinned local payloads remain absent;
  they will not be regenerated, copied, or replaced.
- **Claim and action:** finish the diagnostic-first V2 cycle by (1) measuring
  the causal source populations, attrition, paths, costs, concentration, and a
  fixed-capital barebones portfolio on a broad discovery surface; (2) selecting
  at most one mechanism thesis per sleeve; (3) applying one frozen test per
  selected thesis on a chronologically later, outcome-untouched surface; and
  (4) implementing only a supported thesis and proving offline model/runtime
  parity. Nothing here authorizes demo orders, mainnet, capital, or size.
- **Prior exposure:** current-profile aggregate historical results and prior
  mechanism studies are exposed as recorded in `docs/research_summary.md`.
  The discovery surface is exploratory. Holdout candidate labels and
  thesis-specific treatment/control results are uninspected at registration,
  but the underlying market history is not globally pristine: current-profile
  aggregate results have used part of it. A Phase-5 result is therefore
  prospective for the newly fixed mechanism comparison, not independent proof
  of an unmined market history.

## Frozen data surfaces and stopping rules

All work is read-only against
`C:\Users\user\SHARED_DATA\bybit_full_pit`, Bybit USDT linear perpetuals,
inside the stablecoin exclusions frozen by the first diagnostic contract.
Observed local kline and PIT-manifest coverage is `[2021-01-01, 2026-07-10)`.
Every partition retains the existing 120-calendar-day causal feature warm-up
and four-calendar-day label tail.

### Phase-3 discovery surface

Generate exactly 43 calendar-month signal partitions:

```text
[2021-05-01, 2021-06-01)
then every complete UTC calendar month without a gap
through [2024-11-01, 2024-12-01)
```

The deterministic partition key is `month=YYYY-MM`. The aggregate discovery
window is `[2021-05-01, 2024-12-01)`. December 2024 is a purge/embargo month:
no December signal label may enter discovery or holdout inference. Its causal
market rows may later serve as pre-January feature history because they were
available before the holdout decisions.

Build monthly partitions sequentially with concurrency one. Run the existing
structural preflight before each build, preserve failed working state, and
resume only when its run identity matches. Stop the discovery build if total
measured build time reaches two hours before all 43 partitions pass. A failed
or missing month stops outcome inspection; it does not authorize narrowing the
calendar or skipping the month after seeing any path value.

### Reserved Phase-5 holdout surface

Do not generate or read holdout labels before the thesis-specific contract is
frozen. The reserved union is exactly `[2025-01-01, 2026-07-06)`:

```text
18 complete calendar-month partitions from
[2025-01-01, 2025-02-01) through [2026-06-01, 2026-07-01),
one partial partition [2026-07-01, 2026-07-05), and
the already generated but outcome-uninspected partition
[2026-07-05, 2026-07-06).
```

The existing July-5 partition is consumed in place and is never regenerated or
copied. The holdout is spent for every selected thesis when the first holdout
path value is read, even if a sleeve has no eligible row or a run aborts.
There is one fixed-horizon look per selected thesis; no monthly peeking or
optional stopping is allowed.

No Binance surface is registered. Conclusions remain Bybit-specific. No data,
market, RMOM, research-refresh, active-equity, or baseline refresh is permitted.

## Population, identities, and structural gate

The first diagnostic contract continues to own the exact source definitions,
gate order, entry anchors, characteristic fields, path-label arithmetic, and
writer semantics. Its artifact-bound SHA-256 is
`9b522bb09bc08e36eb8cdddcbc47d915fc580499895879c2d10070b4fe090879`.
This completion contract changes no source or label value.

Before any discovery outcome is summarized, the 43 partitions must jointly
pass all of the following:

1. clean generating commit and exact contract/config/input/output identities;
2. unique, non-overlapping source keys across partitions and an independently
   recomputed source-key population for each month;
3. one label row for every and only every barebones-admitted key;
4. PIT membership provenance and exact
   `feature <= available <= decision <= entry` ordering;
5. valid gate states and first rejection, explicit missingness, no non-finite
   value, and no missing-as-zero conversion;
6. no source date outside discovery and no December-2024 or holdout label;
7. exact source/entry-key replay against the portfolio input before lifecycle
   simulation.

Any unexplained failure makes the affected discovery population invalid until
prospectively repaired. Input OHLC rejection remains visible by partition.

## Frozen Phase-3 analysis

The effective source unit is `(sleeve, venue, symbol, signal_ts_ms)`.
Simultaneous wave is `(sleeve, venue, decision_ts_ms)`. The uncertainty block
is UTC signal date; months and raw rows are not treated as independent
replications.

For each sleeve and each 1h, 6h, 24h, and 72h side-signed path:

- average candidates within a simultaneous wave, then waves within a UTC date,
  and report the equal-date mean;
- report candidate median, win fraction, missing count/reason, unique sources,
  waves, dates, symbols, and top-symbol/top-date concentration as descriptive
  diagnostics;
- compute a 95% percentile interval by resampling UTC dates with replacement
  10,000 times using seed `20260717`. This is block-aware exploratory
  uncertainty, not a confirmatory p-value.

The primary exploratory path for thesis ranking is 24h. The 72h path is a
coherence/fragility guardrail; 1h and 6h are mechanism timing diagnostics. MAE
and MFE use the same date-block estimator. No alternative horizon, weighting,
bootstrap seed, or favorable missing-value bound may influence selection.

### Complete characteristic set

Each continuous characteristic is split at discovery Q25/Q75; the registered
contrast is Q4 minus Q1. Quantiles use finite source rows before portfolio
selection. Each categorical characteristic reports every level and the
predeclared contrast below. A contrast requires at least 100 unique sources,
30 waves, and 30 UTC dates in each side; otherwise it is sparse and cannot
qualify a thesis.

| Family | LONG field/contrast | CONTINUOUS field/contrast |
| --- | --- | --- |
| signal strength | `source_strength`, Q4-Q1 | `source_composite`, Q4-Q1 |
| close location | close location of the dominant pump horizon, Q4-Q1 | `dist_low`, Q4-Q1 |
| volatility/ATR | `atr_14d_pct`, Q4-Q1 | `rv_168h`, Q4-Q1 |
| turnover/liquidity | `log1p(turnover_median_90d)`, Q4-Q1 | `log1p(turnover_quote)`, Q4-Q1 |
| listing age | `symbol_age_days`, Q4-Q1 | `archive_age_lower_bound_days`, Q4-Q1 |
| BTC/ETH regime | joint active `11` minus any-off | active BTC-uptrend pass minus fail |
| residual momentum | not applicable | missing-only integrity count; no effect estimate |
| event subtype | dominant 1d/3d/7d pump level means | strongest of turn4p5, turn4p3, turn3p3, none |
| modeled execution cost | constant active LONG round trip; no split | modeled round-trip bps, Q4-Q1 |

The dominant LONG pump is the horizon with the greatest frozen
`pump_value / pump_threshold`; ties resolve 1d, then 3d, then 7d. CONTINUOUS
event levels resolve in the table's strongest-to-weakest order. Missing values
form an explicit count/bucket and never enter Q1 or Q4.

For each eligible contrast, report the 24h and 72h date-block effect and its
interval, plus the same point estimate in the two fixed eras
`[2021-05-01, 2023-01-01)` and `[2023-01-01, 2024-12-01)`. These are the full
outcome-bearing variants. Other tables may diagnose integrity or
concentration but may not nominate a thesis.

## Frozen barebones portfolio and accounting

Build one combined-sleeve ledger and one combined-sleeve curve payload, with a
`sleeve` column. Candidate selection remains separate by sleeve. Both use
fixed USD 10,000 notional against fixed USD 1,000,000 capital, one open
position per symbol, exit-before-entry at a shared timestamp, descending
`source_strength` then symbol within a wave, maximum 10 LONG and 25 CONTINUOUS
positions, and no cross-sleeve netting.

- LONG uses the funnel's retrace/deadline anchor, positive causal ATR geometry,
  1.5 ATR stop, 4 ATR take profit, 72h maximum hold, zero post-exit cooldown,
  and the active pinned cost multiplied 3x. A tape-admitted row with missing or
  non-positive ATR remains in candidate inference but is explicitly rejected
  from the executable portfolio as `missing_exit_geometry`.
- CONTINUOUS uses the one-hour confirmation anchor, TP12, 24h maximum hold,
  5.5 bps taker fee per side, 2.5 bps spread per side, and
  `50 * sqrt(10000 / signal_turnover)` bps impact per side.
- Both consume exact venue funding settlements when available and preserve
  `modeled`, `partial`, or `missing` coverage. Same-bar stop/target ambiguity
  retains the active conservative stop-first lifecycle ordering.
- The engines must emit the same timestamped target intents through the
  production `AccountExecutionKernel`, execution twin, reducer, and event hash
  chain. Account decisions, fills, positions, cash/equity, fees, funding,
  closes, and final flatness must reconcile before performance is reported.

Fixed notional implies fixed-capital additive accounting:
`equity = 1 + cumulative(net_pnl_usd / 1_000_000)`. The daily curve is
mark-to-market, books entry cost on entry, exact funding on its settlement/exit
accounting boundary, and ends flat. It is an unmatched diagnostic sensitivity,
not a return-delta comparison with the absent active compound curves.

The selected native environment is Windows while the production journal's
durability boundary is POSIX. A single-process research adapter may replace
only `flock` and directory-fsync durability with one process-local mutex,
file-fsync, and atomic replace inside a unique ignored run root. It may not
replace the account kernel, reducer, execution twin, event construction,
prices, decisions, costs, or arithmetic. Journal bytes must verify after the
run. This yields numerical/accounting evidence but no Linux durability,
concurrency, or runtime-deployment claim; any numerical or event mismatch
invalidates the portfolio output.

Report net return, maximum drawdown, worst day, funding/cost contribution,
turnover, position occupancy, exit reason, synchronized loss, symbol/day tail
concentration, and cross-sleeve same-symbol/time overlap. Capacity is valid only
for the frozen USD 10,000 assumption.

## Phase-4 selection rule

Create one candidate mechanism for each eligible outcome-bearing family above
that maps to one causal decision-time lever. A candidate may remove or add one
existing gate, change one existing threshold to the discovery Q25/Q50/Q75
cutpoint, or replace one existing size scalar; no symbol/time exception,
multi-feature rule, or second threshold is allowed. Record every considered
candidate, including failures.

Score each candidate before choosing a thesis:

- identification: 2 direct causal field, 1 reconstructable derived field, 0
  unavailable or provenance-invalid;
- support: 2 for at least 365 sources, 100 waves, and 100 dates on both sides;
  1 for the minimum eligible support above; otherwise 0;
- economic effect: 2 when the 24h point contrast exceeds the applicable median
  modeled round-trip cost and its 95% block interval keeps the same sign; 1
  when the point contrast exceeds cost but the interval crosses zero; else 0;
- temporal stability: 2 when both fixed-era effects retain the full-sample sign
  and at least half its magnitude; 1 when both retain sign; else 0;
- implementation cost: 1 when no new data, state, or lifecycle path is needed;
  otherwise 0;
- prior-evidence penalty: minus 2 when the same lever was directly contradicted
  by a valid prior registered test.

A thesis needs total score at least 6, identification at least 1, support at
least 1, a causal/economic mechanism, plausible post-cost benefit, and one
precise falsifier. Ties resolve by larger `abs(24h effect) / median round-trip
cost`, then family order as listed above. Select at most one thesis per sleeve
and at most two total. CONTINUOUS cannot qualify while the root's residual
momentum provenance prevents construction of the exact current-profile
comparator; a separate causal repair must be registered before outcomes, not
inferred from this diagnostic.

## Phase-5 and Phase-6 rules

Before any holdout label is generated/read, create one compact preregistration
per selected thesis. It must freeze the exact current-profile control, one
treatment lever, discovery-derived constant, primary paired effect, block
method, net portfolio guardrails, multiplicity across selected theses, and
supports/contradicts/inconclusive thresholds. The complete tested set is the
control plus exactly one treatment per selected sleeve. No holdout retune is
allowed.

If and only if a thesis is supported, implement the smallest profile change and
run offline/shadow parity on the exact registered model: source and decision
keys, targets, ordering, lifecycle events, account event hashes/state, and
declared numeric tolerances. Phase 6A closes only when these agree and the full
local quality gate or an explicitly scoped platform limitation is recorded.
Phase 6B (a forward demo/paper execution epoch) remains a separate operational
contract requiring explicit owner authorization of the deployment and risk
boundary. It is not silently launched by this research contract. Mainnet and
real money remain unauthorized.

If no thesis qualifies, or every registered thesis is contradicted or
inconclusive, implementation/runtime parity is not applicable. The plan closes
with that honest negative result; it does not mine another rule on the spent
holdout.

## Artifact budget and reconstruction

Each candidate partition retains exactly three claim-bearing payloads:

```text
manifest.json
decision_funnel.parquet
path_labels.parquet
```

The Phase-3 aggregate analysis retains exactly four:

```text
manifest.json
diagnostics.json
barebones_ledger.parquet
barebones_curve.parquet
```

Tracked evidence cards and preregistrations are control records, not duplicate
run payloads. A thesis run may retain at most four payloads named in its later
contract. Working checkpoints and verified account transaction segments may
exist only below the ignored run root; their aggregate identity and terminal
journal hash belong in the manifest, not as copied evidence bundles.

The manifest pins the clean code commit, both contracts, effective configs,
data-root boundary, every input-partition aggregate, every candidate manifest,
portable-accounting deviation, command, elapsed time, tested set, seed, and
output hashes. Generated paths are:

```text
reports/strategy-overhaul-v2/diagnostic-epoch-2026-07-17/bybit/discovery/month=YYYY-MM/
reports/strategy-overhaul-v2/diagnostic-epoch-2026-07-17/phase3-analysis/
reports/strategy-overhaul-v2/diagnostic-epoch-2026-07-17/bybit/holdout/month=YYYY-MM/
reports/strategy-overhaul-v2/thesis-*/
```

## Decision boundary and non-conclusions

Phase 3 may nominate but cannot support alpha. Phase 5 can support only its
exact Bybit mechanism at the frozen USD 10,000 scale and under the stated
historical execution model. Neither historical agreement nor offline parity
establishes calibrated fills, venue capacity beyond that scale, independent
replication, forward execution reliability, deployment readiness, mainnet
readiness, or real-money authority.

