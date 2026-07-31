# Trading logic

What each sleeve trades, how it sizes, how it exits, and where its evidence stops. Code is
the authority: [`long_native_event_demo.py`](../liquidity_migration/long_native_event_demo.py)
and [`long_native.py`](../liquidity_migration/long_native.py),
[`carry_demo.py`](../liquidity_migration/carry_demo.py) and
[`financed_longs.py`](../liquidity_migration/financed_longs.py),
[`continuous_demo.py`](../liquidity_migration/continuous_demo.py) and
[`continuous_profile.py`](../liquidity_migration/continuous_profile.py),
[`continuous_hedge_manager.py`](../liquidity_migration/continuous_hedge_manager.py). Plain
English: [`plain_english_guide.md`](plain_english_guide.md).

## On today

Publication switches live in [`deploy/sleeves.env`](../deploy/sleeves.env).

| Sleeve | Trades | Demo | Paper | Mainnet |
| --- | --- | --- | --- | --- |
| LONG | Long a fresh volume pump, bought on a shallow retrace | on | on (own producer, `operational` profile only) | off |
| CARRY | Long coins whose shorts pay a deep crowd fee | on | on (demo targets mirrored) | off |
| CONTINUOUS | Short decile 9 of an hourly pump composite | off | off | — |
| Hedge | Long BTC+ETH against the CONTINUOUS short book | off (follows CONTINUOUS) | none | — |

Producers publish absolute component targets; they never place orders and never own fills,
funding, or P&L ([`architecture.md`](architecture.md)). `PAPER_TARGET_MIRROR=on`
republishes demo CARRY targets onto the paper route, so the two books differ only in
execution; the paper CARRY producer is off because two independent producers raced their
caches into a TLMUSDT position demo never asked for (−70.73 USDT, 2026-07-29).

## LONG — `LongV11aDivWeekendVol`

**Signal.** One profile, `long_v11a_profile()`, on fully closed daily bars:

| Filter | Value |
| --- | --- |
| Universe | top 50 by trailing 90d turnover, ≥30d listing history |
| Regime | BTC **and** ETH above their 30d moving averages |
| Volume rank today | ≤ 10 |
| Pump trigger | 1d/3d/7d log return ≥ 2.5σ (30d daily σ); σ unavailable → 15% 1d |
| Close location | ≥ 0.70 (1d trigger), ≥ 0.60 (3d/7d) |
| Volatility ceiling | 14d ATR ≤ 12% of price |
| Signal freshness | ≤ 24h |

Entry fires when price touches `signal_close × 0.99` (`sniper_retrace`), or falls through
at the 6-hour deadline while the signal is still fresh (`sniper_deadline_fallthru`). Ten
concurrent positions, 7-day per-symbol cooldown.

**Sizing.** Base slot `gross_exposure / max_concurrent_positions` = 10% of equity, times
`notional_multiplier` 0.5, times the BTC-vol scalar `clip(0.60 / btc_rv, 0.30, 1.25)`,
times the vol-parity weight `max(min(0.30/vol_used, 3.0), 0.25)` (30d realized vol, 30%
annual floor, 30% position-weight cap), times 1.5 on weekend entries. Entry leverage 2
changes margin only, never quantity. Five new entries per cycle maximum; the producer
refuses to run if projected full-book initial margin exceeds 50% of equity.

There is no separate per-order notional cap. `max_order_notional_pct_equity` is `0.0` in
[`configs/operational.demo.json`](../configs/operational.demo.json), and 0 means *disabled —
derive the slot from `gross_exposure / max_concurrent_positions × notional_multiplier`*
(`long_native_event_demo.py:118`, `target_long_order_notional_pct_equity` at `:214-227`).
Any value > 0 **replaces** the base slot outright rather than bounding it; the loader
accepts [0, 10] (`operational_profile.py:335-342`). Setting a small positive number as a
"safety bound" silently overrides the whole sizing chain.

At the profile's 250,000 USDT capital reference the registered worst-case envelope is
**234,375.00 USDT gross** and **117,187.50 USDT initial margin**: per-order 9.375% of
equity (= 23,437.50) × 10 concurrent positions ÷ entry leverage 2. The 9.375% is 5% base
slot × 1.25 worst-case BTC-vol scale × 1.5 weekend multiplier. Projected full-book initial
margin is therefore 46.875% against the 50% ceiling — 6.7% of headroom, so barely any
increase to `notional_multiplier` leaves the fleet able to boot. Runtime profile bytes
override any number written here.

**Exit.** Each target declares a 1.5×ATR14 stop and a 4.0×ATR14 take-profit; the account
owner converts both to venue prices off the first attributable fill and places the stop.
Time stop at 3 days publishes a zero target.

**Limits.** The forward record is demo-only. The retained internal backtest result depends
materially on take-profit winners, and the research runner does not abort when PIT
membership is incomplete — only an untainted run whose artifacts establish the population
supports a historical-universe claim ([`data.md`](data.md)). The runner's scoped run label
carries a funding-coverage dimension as well as a PIT one, and funding downgrades it
independently: `pit_required_missing_manifest`, `pit_membership_filtered_current_universe`,
`full_pit_universe_funding_missing`, `full_pit_universe_funding_coverage_low`,
`full_pit_universe_funding_partial`, `full_pit_universe` (`long_native.py:1440-1458`), plus
a separate methodology label `invalid` / `biased_benchmark` / `exploratory` derived from
taint and manifest state (`:1472-1487`). `full_pit_universe_pass=true` beside a
`full_pit_universe_funding_coverage_low` label is not a historical-universe claim.

## CARRY — `lane2_carry_hold_v3`

**Signal.** Long-only crowd-fee collection, replayed daily at 00:00 UTC over 90 days of
Bybit hourly data by calling the registered scorer functions directly, so the deployed book
and the forward scorer cannot drift apart. Universe: top 100 by 24h quote turnover.
Per-name hysteresis:

| Event | Rule |
| --- | --- |
| Enter | last settled funding print < −10 bp |
| Exit (normalize) | print rises above −3 bp |
| Exit (recovery) | trailing daily funding rate recovers > 30 bp over 2 days |
| Block entry, suspend hold to zero weight | trailing 3d return in [−30%, −5%) |
| Block entry | trailing 30d daily vol < 5% |

Null conditioning values fail open. The book is empty on 28% of days in the full record;
flat is a state, not a fault.

**Sizing.** `weight = 0.10 × clip(|trailing 24h settled funding| / 120bp-day, 0.25, 1.0)`,
gross capped at 1.0, then `weight × sizing_equity × notional_multiplier` (1.0). Sizing
equity is anchored to the decision, not the live mark — sizing off the live mark makes the
day's target a function of the book's own unrealized P&L (2026-07-30: $84.7k traded against
a ~$30k book in thirteen hours, zero strategy exits). A 5%-of-standing / $1 dead-band is
the backstop; entries below $10 notional are skipped.

**Exit.** Exits and resizes are a diff against the account owner's accepted reservations,
published exit-first. Entry intents expire 6h after the decision bar and are not published
inside the last 15 minutes of that window. A declared 35% stop backstops each position at
the venue. No time stop.

**Limits.** Concentrated (~3–4 names when active), long-only crash beta, single-venue Bybit
evidence, capacity ~$1M at 1% participation. The registered daily frame exits every name
24h before its final panel bar, worth roughly +0.13 Sharpe. The single-clock level is
decision-hour lucky: the same construction over 12 daily offsets spans Sharpe 0.30–1.52 and
midnight is the best cell. The three v3 filters were chosen in-sample in the review that
registered them; the paired forward differential against v2 grades them. After the funding
double-count fix, the corrected carry-hold benchmark Sharpe is **1.21 (t 2.31)** — it does
**not** beat the CONTINUOUS benchmark; the superseded 2.57 / t 4.87 figures are wrong.
Detail: [`carry_hold.md`](carry_hold.md),
[`research_findings.md`](research_findings.md).

## CONTINUOUS — `continuous_ensemble_v2` (off)

**Read the numbers from the profile resolver, not the dataclass.**
`apply_continuous_demo_profile()` (`continuous_demo.py:1604`) is the resolution function
every runtime path goes through (`continuous_demo_daemon.py:175`,
`operational_profile.py:502`, `freeze_account_candidate_universe.py:69`,
`cli_parsers.py:360`) and it overrides seven `ContinuousDemoCycleConfig` defaults:
`rmom_quantile` 0.33→0.25, `feature_set` (rv_168h, vov, dist_low, xsret7, xsret3)→
(`max_ret168`,), `max_hold_hours` 48→24, `sizing_mode` flat→inverse_vol,
`target_vol_per_name` 0.02→0.01, `vol_weight_clamp` 3.0→2.0,
`entry_btc_risk_sizing_enabled` False→True.
[`configs/operational.demo.json`](../configs/operational.demo.json) then overrides
`max_active` (25→1), `max_new_entries_per_cycle` (5→1), `btc_trend_gate`, `entry_leverage`,
`notional_multiplier` and `per_position_notional_pct_equity` on top of that. Reading the
dataclass gives seven wrong numbers.

Deployed revision string `active_single_fund0_tp12_sl35_v1` (`continuous_profile.py:16`),
artifact cell `age240_turn3pop3_fund0_crowd2`, standard run label
`continuous_ensemble_v2_active_single_fund0_tp12_sl35_v1_historical_equity`
(`CONTINUOUS_HISTORICAL_RUN_LABEL`), history start 2023-04-01, evidence label
`exploratory_historical_equity`. `regenerate_hedge_warmstart.py:162,203` and
`continuous_deployed_equity_refresh.py:480` refuse inputs whose `profile_revision` is not
that string. The single funding-gated cell replaced three nested-trigger components
(`turn3_pop3` weight 1/3, `turn4_pop3` 2/9, `turn4_pop5` 4/9).

**Signal.** Shorts decile 9 of the hourly composite through one component
(`p3` / `turn3_pop3`), after: a 1-hour confirmation delay on closed bars, the causal
prior-day 30d BTC uptrend gate, **stable** residual momentum in the lowest quartile,
≥500,000 USDT hourly turnover, a 240-day listing-age floor, and the settled-funding
admission (last settled print at signal-bar close ≥ 0 — only fade pumps whose longs are
paying; settled history only, and no observable print admits and is counted as an unknown
admit).

"Stable" is load-bearing: `require_stable_residual_momentum`
(`continuous_events.py:197-240`) returns only `is_provisional == false` rows, and raises if
the source lacks a boolean `is_provisional` column, has null provenance, non-daily or
duplicate `(symbol, ts_ms)` keys, or null/non-finite `residual_momentum`. A separate
freshness guard (`_assert_rmom_covers_window`, `RMOM_COVERAGE_TOLERANCE_DAYS = 2`) raises
when the table lags the kline window by more than 2 days, because the decile build
left-joins on `(symbol, day_ts)` and a stale table would silently drop every symbol on
recent days — hence a hard STALE failure rather than an empty decile.

Three admission guards, all scoped **per component book** for parity with the
independent-books research engine: an entry-anchored re-entry cooldown
(`entry_reentry_cooldown_enabled=True`, `cooldown_ms = hold_ms`, so a fast take-profit close
cannot re-enter on the next hourly signal), the crowd-2 gate
(`entry_crowding_max_fresh = 2`), and one entry per component per `signal_ts` window. A
sibling component may therefore complete a capacity-truncated stack later in the same
window; a lifecycle whose trade id cannot be attributed to exactly one component fails safe
to the `*` wildcard (`_ALL_COMPONENTS`, `continuous_demo.py:877-911`) and blocks every
component for that symbol. Simplifying the scoping to per-symbol would silently change the
live book relative to its backtest. Rejections surface as `first_rejection_reason` inside
`entry_funnel_json` / `qualified_but_blocked_json` and as `entry_first_rejection_reason` at
cycle level; the value set is `d9`, `liquidity`, `event`, `age`, `funding_admission`,
`crowding`, `already_reserved`, `entry_cooldown`, `same_signal_reentry`, `capacity`,
`target_intent_validation`, `unresolved_account_target`, `terminal_entry_attempt`,
`target_publication`.

New entries pause while the journal shows ≥8 adverse reduction batches in 1,440 minutes
(`entry_pause_after_adverse_exits=8`, `entry_pause_window_minutes=1440`, `:170-176`). The
pause gates new entries only — existing targets are untouched and run to their normal
exits. It is a correlated-squeeze breaker counting `stop_approach`, `failed_fade` and any
net-negative cover, recomputed from the ledger each cycle (`entry_circuit_breaker_tripped`,
`:843-857`), so it lifts on its own as the cluster ages out; restarting the producer to
clear it is unnecessary. The two constants are an operational guardrail, not a validated
optimum — do not re-optimize them on backtest data. 0 disables it.

Each cycle also persists an **observer-only** component funnel and identity block
(`:2790-2840`): `entry_observability_scope="observer_only_no_admission_authority"`,
`entry_funnel_d9`, `entry_funnel_liquidity`, `entry_funnel_event`, `entry_funnel_age`,
`entry_funnel_funding`, `entry_funnel_available`, `entry_funnel_capacity`,
`entry_funnel_json`, `funding_admission_rejected`, `funding_admission_unknown_admitted`,
`qualified_but_blocked_count` / `_symbols` / `_json`, `entry_preselection_rejection_reason`,
`entry_first_rejection_reason`, `entry_paused`, `recent_adverse_exits`,
`entry_feature_state_sha256`, `entry_feature_contract_sha256`, `rmom_source_sha256`,
`rmom_signal_day_sha256`, and `temporarily_ineligible_candidates_json` (also written by the
LONG producer at `long_native_event_demo.py:530`). These are the fields to grep when
diagnosing an empty cycle or a vanished symbol. None of them grants admission authority or
bypasses the BTC, account-health, pause, capacity or account-risk gates — never wire funnel
state into the admission path.

**Sizing.** `equity × 2% × notional_multiplier × component weight × inverse-vol × BTC-risk`.
The shared profile sets `notional_multiplier` 1.0 and `per_position_notional_pct_equity`
2.0, and the single active component `p3` / `turn3_pop3` carries `weight = 1.0`
(`continuous_profile.py:53`). Entry leverage is 2.0: the multiplier changes target
quantity, leverage changes only margin. Inverse-vol is `0.01 / rv_168h` clamped to
[0.5, 2.0]; missing volatility uses 1.0. The BTC-risk overlay (arm id
`CTRL_BTC_RISK_70_90_35`, `continuous_btc_risk.py:25`, `BTC_RISK_MIN_PRIOR = 50`) starts
after 50 accepted decisions and multiplies by 0.35 when the causal score sits in
[0.70, 0.90); the 0.35 applies to every component sharing the decision key
`{symbol}|{signal_ts_ms}`, duplicate keys raise, and live state is reconstructed by
replaying accepted account targets in receipt-chain order from
`btc_risk_sizing_state.parquet` — which is why a research run cannot reconstruct it.
[`configs/operational.demo.json`](../configs/operational.demo.json) currently holds the
book to `max_active: 1` and one new entry per cycle.

**Exit.** 12% take-profit off fill VWAP, a declared 35% stop placed at the venue, and a
zero target 24 hours after the first attributable fill. The 35% stop is modeled identically
in the research engine, and it **replaces** rather than supplements the account owner's
disaster stop: `venue_protection.py:280-321` installs one Full-position exchange-native stop
per net symbol from the *outermost* declared `stop_loss_pct` among the symbol's components,
anchored to entry fill VWAP, tagged `fill_anchored_outermost_component_stop`; it falls back
to `explicit_account_fallback_fraction` off `average_price` only when no component declares
one. A declared value outside (0, 1) raises rather than silently widening to the fallback,
and `continuous_demo.py:2237-2241` rejects such a component book at startup. CARRY declares
the same 0.35.

**Limits.** The standard historical curve reproduces the component book, the funding
admission, inverse-vol sizing, TP12, the 24h hold, the 35% component stop, and the BTC+ETH
hedge with its regime. It does not reproduce the live accepted-decision BTC-risk state,
account risk admission, venue rules, fills, or reconciliation. There is no paper hedge
service, so any paper CONTINUOUS book is an unhedged-book result and cannot be compared
against that hedged curve. A data root named `full_pit` establishes nothing about
membership ([`data.md`](data.md)).

## Hedge (off with CONTINUOUS)

Small long BTC and ETH positions sized to the CONTINUOUS short book's causal rolling beta:
90-day window, 60-observation minimum, 2.0 per-leg cap, 5 bps modeled cost, 30%
total-equity sanity cap, BTC-vol intensity `lam=0.5` over a 30-day vol window and 250-day
percentile window. Daily volatility rebalance is disabled. The timer fires every 5 minutes
and is enabled only while CONTINUOUS is.

There is also a **joint total** cap of `hedge_cap * scale` = 2.0 × scale, separate from the
per-leg 2.0: `_capped_hedge_legs` (`continuous_rebalance.py:359-376`) clips each leg to
`hedge_cap` and then, if `r1 + r2` exceeds `hedge_cap * scale`, shrinks both legs
proportionally. Only after that does the manager apply the 30%-of-equity cap, again
proportionally (`continuous_hedge_manager.py:297-300`). The worst-case combined hedge ratio
is 2.0, not 4.0.

Every published hedge target carries the model-prior provenance stamp — eight fields from
`HedgeModelPrior.provenance()` (`continuous_hedge_manager.py:88-98`): `model_prior_kind` =
`immutable_historical_model_prior`, `model_prior_artifact_sha256`,
`model_prior_source_summary_sha256`, `model_prior_start_date`,
`model_prior_data_through_date`, `model_prior_rows`, `model_prior_live_extension` = `False`,
and `model_prior_evidence_scope` =
`sizing_only_not_current_calibration_or_performance_evidence` — the field that forbids
citing the prior as calibration or performance evidence. A refresh changes
`model_prior_artifact_sha256`. The stamp is spliced into each target's intent metadata
(`scripts/runtime/run_continuous_hedge.py:114-117`) and the runner status JSON (`:451`). Missing,
malformed, future-dated or estimator-inadequate prior data fails closed. Prior age is
informational, not a freshness gate; coefficient drift remains a stated limitation. The
demo hedge sizes current BTC/ETH targets from live canonical CONTINUOUS gross exposure,
current account equity and current prices — only the beta and BTC-vol regime inputs come
from the commit-owned prior.

Betas are rolling OLS over the trailing 90 ledger days of
[`bybit_warmstart.csv`](../deploy/hedge_warmstart/bybit_warmstart.csv) (200 rows, data
through 2026-07-09). The runtime never extends that prior with live returns: the live
account path cannot reconstruct the regression's per-unit book return. Regeneration runs
via [`regenerate_hedge_warmstart.py`](../scripts/maintain/regenerate_hedge_warmstart.py) after each
research refresh of the continuous equity pipeline, at least quarterly, from the
code-defined TP12 component ledgers. `ContinuousHedgeRule` supports `shrinkage_weight` /
`prior_beta_1` / `prior_beta_2` (`beta = (1−w)·OLS + w·prior`), previous vintage as prior,
`w = 0.3` intended at the first refresh; the deployed value is `0.0`, so enabling it is a
committed change. No refresh has run yet — the deployed vintage is still 2026-07-09.

*Regeneration gates.* The script has exactly four refusal conditions (`overwrite_blocked`,
`scripts/maintain/regenerate_hedge_warmstart.py:413-435`, in this order): no date overlap with the
existing CSV; `max|delta_unit_ret|` over the overlap above `--max-unit-drift` (default
1e-3); regenerated row count below the existing row count; and beta drift above
`MAX_PRIOR_BETA_DRIFT`. A 60-observation minimum is **not** one of them — regenerating from
a short component ledger writes the CSV, and the runtime then silently produces a zero beta
from it. (The number 60 lives in two unrelated places: `beta_min_obs = 60`, runtime
estimator behavior; and `MIN_OBJECT_REFERENCE_OVERLAP = 60` (`:67`), reached only on the
`--replace-component-object` path.) `MAX_PRIOR_BETA_DRIFT = 0.25` is in hedge-ratio units —
the same units as the 2.0 per-leg cap — measured as `max(|new_b1 − old_b1|, |new_b2 −
old_b2|)` between the deployed CSV's betas and the candidate's betas, each **re-estimated**
with the runtime estimator (`_series_betas` → `compute_hedge_betas_2f`, 90d window / 60-obs
minimum) at the end of its own series; it is skipped when the two vintages share no dates
(`:430`, `_beta_drift` at `:391-400`). `--force` is the only override **and requires a
written review before use** — the refusal message points at this document for it.

*Estimator behavior, not refusals.* Three paths return `(0.0, 0.0)`: `idx −
beta_extra_lag_days <= 0`, fewer than `beta_min_obs` = 60 joint rows, and zero variance in
either leg. Collinearity is different: when the two legs are collinear within the window
(`|corr| > HEDGE_2F_COLLINEARITY_GUARD = 0.995`, `continuous_rebalance.py:303, 349`) or the
2×2 system is singular (`det <= 0`), `compute_hedge_betas_2f` falls back to the
single-factor beta on leg 1 (BTC) with `b2` exactly `0.0`; shrinkage still applies to that
leg-1 beta but `prior_beta_2` is ignored (`:350`). BTC and ETH daily returns routinely
correlate above 0.9, so a hedge target with a nonzero BTC leg and a zero ETH leg is the
estimator deliberately collapsing to one factor, not missing ETH data.

## Shared machinery

[`configs/operational.demo.json`](../configs/operational.demo.json) is the one editable sizing
surface. Caps are a fraction of observed wallet equity
([`equity_anchored_envelope.py`](../liquidity_migration/equity_anchored_envelope.py):
contraction immediate, expansion behind a dead band, unknown equity moves nothing);
[`account_kernel.py`](../liquidity_migration/account_kernel.py) holds each sleeve to its own
partition of it; [`account_loss_guard.py`](../liquidity_migration/account_loss_guard.py) halts
the day at a loss ceiling. The paper twin's fixed capital base comes from the same file:
`scripts/deploy_vps_live.sh:503` sets `PAPER_EQUITY_USDT =
load_operational_profile(...).capital_reference_usdt` (currently 250,000), with no per-host
tuning — every percentage return or drawdown read off the paper book is against that number.

**Profile load refusals** (all in
[`operational_profile.py`](../liquidity_migration/operational_profile.py)): unknown or
missing fields in any block (`_object`, `:44-50`); any producer `entry_leverage` above
`account_risk.max_leverage` (`:447-453`); an account gross cap above `capital_reference_usdt
× max_leverage` (`:455-458`); an initial-margin cap above `capital_reference_usdt`
(`:459-462`); a symbol cap above the component cap, or a component cap above the account cap
(`:309-312`); a LONG full-book margin projection above its own
`max_projected_initial_margin_pct_equity` (`:493-500`); and any registered
LONG/CONTINUOUS/CARRY envelope — per-symbol, combined gross, combined margin — outside the
account caps (`:558-568`). When `sleeve_limits` is declared, each producer envelope must
also fit its own share, and a sleeve with a non-zero envelope but no share is refused
(`:570-594`). The validator is `_validate_profile_envelopes` and it re-runs on the
equity-rescaled profile (`:682`), not only at load. Separately: a normal risk or venue-rule
rejection when live account state differs from the validation reference is a safety
decision, not configuration drift — do not "fix" it by raising caps.

Turnover, listing age, and rank are re-evaluated every cycle, so a
symbol can be skipped without disappearing; a newly observed future `deliveryTime` drops it
from new-entry membership, and retiring it requires position, component targets, component
desires, working orders, the aggregate target, **and** the unresolved inbox all flat for
that symbol (`_assert_scheduled_retirement_flatness`,
`account_candidate_universe.py:1225-1279`); any remainder raises `scheduled-retirement
symbols are not account-flat`. The private retirement registry preserves the delivery
observation after the venue removes the instrument row, and a moved delivery date updates
the record in place while keeping the original first-observed timestamp as the causal
anchor. A symbol that leaves the live population *without* delivery evidence does not fail
the cycle: it drops to journaled temporary ineligibility and returns automatically when the
venue restores it. Reasons are `turnover_below_floor`, `listing_age_below_floor`,
`listing_age_above_ceiling`, `outside_configured_liquidity_rank`,
`unexplained_absence_from_venue`, and `scheduled_retirement_reentered_eligibility` (a
cancelled or moved delisting, which leaves the symbol non-tradable while its delivery
evidence stands). Malformed eligibility input still raises. The distinction matters: a venue
hiccup that self-heals must not be intervened on.

The frozen candidate artifact is a forward population contract. The active set is the
intersection of the frozen per-profile population with the current live population
(`account_candidate_universe.py:1200-1205`), so a post-freeze listing can never enter until
someone re-freezes, while turnover, listing age and configured liquidity rank are
re-evaluated every cycle and a frozen symbol failing one of those dynamic filters is skipped
with its exact reason written to the cycle receipt
(`temporarily_ineligible_candidates_json`) — normal ranking movement, distinct from
disappearance.

Bybit's demo realm rejects orders its own published
`minNotionalValue` accepts, so
[`demo_rule_probe.py`](../liquidity_migration/demo_rule_probe.py) measures the executable
minimum with bounded probe orders (≤200 USDT, 100 bps away) and caches it per symbol; entry
dust skips key off that. A component below 4× that minimum is quantization-distorted, so a
day where such components carry >20% of gross exposure measures plumbing rather than
economics ([`research_findings.md`](research_findings.md)).

Grading rules and the claim boundary are in [`AGENTS.md`](../AGENTS.md); mainnet arming is
[`real_money.md`](real_money.md).
