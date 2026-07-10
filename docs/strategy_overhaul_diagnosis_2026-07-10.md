# Strategy Overhaul Diagnosis

Date: 2026-07-10

Status: diagnosis plus synthetically hardened outcome-blind scout scaffolding;
one local-root Phase 0 returned `NOT_READY`, with no outcome read or production
parameter change

The current strategy should not be “optimized harder.” Its largest weaknesses are
measurement, state semantics, dependence, and risk calibration. A broad gate-off
equity curve would produce more trades, but it would confound the very mechanisms
we need to identify. The first overhaul is therefore the research object: one
causally timestamped population tape, one unique event unit, raw paths before
portfolio choices, and every inherited gate retained as data.

The proposed feasibility program is
`docs/preregistration/strategy-overhaul-scout-2026-07-10.md`.

## Decisions We Can Make Now

### Correctness repairs do not need an alpha contest

These are implementation/accounting defects or unresolved parity gaps, not
candidate strategies:

1. CONTINUOUS TP and deadline are planned from the pre-order reference. The fill
   updates `entry_price` without rebasing TP or the holding clock. The live object is
   therefore not literally TP12/24h from actual entry.
2. Same-symbol component orders are submitted sequentially even though the three
   predicates are nested. This creates avoidable within-batch price dispersion and
   makes the declared component order a capacity allocator.
3. Research gives each component a separate capacity state; live mixes unique held
   symbols and component legs under a global five-leg cycle cap. Those are different
   portfolios.
4. Candidate tapes are emitted only after major alpha gates. They cannot audit the
   population from which the strategy claims to select.
5. LONG emits MAE/MFE as unmeasured. Any tail or exit diagnosis using those zeros as
   observations is invalid.
6. Error/missing-data paths in sizing and risk should be explicit and fail according
   to their risk consequence. Returning silently to full size is not a neutral
   fallback.

Code changes still need tests, demo/paper parity, and a controlled release. They do
not need a favorable backtest to establish that position protection and clocks must
reference the position that was actually filled.

### No size increase is supported

The current forward execution tape contains only five paired component rows. Mean
adverse entry slippage is 72.13 bps, median 136.96 bps, and worst 170.73 bps; one
favorable fill makes the mean look much better. This is enough to reject the current
20–23 bps modeled round-trip cost as a calibrated point estimate, but nowhere near
enough to estimate a replacement.

The current inverse-vol rule also is not demonstrated tail control. It is an
arithmetic exposure transform with clamps, not a calibrated forecast of future MAE,
ES, jump risk, or liquidation loss. The registered disaster-budget experiment is
still the appropriate narrow sizing study once its data boundary is ready.

## Parts That Need Overhaul

| Surface | Naive current assumption | What must be learned or repaired |
| --- | --- | --- |
| CONTINUOUS event definition | Three components are treated as separate alphas | Treat nested p3/p4p3/p4p5 as one event with continuous strength and measure incremental information |
| CONTINUOUS score | Conditional D9 of `max_ret168` is presumed to add quality | Measure whether it adds anything after the current pump and turnover already embedded in the trigger |
| Residual momentum | q25 is a useful hard cutoff | Estimate the continuous conditional surface, peer-count reliability, and venue/time stability |
| Regime | A binary prior-BTC sign is the right estimand | Retain endpoint, log, simple-sum, volatility, breadth, and dispersion continuously; learn whether regime belongs in alpha, sizing, or neither |
| Age | 240 days is a safety/quality boundary | Reconcile root-first-bar and venue-launch definitions; measure age conditional on liquidity/event strength and migration/rename cohorts |
| Liquidity/capacity | One signal-hour turnover is both alpha gate and ADV | Separate population economics from executability; calibrate fills with depth, participation, order size, and latency |
| Entry | Ideal next close approximates a real fill | Build a decision-to-fill clock and conditional slippage/fill model; compare aggregated versus sequential same-symbol orders |
| Exit | TP/hold choices can be judged from selected trades | Use common-anchor MFE/MAE and first-passage paths; distinguish alpha realization, risk truncation, and intrabar ambiguity |
| Sizing | Inverse trailing volatility equalizes risk | Test forecast calibration to realized variance, MAE, ES, and jumps; use monotone loss budgets for capital constraints |
| BTC risk overlay | A fitted non-monotone percentile mapping is safety | Separate alpha-conditioned sizing from monotone portfolio risk limits; batch-freeze state and expose missing/error behavior |
| Portfolio | Fixed component weights provide diversification | De-duplicate decisions, aggregate symbol targets, estimate effective bets/correlation clusters, and allocate on a common capital clock |
| Hedge | Historical ledger beta transfers to the current live book | Measure holdings-based event-time exposures, minimum-order dead zones, tracking error, funding, and basis under stress |
| LONG entry gates | FC thresholds and regime filters jointly define quality | Emit every gate marginally and sequentially; characterize raw pump, volume, sigma, close location, ATR, age, and regime |
| LONG retrace | One-percent/six-hour fall-through improves entry | First reconstruct only the exact close-based policy; alternate retraces are later registered outcome views, not feature inputs |
| LONG exits | ATR 1.5/4.0 and 3d hold explain the edge | Reconstruct hourly first passage, same-bar ambiguity, and TP-tail contribution from the full pre-gate population |
| Inference | Trade/component rows are independent evidence | Use unique decisions, simultaneous event waves, daily clusters, calendar blocks, and matched cross-venue events |

## Ordered Big-PC Program

1. **S00 — outcome-blind feasibility.** Freeze commit/config/source inventory;
   measure field/PIT provenance and root support; estimate rows, storage, memory, and
   runtime; propose schemas and minimum support without reading future labels.
2. **S01 — two small child contracts.** Freeze separate CONTINUOUS-A0 and LONG-A0
   run IDs, exact objects/hashes, schemas, timing/rank rules, finite feature lists,
   minimal labels, dependence blocks, negative controls, and analysis formulas.
3. **S02 — signal-time feature tapes.** Emit pre-filter symbol-hour/symbol-day
   features by venue/month and preserve missingness. Reconstruct only static
   candidate/classifier/component decisions and first rejection from information
   available at the signal; stop on any parity failure. No post-signal entry price
   belongs in this tape.
4. **S03 — entry anchors/policies.** For LONG, write the separate exact
   close-based retrace/fall-through reconstruction and prove entry-policy parity.
   CONTINUOUS writes only its frozen next-close anchor. No path outcome is read.
5. **S04 — minimal path labels.** Append the registered common/current-anchor
   1h/24h/72h returns and 24h/72h MFE/MAE plus completeness/ambiguity in separate
   keyed artifacts. No costs, sizing, or portfolio PnL.
6. **Post-S04 finite initial analysis.** Run only the child-manifested support/gate
   attrition, component overlap, small univariate calibration set, LONG entry-policy
   selection, fixed controls, and block-aware uncertainty.
7. **Hypothesis dossier.** Advance at most two hypotheses per sleeve, labelled
   `hypothesis_positive`, `hypothesis_negative`, or default `unidentified`. Preserve
   every null/failed view and write one prospective falsifier per advanced claim.
8. **Later contracts.** Keep exhaustive exit surfaces, granular adverse-state work,
   sizing, forward execution, and cross-sleeve portfolio/hedge optimization separate.
   Scout associations do not become trading rules retroactively.

## Current Implementation Boundary

The code now closes several mechanical leakage and schema gaps, but only on
synthetic fixtures:

- CONTINUOUS raw features accept a narrow source projection and split each
  symbol into exact consecutive-hour segments, so rolling returns, volatility,
  maxima, turnover diagnostics, spells, waves, and labels cannot bridge an
  interior hourly gap. A diagnostic S02 orchestrator requires separate exact
  source/warmup and retained signal-window key inventories, an exact stable-RMOM
  source with source-day and provisional-state provenance, supplied venue-local
  identity/PIT inputs whose semantics still require canonical receipts, and emits
  the exact registry-typed 196-field schema. Stable RMOM[D]
  receives a mechanically derived causal-computability time of
  `D - 1 day + 1 hour` under the frozen shift-3 target construction; provisional rows
  remain unavailable. This does not claim historical publication, ingestion, or
  operational latency. S03 entry anchors and S04 minimal paths are separate exact
  projections and reject anchor or stage tampering.
- LONG now binds every stage to the exact runtime v11a config; canonicalizes
  `signal_ts_ms`; rejects duplicate, null, negative, off-grid, or malformed
  consumed keys/OHLC; checks the daily close against the exact signal-hour
  close; re-derives exit and entry geometry before accepting downstream input;
  and reserves the registered S04 path for the frozen 1h/24h/72h point and
  24h/72h excursion horizons. Arbitrary horizons are explicitly exploratory.
  The exact S02 wrapper validates supplied population/age and a mechanical
  raw-OHLC sidecar builder reconstructs causal availability, BTC/ETH regime, and
  configured BTC-month context without converting missing source state into a
  value. The wrapper recomputes rank metadata and emits 138 fields. S03 emits only its 30 entry
  fields; S04 consumes exact S02+S03, reconstructs their geometry, and emits only
  the 71 registered labels.
- The central artifact projector enforces exact field order, Polars dtype,
  declared non-nullability, and registered key uniqueness. The proposed
  registry is now v4: its implementation vocabulary distinguishes builder,
  passthrough, adapter, projection, missing, and semantic mismatch, and each
  implemented/derived field records its `source_columns` lineage. No current
  field is missing or semantically mismatched; seven receipt/provenance blockers
  remain.

This hardening does not establish data support or strategy merit. The local
outcome-blind bundle `strategy-overhaul-phase0-bccefdfc38ae9fda3c17` returned
`NOT_READY` and was internally re-executable, but it did not authenticate its
sources or roots and authorized no downstream action. It found a seven-day
Binance partition gap, provenance-unknown Binance membership, unavailable
Binance RMOM provisional state, 360 source-unlabelled Bybit kline rows, missing
canonical root lineage, an unconsumed partial auto map, and unwired S02 config
parity. No S02 feature tape, S03 entry artifact, S04 path-label artifact, return,
MFE/MAE, PnL, or other outcome has run. The derived RMOM timestamp proves only
offline causal computability; the RMOM source-day, provisional-state, and
root-content provenance are not yet authoritative and receipt-bound. The
  independently expected population and config identities are not yet verified
  inside every stage wrapper. The root-snapshot utility is only a byte-binding
  precursor: it does not verify registered scope, earliest history, Phase-0
  semantics, or S01 readiness. The generic stage-receipt utility likewise binds
  bytes and declared metadata but does not validate the caller's artifact schema,
  key/window semantics, outcome-blindness claim, or all transitive identities.
  Consequently the mechanically derived LONG sidecars and PIT/map/rank-population
  inputs are not yet linked by a canonical semantic receipt chain. Those are
  blocking identity debts, not caveats that a favorable later result can waive.

## Provisional Architecture Hypotheses

These are directions to test, not conclusions:

- one unique pump-event state with a continuous strength/calibration surface instead
  of three nested pseudo-components;
- continuous alpha estimates with uncertainty, followed by a separate monotone risk
  allocator, instead of stacked hard filters and non-monotone fitted multipliers;
- one batch portfolio optimizer that nets same-symbol targets, cluster heat,
  liquidity, margin, and hedge exposure before creating orders;
- fill-based state and protection clocks shared by research, paper, and demo;
- event-time execution/capacity models calibrated from actual order lifecycle data;
- LONG and CONTINUOUS allocated together on a common capital and stress clock.

The data may reject any of these. The point of the scout is to make that rejection
possible before another attractive equity curve hardens a new arbitrary rule.
