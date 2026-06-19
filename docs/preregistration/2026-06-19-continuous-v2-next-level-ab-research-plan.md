# Pre-registration: Continuous V2 Next-Level A/B Research Plan

Date: 2026-06-19
Author: Codex
Stage: proposed program registration
Scope: continuous v2 fade book only

This document registers the next research program for the continuous v2 fade
book. It is not a permission slip to change demo/paper wiring, deploy code, or
claim real-money readiness. Every executable phase below still needs a dated
construction receipt before touching per-venue working datasets or running new
cells.

## Honest Starting Point

- Research stage only. `REAL_MONEY` stays false.
- Forward demo/paper remains the out-of-sample arbiter. Internal backtests are
  diagnosis and mechanism evidence, not promotion evidence.
- The currently wired continuous object is the operator override object:
  component TP 12 percent and daily vol-target rebalance disabled.
- That override is not a research win. TP12 helped Bybit and hurt Binance; the
  vol-off change removes the book's main daily risk control.
- Future tests must compare against both:
  - `V2_LIVE_RESEARCH_CONTROL`: post-override TP12, daily vol adjuster off.
  - `V2_EVIDENCE_ANCHOR`: pre-override TP10, daily vol adjuster on, max4.
- Previously closed mechanisms stay closed unless new data or lifecycle
  fidelity directly changes the mechanism being tested:
  - E1 sell-into-strength entry timing failed by adverse selection.
  - Binance-only flow is exploratory and cannot clear a two-venue bar.
  - Shorter-hold exit and time-decay exit variants failed by cutting the
    trades that reached the 10 percent TP.
  - Conviction sizing arms were beaten by hash controls.
  - TP12/TP15 are Bybit-positive but Binance-negative.

## Source And Methodology Anchors

Data and methodology references to use while building receipts:

- Bybit V5 kline endpoint: `https://bybit-exchange.github.io/docs/v5/market/kline`
  - Supports 1-minute intervals.
  - Returns candles sorted reverse by start time.
  - The close price is the last traded price if a candle has not closed.
- Bybit public trade archive: `https://public.bybit.com/trading/BTCUSDT/`
  - Daily trade CSV archives are available for BTCUSDT through at least
    2026-06-18 at the time this plan was written.
- Binance public data repository:
  `https://github.com/binance/binance-public-data`
  - Daily and monthly files are published for spot, USD-M futures, and COIN-M
    futures.
  - Checksum sidecars are published and must be validated.
- Binance USD-M futures kline endpoint:
  `https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data`
  - Klines are identified by open time.
  - Max REST limit is 1500 rows.
- Multiple-testing and overfit controls:
  - Probability of Backtest Overfitting / CSCV:
    `https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253`
  - Deflated Sharpe Ratio:
    `https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551`
  - Harvey, Liu, and Zhu on multiple testing:
    `https://doi.org/10.1093/rfs/hhv059`
- Crypto execution benchmarks to review before final fill calibration:
  - TWAP/VWAP execution references should be used only for benchmark framing;
    actual cost assumptions must come from demo/paper fills and local depth data.

## Objective

Build an execution-realistic, point-in-time research platform for continuous v2,
then test a small set of mechanism-specific A/B books that were impossible or
under-specified under the prior 1h bar framework.

The first objective is not "find a better parameter." The first objective is to
make the platform capable of rejecting false improvements caused by:

- 1h bar path ambiguity.
- Hidden lookahead in sub-hourly features.
- Unrealistic market-order fill assumptions.
- Venue-specific microstructure effects.
- Multiple-testing and hash-control artifacts.

## Non-Objectives

- No real-money recommendation.
- No broad unregistered parameter search.
- No revival of the erased daily SHORT sleeve.
- No single-venue candidate can become a two-venue candidate.
- No internal backtest result can promote code or reset demo/paper evidence by
  itself.
- No A/B cell may be run before its data roots, feature timestamps, engine
  semantics, costs, and null controls are registered.

## Global Evidence Gates

Every future construction receipt and run must declare:

- `decision_ts`: timestamp when the strategy decides.
- `data_available_ts`: timestamp when each feature is known to the strategy.
- `order_submit_ts`: timestamp when the order is sent.
- `fill_window`: simulated or observed fill interval.
- `exit_activation_ts`: timestamp when stops, TP, or time exits become active.
- `state_initialization_ts`: timestamp used to seed portfolio and feature state.

Every run must produce:

- Immutable config hash.
- Data-root manifest and source identity.
- Feature manifest with availability rules.
- Trade ledger.
- Order/fill ledger.
- Cost and funding ledger.
- Ambiguous-bar ledger.
- Null-control ledger.
- Bootstrap/stress report.
- Replay command and environment capture.

Run labels:

- `exploratory`: can diagnose mechanisms only.
- `candidate`: can become a forward-shadow candidate only after passing all
  preregistered tests.
- `paper_ready`: can be considered for a no-order or paper shadow only after a
  separate operator-gated receipt.

## Decision Rules

An A/B arm can be called a candidate only if all conditions hold:

- It improves pooled MAR versus both `V2_LIVE_RESEARCH_CONTROL` and
  `V2_EVIDENCE_ANCHOR`.
- It does not make either venue materially worse on MAR, drawdown, worst day,
  or cost-adjusted return.
- It has enough trades to avoid one or two path-dependent winners dominating
  the result.
- Bootstrap left-tail deltas are not negative enough to erase the edge.
- Hash controls and delayed-feature controls fail to explain the improvement.
- Ambiguous intrabar resolution is not concentrated in the improved trades.
- Cost, funding, and capacity stress do not flip the result.
- The mechanism explanation matches the ledger evidence.

If a venue split appears, the default verdict is "no two-venue candidate." A
venue-specific paper shadow needs a separate operator decision and a new receipt.

## Phase 0: Freeze Baselines And Receipts

Purpose: stop control drift before new infrastructure work begins.

Artifacts:

- `baseline_manifest.json`
- `baseline_replay_bybit.csv`
- `baseline_replay_binance.csv`
- `baseline_diff.md`
- Config hash for both controls.
- Exact replay command for both controls.

Planned cells:

- `P0A_V2_LIVE_RESEARCH_CONTROL`
  - TP12.
  - Daily vol-target adjuster disabled.
  - Current operator override object.
- `P0B_V2_EVIDENCE_ANCHOR`
  - TP10.
  - Daily vol-target adjuster enabled with the prior max4 wiring.
  - Prior evidence anchor before the override.
- `P0C_CONTROL_DELAYED_FEATURES`
  - Same trades as the control, but with explicit delayed copies for features
    that could become sub-hourly in later phases.

Acceptance:

- The engine can reproduce the current 1h baseline before any 1m or trade-aware
  logic is enabled.
- Any mismatch is explained in `baseline_diff.md` before proceeding.

## Phase 1: Full 1-Minute PIT Data Foundation

Purpose: remove the current 1h path blind spot before testing stops, TWAP, or
intrabar exits.

### D1: Bybit Full 1m Root

Build a full 1m Bybit root for the continuous universe.

Required approach:

- Prefer public trade archives where available.
- Aggregate trade archives into dense `klines_1m`.
- Use V5 market kline only for gap fill or backstop, never as an unexplained
  replacement for local archive history.
- Record when REST candles are open/incomplete and exclude incomplete candles
  from decision features.

Expected code surfaces:

- `scripts/build_full_pit_bybit.sh`
- archive manifest helper
- ingestion module
- storage module
- data audit tests

Acceptance:

- 1440 expected 1m rows per complete UTC day per symbol, allowing only listed
  lifecycle gaps.
- Manifest-vs-kline lag report is produced.
- Open candle use is prohibited for decisions.
- Gaps are ledgered, not silently filled.

### D2: Binance Full 1m Root

Extend Binance Vision ingestion from hard-coded 1h behavior to interval-aware
1m ingestion.

Required approach:

- Support `--interval 1m`.
- Fetch USD-M futures monthly and daily kline archives.
- Validate checksum sidecars.
- Write canonical `klines_1m`.
- Derive and verify `klines_5m` and `klines_1h` from `klines_1m` where possible.
- Compare derived 1h bars to existing 1h roots before accepting the new root.

Expected code surfaces:

- Binance Vision ingestion helper.
- Archive manifest helper.
- Derived bar validator.
- Unit tests for interval paths and checksum handling.

Acceptance:

- 1m root is PIT-safe and checksum-validated.
- Derived 1h root matches the existing accepted 1h root within defined tolerance.
- Any mismatch is ledgered by symbol/day.

### D3: Ancillary And Flow Coverage

Purpose: distinguish "alpha failed" from "data did not exist."

Required coverage ledger:

- OI.
- Taker buy/sell volume.
- Liquidation/event flow.
- Depth snapshots, if available.
- Funding.
- Symbol lifecycle and listing state.

Acceptance:

- Every feature in the later almanac has one of:
  - two-venue full-market coverage,
  - explicitly single-venue exploratory status,
  - or "blocked" status.
- No feature can silently promote from single-venue exploratory to two-venue
  candidate evidence.

### D4: Data Quality Ledger

Required report path:

- `reports/continuous_v2_1m_data_audit_<date>/coverage_by_symbol_year.csv`
- `reports/continuous_v2_1m_data_audit_<date>/gap_minutes.csv`
- `reports/continuous_v2_1m_data_audit_<date>/manifest_vs_kline_lag.csv`
- `reports/continuous_v2_1m_data_audit_<date>/source_identity.json`

Acceptance:

- Report is cited by the construction receipt before any A/B run.
- The report includes both venue-level and symbol-level pass/fail flags.

## Phase 2: 1m And Trade-Aware Execution Engine

Purpose: make stops, TP, TWAP, and event-driven order logic testable without
pretending 1h bars give order-path fidelity.

### X1: Intrabar Path Engine

Add a registered `intrabar_resolution` setting:

- `1h`: current engine behavior and baseline reproduction mode.
- `5m`: compatibility mode for known 5m datasets.
- `1m`: default next-level research mode.
- `trade`: future mode only when trade tapes and cost logic are complete.

Rules:

- Stops and TP must resolve using the selected intrabar path.
- If both stop and TP are touched in the same resolution bucket, choose
  adverse-first unless a stricter registered path rule is available.
- Ambiguous exits must be recorded in an ambiguity ledger.
- Exit activation timing must be explicit.

Acceptance:

- `intrabar_resolution=1h` reproduces the accepted baseline.
- 1m mode changes only path-dependent trades.
- Ambiguous same-bucket events are visible in the ledger.

### X2: Order And Fill Ledger

Every simulated order must create:

- Decision row.
- Intended order row.
- Fill row or non-fill row.
- Slippage row.
- Fee row.
- Funding row.
- Position-state row.

Minimum fields:

- venue
- symbol
- side
- signal_ts
- decision_ts
- order_submit_ts
- order_type
- intended_qty
- fill_qty
- fill_ts
- fill_price
- mark_price
- best_bid
- best_ask
- spread_bps
- participation_bps
- fee_bps
- funding_bps
- model_cost_bps
- realized_cost_bps, when demo fill exists

Acceptance:

- A replay can reconcile entry, exit, cost, and funding effects from ledger rows
  alone.

### X3: Fill And Cost Calibration

Use demo/paper fills as calibration data, not as alpha evidence.

Required outputs:

- Fill-latency distribution.
- Slippage distribution by venue, symbol liquidity, side, and order size.
- Spread and impact model.
- Stress settings for 1x, 2x, and 3x realized cost.
- Capacity limit estimate.

Acceptance:

- No A/B arm can become a candidate if it only wins under zero-cost or
  uncalibrated market-order assumptions.

## Phase 3: Feature Almanac V3

Purpose: rebuild the feature library with explicit sub-hourly availability and
null diagnostics.

Feature families:

- 1m path features:
  - excursion before entry,
  - excursion after entry,
  - wick pressure,
  - intrabar volatility,
  - gap and jump flags.
- Flow features:
  - taker imbalance,
  - residual taker imbalance,
  - flow acceleration,
  - venue divergence.
- Liquidity features:
  - spread,
  - depth,
  - impact proxy,
  - funding stress.
- Squeeze/crowding features:
  - liquidation clusters,
  - OI change,
  - funding/OI disagreement.
- Regime features:
  - BTC trend,
  - BTC realized volatility,
  - alt breadth,
  - cross-sectional dispersion.
- Lifecycle features:
  - age since listing,
  - archive coverage state,
  - symbol liquidity regime.

For every feature:

- Define source.
- Define timestamp.
- Define `data_available_ts`.
- Define missing-value rule.
- Define delayed-copy diagnostic.
- Define hash/null control when used for sizing, admission, or exit.

Acceptance:

- No feature can enter an A/B cell without a row in the almanac manifest.
- Delayed-copy tests must be runnable from the same config.

## Problem Book A: Real Stops And Dynamic TPSL

Purpose: test whether the book can keep the fade edge while cutting tail events
using 1m path fidelity.

Arms:

- `A0_CONTROL`: baseline TP/time exits only.
- `A1_CATASTROPHIC_1M_STOP`: wide emergency stop, adverse-first path rule.
- `A2_DELAYED_ARM_STOP`: stop arms only after initial mean-reversion window.
- `A3_VOL_SCALED_STOP`: stop distance from pre-entry realized vol.
- `A4_STOP_TO_HEDGE`: hedge intensity increases instead of closing the name.
- `A5_DYNAMIC_TP_VOL`: TP expands/contracts by pre-entry vol regime.
- `A6_TP_LADDER`: partial TP at first threshold, runner at second threshold.
- `A7_HASH_TPSL`: same distribution as dynamic TPSL but hash-assigned.

Primary tests:

- MAR delta versus both controls.
- Worst-day and max-drawdown delta.
- Tail-trade contribution.
- Ambiguous stop/TP count.
- Cost-adjusted return.

Candidate rule:

- Must reduce tail loss without deleting the trades that carry the book's edge.
- Must beat `A7_HASH_TPSL`.

## Problem Book B: Entry Alpha And Admission

Purpose: test only entry mechanisms that the prior 1h engine could not measure
properly.

Arms:

- `B0_CONTROL`: current admission.
- `B1_EXHAUSTION`: require 1m exhaustion before entry.
- `B2_FLOW_DIVERGENCE`: require venue-consistent flow divergence.
- `B3_SKIP_ACTIVE_SQUEEZE`: block entries during active squeeze conditions.
- `B4_PRIORITY_EXHAUSTION`: size/rank by exhaustion quality.
- `B5_WAIT_5M_15M`: deterministic delayed entry after signal.
- `B6_RANDOM_WAIT`: null with same wait distribution as B5.
- `B7_SYMBOL_HASH_PRIORITY`: null with same sizing distribution as B4.

Candidate rule:

- Any wait or priority arm must beat its random-wait or hash-priority null.
- Selling into strength remains closed unless the new feature definition proves
  it is not the same adverse-selection mechanism as E1.

## Problem Book C: Fixed TWAP And Event-Driven TWAP

Purpose: separate alpha from execution impact.

Arms:

- `C0_MARKET_CONFIRM`: current market-entry assumption with calibrated cost.
- `C1_TWAP_5M`: equal slices over 5 minutes.
- `C2_TWAP_15M`: equal slices over 15 minutes.
- `C3_TWAP_30M`: equal slices over 30 minutes.
- `C4_TWAP_60M`: equal slices over 60 minutes.
- `C5_FRONT_LOADED`: larger first slice, smaller later slices.
- `C6_BACK_LOADED`: smaller first slice, larger later slices.
- `C7_EVENT_LIQUIDITY`: slice only when spread/depth is favorable.
- `C8_ADVERSE_CANCEL`: cancel remaining slices when path moves against the fade.
- `C9_PASSIVE_FIRST`: maker-first simulation when depth/fill data supports it.
- `C10_RANDOM_SLICES`: null with same slice count and duration.

Candidate rule:

- Must improve implementation shortfall without losing too much alpha decay.
- Must beat random slicing.
- Must remain viable at stressed cost.

## Problem Book D: Rank Exits And Replacement

Purpose: test whether decayed cross-sectional rank can improve exits or capital
rotation after entry.

Arms:

- `D0_CONTROL`: TP/time exits only.
- `D1_RANK_DECAY_SHADOW`: no-order shadow of rank decay after entry.
- `D2_RANK_EXIT_AFTER_MFE`: rank exit only after minimum favorable excursion.
- `D3_REPLACEMENT_EXIT`: close weakest active name when a stronger new fade
  appears.
- `D4_RANK_HEDGE_INTENSITY`: change hedge intensity rather than closing.
- `D5_COMPONENT_SPECIFIC_RANK`: separate rules for p3, p4p3, and p4p5.
- `D6_HASH_RANK_EXIT`: hash-matched null.

Candidate rule:

- Must not reproduce the failed shorter-hold mechanism.
- Must beat hash rank exit and preserve TP-runner trades.

## Problem Book E: Dynamic TPSL And Winner Management

Purpose: revisit TP only after path fidelity and venue split diagnostics exist.

Arms:

- `E0_TP10_ANCHOR`: evidence anchor.
- `E1_TP12`: operator override TP.
- `E2_TP15`: wider TP.
- `E3_VOL_SCALED_TP`: TP from pre-entry realized vol.
- `E4_LIQUIDITY_SCALED_TP`: TP from liquidity/impact state.
- `E5_MFE_EXTENSION`: extend TP after favorable path confirms.
- `E6_TIME_TO_TP_HAZARD`: shrink/extend TP by observed time-to-TP hazard.
- `E7_PARTIAL_TP_LADDER`: take partial profit and let a runner continue.
- `E8_MATCHED_DYNAMIC_NULL`: same TP distribution assigned by hash.

Candidate rule:

- Must resolve the existing Bybit/Binance venue split.
- A Bybit-only win stays single-venue exploratory unless separately registered.

## Problem Book F: BTC Regime Filter And Sizing Replacement

Purpose: replace or repair the current BTC trend and vol regime controls without
creating hidden exposure concentration.

Arms:

- `F0_BTC_UPTREND_CONTROL`: current BTC uptrend gate.
- `F1_SOFT_TREND_SIZE`: scale size by trend confidence instead of hard gate.
- `F2_BTC_VOL_GROSS_SCALE`: gross exposure scales by BTC realized vol.
- `F3_BTC_VOL_HEDGE_SCALE`: hedge only scales by BTC realized vol.
- `F4_DRAWDOWN_AGE`: reduce size after recent strategy drawdown.
- `F5_ALT_BREADTH`: admission or sizing from alt-market breadth.
- `F6_REGIME_SPECIFIC_TPSL`: TPSL rules depend on regime.
- `F7_LATENT_REGIME_SHADOW`: no-order regime classifier shadow.
- `F8_HASH_REGIME`: null with same regime bucket frequencies.

Candidate rule:

- Must beat hash regime control.
- Must not improve only by lowering exposure during already-known bad windows.

## Problem Book G: Volatility-Control Rework

Purpose: recover daily risk control after the operator disabled the prior
vol-target adjuster.

Arms:

- `G0_CONSTANT_GROSS`: current vol-off override.
- `G1_PRIOR_MAX4`: previous daily vol-target max4 anchor.
- `G2_CLIPPED_SMOOTH_VOL`: slower vol control with tighter change limits.
- `G3_DRAWDOWN_ONLY_DERISK`: no vol target, only drawdown response.
- `G4_STRATEGY_MOMENTUM_DERISK`: reduce risk after strategy-level decay.
- `G5_REGIME_AWARE_VOL`: vol target changes by BTC/regime state.
- `G6_HASH_VOL_SCALE`: hash-matched exposure scaling null.

Candidate rule:

- Must improve risk-adjusted behavior without just suppressing trade count.
- Must beat hash exposure scaling.
- Must not create a hidden venue split.

## Problem Book H: Flow, Squeeze, And Venue Policy

Purpose: keep the real flow signal honest while preventing single-venue
over-interpretation.

Arms:

- `H0_BOTH_VENUE_FLOW_SCREEN`: only if full-market Bybit and Binance flow exist.
- `H1_RESIDUAL_SIZE`: size by residual flow only after two-venue coverage.
- `H2_DIVERGENCE_ADMISSION`: admit when venues disagree in a specified way.
- `H3_SQUEEZE_HEDGE`: use squeeze state for hedge intensity only.
- `H4_UNWIND_EXIT`: exit or reduce when flow unwind confirms.
- `H5_LIQUIDATION_CLUSTER_BLOCK`: block entry near adverse liquidation clusters.
- `H6_HASH_FLOW`: same distribution as selected flow rule.

Candidate rule:

- Binance-only flow remains exploratory.
- Two-venue claims require full-market Bybit flow and hash/delayed controls.

## Problem Book I: Portfolio Interaction And Sleeve Risk

Purpose: test whether the continuous book fails because of portfolio-level
interactions rather than single-trade alpha.

Arms:

- `I0_CONTINUOUS_ONLY`: continuous sleeve alone.
- `I1_LONG_CONTINUOUS_RESERVATION`: reserve capital across long and continuous.
- `I2_BETA_CLUSTER_CAP`: cap same-beta cluster exposure.
- `I3_HEDGE_FIRST_DRAWDOWN`: respond to drawdown via hedge before name exits.
- `I4_COMPONENT_CONCENTRATION_CAP`: cap p3/p4p3/p4p5 concentration.
- `I5_HASH_CLUSTER_CAP`: null with same cap frequency.

Candidate rule:

- Must improve portfolio risk without hiding alpha decay or starving the sleeve.

## Run Sequencing

Wave 0: plan and baseline receipts.

- Create this plan.
- Freeze both controls.
- No new A/B research cells.

Wave 1: data foundation.

- Build Bybit full 1m root.
- Build Binance full 1m root.
- Produce data quality ledger.

Wave 2: execution engine.

- Add `intrabar_resolution`.
- Add order/fill ledger.
- Calibrate cost model from demo/paper fills.

Wave 3: feature almanac.

- Build feature almanac V3.
- Run delayed-copy and hash-null diagnostics.

Wave 4: first A/B wave.

- Select at most one stops/TPSL arm family.
- Select at most one TWAP arm family.
- Select at most one regime/vol-control arm family.
- Freeze all configs before execution.

Wave 5: no-order forward shadow.

- Only after a candidate survives Wave 4.
- No live order changes without a separate operator receipt.

## Initial Implementation Checklist

First engineering batch:

- Add Bybit 1m build stage to `scripts/build_full_pit_bybit.sh`.
- Extend Binance Vision ingestion to `--interval 1m`.
- Add `scripts/audit_full_1m_roots.py`.
- Add tests for Binance interval handling and checksum failure.
- Add tests for Bybit dense-day checks and V5 gap-fill behavior.
- Add `intrabar_resolution` fixtures to continuous v2 tests.
- Add order/fill ledger schema tests.
- Add the construction receipt for the first executable phase.

Do not start alpha arms in the same batch. Data and engine parity come first.

## Required Future Receipts

Before execution, create separate receipts for:

- `continuous-v2-1m-data-foundation-construction`
- `continuous-v2-intrabar-execution-engine-construction`
- `continuous-v2-feature-almanac-v3-construction`
- `continuous-v2-stops-tpsl-ab-construction`
- `continuous-v2-twap-execution-ab-construction`
- `continuous-v2-regime-volcontrol-ab-construction`

Each receipt must include:

- objective,
- touched code paths,
- data roots,
- config arms,
- control arms,
- success metrics,
- null controls,
- stop conditions,
- exact commands to run,
- expected artifact paths.

## Stop Conditions

Stop the program and write a verdict if any of these happen:

- 1m roots fail PIT, lifecycle, or checksum integrity.
- `intrabar_resolution=1h` cannot reproduce the accepted 1h control.
- Demo/paper fill calibration implies the strategy has too little capacity.
- First A/B wave produces no candidate and hash controls explain apparent wins.
- Forward demo/paper drift cannot be reconciled after config-hash or data-root
  changes.

## Initial Recommendation

Start with data and execution realism, not new alpha. The next useful work is:

1. freeze the two controls,
2. build and audit full 1m PIT roots,
3. add a path-aware execution engine and fill ledger,
4. calibrate costs from demo/paper,
5. only then run the first small A/B wave.

Until those steps are complete, any new alpha result is too likely to be an
artifact of path ambiguity, cost optimism, or data availability leakage.
