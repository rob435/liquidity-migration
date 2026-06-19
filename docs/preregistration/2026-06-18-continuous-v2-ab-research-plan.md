# Draft Pre-Registration: Continuous V2 Deep A/B Research Plan

**Date:** 2026-06-18
**Stage:** draft plan, not approved to run
**System:** `continuous_ensemble_v2`
**Authoring HEAD:** `e8c8080f8efa0f8fc4169bc48a13436388996d3b`
**Scope:** CONTINUOUS demo/paper research only. No real-money claim.

## Executive Summary

This plan is intentionally deeper than a simple list of switch tests. The next
research pass should not be "turn BTC regime off and see what happens." That is
too shallow and repeats the failure mode that got us here: small signal probes,
feature toggles, and one-off diagnostics that did not map cleanly into the live
trade lifecycle.

The control is the frozen v2-forward baseline:

- Profile: `continuous_ensemble_v2`
- Demo strategy id: `continuous_fade_v2`
- Paper strategy id: `continuous_fade_v2_paper`
- Forward baseline start: `2026-06-18T19:54:00+00:00`
- Baseline receipt:
  `docs/preregistration/2026-06-18-continuous-v2-forward-baseline.md`

Every candidate-track experiment changes exactly one mechanism versus that
control and must produce both-venue, full-PIT, costed, reconstructable ledgers.
A screen can rank ideas, but only a full trade-lifecycle A/B can accept or
reject a candidate alpha mechanism.

Operator amendment, 2026-06-19: Problem Book C flow/microstructure work may run
on Binance only because Bybit full-market taker-flow is not available. Those
C-book flow runs are `exploratory` / `single_venue_investigation` only. They can
rank mechanisms and decide whether a Bybit archive build is worth doing; they
cannot satisfy the two-venue candidate bar or support demo/paper wiring.

The research program is organized as problem books:

1. Regime scoring and BTC state response.
2. Composite-score construction and score confidence.
3. Order flow and microstructure, including residualized flow and nonlinear
   flow forecasts.
4. Squeeze, crowding, funding, and OI.
5. Execution and cost alpha.
6. Exit timing and risk-control redesign.
7. Component and venue interaction checks.

The first runnable deliverable is not a backtest. It is a feature almanac and
research harness that proves every candidate input is causal, PIT-available,
covered for its claimed venue scope, and mapped to a live-compatible
intervention. The claimed scope must be explicit: both-venue candidate-track or
Binance-only exploratory flow.

## Important Honesty

The previous draft was too basic. It would have let an agent run a handful of
easy BTC-gate variants, call the plan complete, and miss the more interesting
old research threads: regime weighting, score construction, path-shape,
order-flow, OI/funding crowding, depth, liquidation, and execution.

But the opposite error is also dangerous. "Deep research" cannot mean unbounded
mining. The old W5/W6 work already taught a painful lesson: several signals had
real IC, but the harvest failed because the intervention concentrated the same
correlated squeeze tail. A real score is not automatically a tradable edge.

Therefore:

- Screens are discovery only.
- A/B tests are mechanism evidence.
- Forward demo/paper is the arbiter.
- Nothing here approves real money.
- Old research is a hypothesis map, not binding evidence for v2.
- A Binance-only flow result can be useful, but it is not a candidate, not a
  Bybit demo/paper input, and not a substitute for cross-venue agreement.

## Current Control

The control arm is the currently wired v2 demo/paper system:

- Components:
  - `p3`: `turn3_pop3`, age 240d, TP 10%, weight 0.3333333333333333
  - `p4p3`: `turn4_pop3`, age 240d, TP 10%, weight 0.2222222222222222
  - `p4p5`: `turn4_pop5`, age 240d, TP 10%, weight 0.4444444444444444
- Signal: D9 fade, rmom q25, `feature_set=("max_ret168",)`,
  `liq_turnover_min=500000`.
- BTC trend gate: `uptrend`, meaning prior-30d BTC return must be positive.
- Entry confirmation: confirmed-bar path with +1h confirm delay.
- Max active shorts: 25.
- Max new entries per cycle: 5.
- Entry sizing: inverse-vol, `target_vol_per_name=0.01`, clamp `[0.5, 2.0]`.
- Daily rebalance: enabled, 90d realized vol window, target daily vol 0.045,
  max scale 4.0, drawdown half threshold -0.04, no strategy momentum hurdle.
- Exits: 10% component venue TP and 24h max hold.
- Disabled exits/risk: no `left_decile`, no `stop_approach`,
  no `failed_fade`, no `breakeven`, no re-entry cooldown, no server stop.
- Adverse-exit entry breaker: retained.
- Hedge: BTC+ETH 2f hedge plus BTC-vol regime overlay.
- Sniper: demo-forward execution add-on only; not part of the frozen backtest
  control unless explicitly tested in a separate forward/shadow protocol.

## Non-Negotiable Integrity Gates

Every run in this plan must satisfy the repo methodology gate:

- Declare `decision_ts`, `data_available_ts`, `order_submit_ts`,
  `fill_window`, `exit_activation_ts`, and `state_initialization_ts`.
- Use full PIT Bybit and Binance roots, including delisted and migrated names.
- Use causal features only. BTC 30d trend, BTC-vol intensity, order-flow,
  funding, OI, depth, and liquidation features must exclude unavailable bars.
- Include fees, funding/carry where available, spread/slippage assumptions,
  and capacity stress.
- Preserve live-equivalent state for exits, cooldowns, breakers, max-active,
  max-new, rebalance state, hedge state, and any adaptive feature.
- Produce trade ledgers, equity curves, split metrics, drawdown, worst day,
  run label, config hash, data-root identity, and report artifacts.
- Run both venues for every both-venue claim. A one-venue win is not a
  candidate.
- The amended C-book flow branch may run Binance only. Every such report must
  declare `claimed_venue_scope=binance_only_flow_exploratory` and use the
  `exploratory` run label.
- Do not lower thresholds after seeing results.

Run labels:

- `exploratory`: the default label for screens and incomplete lifecycle tests.
- `candidate`: only if PIT, costs, splits, ledgers, robustness, and both venues
  pass.
- `paper_ready`: only if candidate plus a forward/demo plan matching the tested
  lifecycle.

No internal run can be real-money evidence.
No Binance-only flow run can be candidate or paper-ready evidence by itself.

## Research Objective

Primary objective:

Find single-mechanism changes that improve v2 risk-adjusted performance without
breaking cross-venue agreement or live reconstructability. For the amended
Binance-only C-book flow branch, the objective is narrower: learn whether the
flow mechanism is worth further investment, not whether it is acceptable for
demo/paper.

Primary metric:

- Delta MAR versus control, per claimed venue scope. Pooled MAR applies only to
  two-venue candidate-track arms.

Secondary metrics:

- Total return.
- Max drawdown.
- Sharpe-like.
- Worst day return.
- Worst month and monthly hit rate.
- Trade count and per-component retention.
- Exit-reason return attribution.
- Funding/cost contribution.
- Hedge turnover and hedge PnL.
- Capacity and participation stress.
- Demo/paper forward compatibility.

Hard rejects:

- Any candidate-track arm negative on either venue.
- Any Binance-only flow arm negative on Binance.
- Any arm improves headline return by accepting materially worse drawdown
  without a documented operator decision that return is the objective.
- Any arm relies on non-PIT membership, future data, current-day BTC return, or
  artifact-only helper code that cannot be reproduced from repo source.
- Any arm only works because it removes realistic costs, funding, capacity, or
  fill assumptions.
- Any arm materially reduces trade count without explaining whether it is true
  selectivity or overfitting.
- Any screen-only result called an alpha.
- Any Binance-only flow result presented as a two-venue candidate, Bybit
  deployment input, or promotion result.

Candidate promotion bar:

- Both venues positive.
- Both venues improve MAR versus v2 control, or one venue ties within tolerance
  while the other improves materially and pooled robustness passes.
- No venue worsens max drawdown materially.
- Monthly and sub-period results do not reveal a single-regime artifact.
- Bootstrap / leave-one-month-out / sub-period thirds from
  `scripts/r1_robustness.py` do not flip the Tier-2 decision negative.
- The report names the falsifier and whether it fired.
- Binance-only flow arms are excluded from this candidate bar regardless of
  headline metrics.

## Prior Research Map

This section is an archived-mechanism map. It is not a claim that old results
transfer to v2. The old controls, components, take-profit settings, and data
windows differ. Use the map to choose hypotheses; use the v2 A/B harness to
judge them.

### W5 Continuous Signal-Alpha Program

Archived report:
`git show 96351a7:docs/research_plans/w5_continuous_signal_alpha/PROGRAM_REPORT.md`

Key lessons:

- The book's edge was diffuse. It profited when broadly deployed in
  dislocations.
- Most selection, shrinkage, and early-exit ideas failed because they removed
  diffuse winners or concentrated drawdown.
- The one robust historical harvest was a BTC-vol hedge-intensity overlay: it
  kept the whole book and hedged the squeeze tail.
- Score-entry priority was structurally weak because there was little
  same-cycle contention to re-order.
- Path-shape had real within-symbol residual IC, but admissibility was not
  enough; downstream sizing did not cleanly harvest.
- Regime book sizing down in high BTC-vol regimes failed; the book made money
  in those regimes and needed tail hedging rather than shrinkage.
- The sniper looked strong in a single setting, then failed robustness checks.
  Standout results require parameter and null-control stress before banking.
- Dispersion and book-drawdown hedge signals were venue-split. Venue-specific
  findings can be useful, but they are not both-venue alpha.

### W6 Bybit-First Orderflow And Squeeze Program

Archived plan:
`git show d40f6e0:docs/research_plans/w6_bybit_program/PLAN.md`

Key lessons:

- OI/funding squeeze features produced real screens:
  - `oi_chg_24h` had a positive Bybit within-symbol partial rank-IC.
  - `funding_level` had a positive Binance screen and same-sign Bybit context.
- Squeeze sizing, squeeze hedge-intensity, gross-scaler, and crowding admission
  did not produce a robust harvest in the registered runs.
- Crowding admission added profitable rejected fades in isolation, but lowered
  MAR because the marginal exposure was tail-correlated.
- Raw taker-flow composition was mostly null in the old scout. The next test
  should not simply replay raw taker imbalance; it should residualize flow
  against lagged returns and separate market-wide from idiosyncratic flow.
- Execution/cost alpha remained under-explored and was data-gated by missing
  sub-hourly and depth coverage.

### Current Hot-Path Summary

Current docs deliberately removed old staged receipts from the source of truth.
That was right for deployment clarity, but it means this plan must explicitly
mine git history and local artifacts for hypotheses before running a new sweep.

Relevant current files:

- `STATE.md`
- `docs/research_summary.md`
- `docs/promoted_trading_logic.md`
- `docs/backtesting_errors_we_never_repeat.md`

Relevant local artifact examples:

- `/Users/jhbvdnsbkvnsd/SHARED_DATA/orderflow_squeeze_proxy_screen_2026-06-15/`
- `/Users/jhbvdnsbkvnsd/SHARED_DATA/continuous_taker_flow_scout_2026-06-12/`
- `/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_full_pit/taker_flow_5m/`
- `/Users/jhbvdnsbkvnsd/SHARED_DATA/binance_full_pit/binance_usdm_taker_flow_1h/`

These artifacts can inform hypotheses and coverage audits. They cannot be cited
as current v2 promotion evidence unless the new v2 harness reproduces the
mechanism under current rules.

## External Order-Flow Prior

The order-flow paper supplied by the operator is useful, but it must be
translated carefully.

Reference:

- SSRN abstract: `https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5020002`
- University of Guelph thesis/PDF source:
  `https://atrium.lib.uoguelph.ca/bitstreams/bae607b2-3fff-401a-8412-c34569fd5f98/download`

What the paper does:

- Studies a broad crypto cross-section.
- Uses international order flows denominated in 11 fiat currencies.
- Aggregates them into a "world order flow" measure.
- Tests lagged order flow after controlling for lagged returns, separating
  reversal from persistent flow information.
- Finds stronger predictive power from nonlinear ML models conditioning on the
  full order-flow panel.

What we can and cannot copy:

- We do not have their 11-fiat "world order flow" dataset.
- We do have venue-native proxies: taker imbalance, OI, funding, premium,
  long/short ratio where available, liquidation tapes, and depth snapshots.
- Therefore the right translation is not "use their signal." It is:
  residualize lagged venue order-flow proxies against lagged returns, separate
  market-wide flow from idiosyncratic symbol flow, and only then test whether a
  nonlinear flow composite improves the v2 lifecycle.
- Their reported Sharpe is not our prior. Our prior must be cut heavily for
  data mismatch, perp-vs-spot differences, small-cap liquidity, borrow/shorting
  reality, and our specific short-fade lifecycle.

Admissible order-flow hypotheses:

- Persistent flow residual predicts the next fade return after removing
  short-term reversal.
- Market-wide flow state changes how much hedge the book needs.
- OI/funding/taker-flow divergence identifies squeeze-risk tails better than
  price-only BTC regime.
- Nonlinear combinations can help, but only after linear residualized features
  show causal coverage and economic sign.

## Research Standard

Use a prop-desk standard for each problem book:

1. State the economic mechanism in one paragraph.
2. Build a causal feature tape with data availability timestamps.
3. Prove coverage by venue, symbol, year, and component.
4. Run negative controls before any expensive sweep.
5. Use screens only to rank hypotheses.
6. Run the full v2 lifecycle A/B for any serious claim.
7. Include cost, funding, hedge, rebalance, drawdown, and capacity effects.
8. Diagnose failures to root cause before moving to the next related arm.
9. Stop the exact mechanism when its falsifier fires.
10. Do not stack two unproven features.

Every problem book must leave a written receipt with:

- Why this mechanism could improve v2.
- What old research says for and against it.
- What data exists at decision time.
- Which A/B arms ran.
- Which falsifier fired.
- Why the next step is justified or why the line is closed.

## Required Harness Work Before Any Run

Do not run serious A/B cells through a scratch artifact import.

Create a checked-in dispatcher:

`scripts/continuous_v2_ab_research_runner.py`

Requirements:

- No imports from local artifact directories.
- Uses repo modules or checked-in helper functions only.
- Loads both full-PIT roots:
  - `~/SHARED_DATA/bybit_full_pit`
  - `~/SHARED_DATA/binance_full_pit`
- Emits one run directory:
  `backtest-runs/continuous_v2_ab_<YYYY-MM-DD>/`
- Emits one subdirectory per arm and venue.
- Saves normalized configs as JSON before running each arm.
- Saves config hash and git commit.
- Supports `--arms`, `--venues`, `--start-date`, `--end-date`,
  `--out-root`, `--resume`, and `--max-workers`.
- Writes:
  - `trades.csv`
  - `orders_or_fill_model.csv` when applicable
  - `mtm.csv`
  - `equity.csv`
  - `monthly.csv`
  - `splits.json`
  - `summary.json`
  - `run_report.md`
  - pooled `ab_table.csv`
  - pooled `decision_rule_input.csv`
- Checkpoints after each venue/arm pair.
- Refuses to run if the control arm is missing from the same run directory.

The first commit for this program should add the dispatcher and tests only.
The second step runs registered arms.

## Feature Almanac Deliverable

Before any A/B arm, build:

`backtest-runs/continuous_v2_feature_almanac_<YYYY-MM-DD>/`

The almanac is a data proof, not an alpha test.

Required outputs:

- `feature_inventory.csv`: feature name, source table, venue, coverage, earliest
  timestamp, latest timestamp, decision lag, known gaps.
- `feature_tape_<venue>.parquet`: one row per candidate entry opportunity,
  all candidate features, and all data availability timestamps.
- `coverage_by_symbol_year.csv`.
- `coverage_by_component.csv`.
- `latency_audit.csv`: feature value at decision time versus delayed copy.
- `negative_controls.csv`: symbol hash, calendar hash, shuffled-within-symbol,
  shuffled-within-day.
- `feature_corr.csv`: correlations among current composite, regime, OI,
  taker-flow, funding, premium, liquidity, and returns.
- `readme.md`: which features are admissible for full A/B and which are
  data-gated.

Feature families to inventory:

- Current composite and component scores.
- Score margin: D9 minus D8, D9 minus median, rank distance, feature agreement.
- BTC 30d return, BTC realized vol, BTC-vol percentile, BTC drawdown, BTC
  trend flip age.
- Cross-sectional breadth, dispersion, alt-minus-BTC return, market beta.
- Funding level, funding change, premium level/change, mark/basis, carry.
- OI level/change, OI/volume, OI acceleration.
- Taker imbalance 5m/1h/6h/24h, lagged and residualized.
- Market-wide taker imbalance and symbol residual from market flow.
- Long/short ratio where available.
- Liquidation cluster and book-thinning where tapes have matured.
- Liquidity/turnover, spread/depth, realized entry slippage proxies.
- Path-shape: pre-6h/24h return, realized vol, wick/retrace, distance from
  local high.

No feature can enter a serious A/B unless the almanac marks it as causal and
covered enough for the claimed venue scope.

For the amended C-book flow branch, the claimed venue scope is Binance only and
the admissible source is `binance_usdm_metrics_5m`. The current Bybit
event-scoped `taker_flow_5m` tape must not be used as full-market flow evidence.

## Methodology Timestamps

These must be stamped into every arm report:

- `decision_ts`: component signal bar close, after the signal input window is
  closed.
- `data_available_ts`: closed-bar feature availability plus causal residual
  momentum shift. Daily regime features use only prior closed daily bars.
- `order_submit_ts`: entry bar close after configured confirmation delay.
- `fill_window`: historical hourly bar model or finer execution model, with
  explicit cost and slippage assumptions.
- `exit_activation_ts`: venue TP, 24h max hold, and any experimental exit
  activation time.
- `state_initialization_ts`: run start plus enough warmup for listing age,
  rmom, BTC trend, volatility, rebalance, hedge, and feature state.

## Phase 0 - Control Reproduction And Transfer Audit

Do this before any experimental arm.

Arms:

- `V2_CONTROL`: exact current control.
- `V2_CONTROL_DELAYED_FEATURES`: same as control, but any uncertain external
  feature path uses a one-bar or one-day latency delay. This should match the
  control for features not used by v2; it is a harness sanity check.

Required checks:

- Reproduce the v2 control under both venues in the same run directory.
- Confirm entry counts, exit counts, component weights, inverse-vol entry
  sizing, max4 rebalance, hedge, BTC-vol overlay, and no live-exit stack.
- Confirm current source component ledgers are fixed-hold / TP / 24h only.
- Confirm no helper imports from old artifact directories.
- Record the exact differences between this v2 control and historical W5/W6
  controls so old results are not over-interpreted.

Pass bar:

- Control artifacts complete.
- No unexplained venue truncation.
- No PIT membership failures.
- No same-minute mass exits unless explained by code and market data.

## Problem Book A - Regime Scoring

Question:

Can a richer causal regime response beat the hard BTC-uptrend gate without
repeating the old overfit downtrend-extension mistake?

What we already know:

- Plain gate-off was historically a useful falsifier, not an automatic
  improvement.
- Old bounded trend-band variants failed after full lifecycle, funding, and
  exits.
- Sizing down in high BTC-vol regimes failed; the book often profits in high
  vol and needs tail protection, not shrinkage.
- The BTC-vol hedge overlay was the best historical regime harvest.

Research approach:

- Treat BTC 30d return as one feature, not the whole regime.
- Build a continuous regime score from causal inputs:
  - BTC 30d return sign and magnitude.
  - BTC realized-vol percentile.
  - BTC drawdown depth and trend flip age.
  - Cross-sectional breadth and dispersion.
  - Aggregate funding/carry stress.
  - Aggregate OI/taker-flow squeeze pressure only for Binance-only exploratory
    variants; two-venue A-book arms must omit flow until Bybit full-market flow
    exists.
  - Book equity drawdown state, lagged and causal.
- Map that score into one intervention at a time.

Arms:

- `A0_BTC_GATE_OFF_NEG_CONTROL`: remove the BTC-uptrend gate, all else equal.
  This is a falsifier and benchmark, not a preferred candidate.
- `A1_BTC_DOWN_ONLY_NEG_CONTROL`: trade only when prior-30d BTC return is
  negative. This should fail unless the old regime story is wrong under v2.
- `A2_SOFT_BTC_SCORE_ENTRY_SCALE`: replace hard gate with a locked monotone
  size map based only on prior-30d BTC return. Mean-1 within admissible days;
  no other feature.
- `A3_MULTIFACTOR_REGIME_ENTRY_SCALE`: same intervention as A2 but the score is
  the predeclared multifactor regime score.
- `A4_REGIME_HEDGE_INTENSITY`: keep entries unchanged; map the multifactor
  regime score into hedge intensity only.
- `A5_REGIME_COMPONENT_ADMISSION`: keep the global gate but allow
  component-specific regime admission if a component is historically robust in
  that regime. One component rule per arm; no bundled component mining.
- `A6_REGIME_HASH_CONTROL`: same score distribution as A3/A4 but calendar or
  symbol-hash regime labels.

Preferred first serious arm:

- `A4_REGIME_HEDGE_INTENSITY`, because historical research says overlaying tail
  protection while keeping breadth is the most plausible harvest mode.

Falsifiers:

- The regime score is matched by the hash control.
- The gain comes from trading more in regimes where full lifecycle PnL is worse.
- The score improves return but worsens drawdown enough to lower MAR.
- A2/A3 just re-create a bounded gate-off/cap variant.
- One venue carries the entire result.

## Problem Book B - Composite Scores And Score Confidence

Question:

Is the current composite too crude, and can a better score improve entry,
sizing, hedge, or exit timing without shrinking the diffuse book?

What we already know:

- Score-entry priority was historically a structural no-op when there was
  little same-cycle contention.
- Path-shape features had real within-symbol residual IC.
- Sizing harvests can fail even when the screen is real.
- A ridge combiner was previously weak in an old context; do not assume a
  black-box score will save the book.

Research approach:

- Separate score quality from intervention quality.
- Test score improvements first as screens with within-symbol and
  same-cycle-controls.
- Only then map one score to one intervention.

Candidate score families:

- `current_composite`: current D9 fade score.
- `weighted_linear_composite`: fixed weights from train-fold ICs, frozen before
  validation.
- `score_margin`: D9 score minus next-best candidate or D8 threshold.
- `feature_agreement`: fraction of features that agree with the fade thesis.
- `score_entropy`: concentrated versus diffuse feature support.
- `residualized_composite`: composite residualized against BTC beta, funding,
  volatility, liquidity, and market flow. Market-flow residualization is
  Binance-only exploratory until Bybit full-market flow exists.
- `path_shape_augmented`: current composite plus pre-6h/24h return and pre-24h
  realized-vol residuals.
- `monotone_nonlinear_score`: constrained nonlinear model, only after linear
  and residual screens pass.

Arms:

- `B0_SCORE_SCREEN_ONLY`: feature almanac plus train/validation screen. No
  trading decision.
- `B1_SCORE_MARGIN_SIZING`: same entries as control, size multiplier from score
  margin, mean-1, clamped, gross-neutral.
- `B2_FEATURE_AGREEMENT_SIZING`: same entries, size multiplier from feature
  agreement only.
- `B3_RESIDUALIZED_COMPOSITE_PRIORITY`: priority only when the same signal hour
  has more candidates than allowed. Entry count must remain comparable.
- `B4_PATH_SHAPE_EXIT_TIMING_SHADOW`: no-order shadow exit timing based on
  path-shape score; does not submit live orders.
- `B5_MONOTONE_SCORE_SIZING`: nonlinear score drives same-entry sizing; allowed
  only if B0 shows stable train/validation sign and B1/B2 establish a harvest
  mode.
- `B6_SCORE_HASH_CONTROL`: same distribution, shuffled within symbol/month.

Falsifiers:

- Score IC is symbol-mix rather than within-symbol.
- Same-cycle contention is too low for priority to matter.
- Sizing raises drawdown faster than return.
- The hash/shuffle control matches the score.
- A nonlinear model works only in one sub-period or one venue.

## Problem Book C - Order Flow And Microstructure

Question:

Can order-flow information add persistent return prediction or tail-risk
prediction after removing short-term reversal and current composite effects?

What we already know:

- Raw taker-flow composition was mostly null in the old scout.
- OI/funding squeeze proxies had real screens but did not harvest through simple
  sizing/admission.
- The external order-flow paper argues that residualized lagged world order
  flow and nonlinear order-flow panels can predict crypto cross-sections.
- Our data is not their data. We need venue-native proxies and a harsher
  translation.

Venue scope amendment, 2026-06-19:

- Run C-book flow work on Binance only using
  `~/SHARED_DATA/binance_full_pit/binance_usdm_metrics_5m`.
- Do not wait for Bybit full-market taker-flow before running the amended
  Binance flow screens or Binance flow overlay diagnostics.
- Do not use the current Bybit event-scoped `taker_flow_5m` tape to fill the
  Bybit gap.
- Every C-book flow run must declare
  `claimed_venue_scope=binance_only_flow_exploratory`.
- A positive result can justify a Bybit archive backfill or a later
  venue-policy amendment. It cannot justify demo/paper wiring by itself.

Feature construction:

- `flow_imb_1h/6h/24h`: taker buy minus sell pressure, normalized by total
  taker volume.
- `flow_resid_return`: flow residual after controlling for lagged returns at
  the same horizon.
- `flow_resid_composite`: flow residual after controlling for current composite
  and path-shape.
- `market_flow`: turnover-weighted cross-sectional flow.
- `idiosyncratic_flow`: symbol flow minus market flow exposure.
- `flow_persistence`: same-sign flow across consecutive horizons.
- `flow_divergence`: price pump with weak or negative taker support, or price
  pump with rising OI but fading taker buy pressure.
- `flow_squeeze`: OI buildup plus positive funding plus aggressive taker buy
  imbalance.
- `flow_unwind`: OI falling, funding normalizing, taker imbalance reversing.

Screens:

- Within-symbol partial rank-IC over current composite.
- Orthogonalize lagged flow against lagged returns before prediction.
- Separate daily and weekly horizons if data supports both.
- Symbol hash, calendar hash, within-symbol permutation, and flow-shuffle nulls.
- Coverage by Binance symbol/year/component using metrics archives. Binance REST
  recent-window fields must not be presented as full-history evidence.
- Bybit flow coverage is reported as gated/unavailable for this branch.

Arms:

- `C0_ORDERFLOW_SCREEN_BINANCE_ONLY`: residualized flow screen only.
- `C1_FLOW_RESID_FEATURE_SIZING_BINANCE_ONLY`: same entries as control; mean-1
  size tilt from residualized idiosyncratic flow. This is the closest
  translation of the paper's residualized lagged order-flow idea.
- `C2_MARKET_FLOW_HEDGE_INTENSITY_BINANCE_ONLY`: keep entries unchanged; scale
  hedge intensity when market-wide flow indicates squeeze risk.
- `C3_FLOW_SQUEEZE_HEDGE_INTENSITY_BINANCE_ONLY`: keep entries unchanged; hedge
  more when the active book has high aggregate squeeze score.
- `C4_FLOW_DIVERGENCE_ADMISSION_BINANCE_ONLY`: admit or prioritize only
  candidates with a predeclared divergence pattern. This is lower prior because
  old admission concentrated tails; run only after C0/C2/C3.
- `C5_FLOW_UNWIND_EXIT_SHADOW_BINANCE_ONLY`: no-order exit timing shadow based
  on structural squeeze completion, not price stop.
- `C6_NONLINEAR_FLOW_SCORE_BINANCE_ONLY`: monotone or tree-based score on
  residualized flow, OI, funding, and premium features. Allowed only after C0
  establishes stable linear signal and the training protocol is purged and
  time-split.
- `C7_FLOW_HASH_CONTROL_BINANCE_ONLY`: same intervention using shuffled or hash
  flow.

Preferred first amended flow arm:

- `C2_MARKET_FLOW_HEDGE_INTENSITY_BINANCE_ONLY` or
  `C3_FLOW_SQUEEZE_HEDGE_INTENSITY_BINANCE_ONLY`, not sizing. Historical
  evidence says tail-protection overlays are more likely to harvest than adding
  exposure. The label remains exploratory.

Falsifiers:

- Raw flow is not incremental after lagged returns and composite.
- Flow-shuffle, symbol-hash, or calendar-hash controls match the feature.
- The result is not stable across time splits, liquidity buckets, or component
  subsets.
- The nonlinear score beats linear only by overfitting one period.
- The intervention raises drawdown faster than return.
- The result is described as a two-venue candidate or Bybit deployment input.

## Problem Book D - Squeeze, Crowding, Funding, And OI

Question:

Can the old OI/funding/crowding signal be harvested through a better
intervention than sizing/admission?

What we already know:

- `oi_chg_24h` had a real Bybit screen in the archived squeeze proxy.
- `funding_level` had a Binance screen and same-sign Bybit context.
- Sizing and admission raised or preserved return but worsened drawdown/MAR.
- The crowding cap rejected trades that were profitable in isolation, but they
  were lower-quality tail-correlated exposure.

Arms:

- `D0_SQUEEZE_FEATURE_REFRESH`: re-run the squeeze screen under v2 with current
  component set and current feature almanac.
- `D1_OI_FUNDING_HEDGE_INTENSITY`: keep entries unchanged; hedge more on days
  with high active-book squeeze score.
- `D2_OI_FUNDING_EXIT_SHADOW`: no-order structural completion exit when OI and
  funding normalize.
- `D3_CROWDING_CAP_SENSITIVITY`: one-knob sensitivity around current crowding
  behavior, with constant-leverage control. Do not treat added trades as a win
  unless MAR improves.
- `D4_FUNDING_CARRY_ATTRIBUTION`: decompose trade return into price fade,
  funding/carry, and hedge. This is diagnostic; it decides whether funding is a
  signal, cost, or tail-warning input.
- `D5_SQUEEZE_HASH_CONTROL`: random squeeze score with same distribution.

Falsifiers:

- Squeeze score is real but every harvest mode worsens MAR.
- The score only predicts isolated trade return, not portfolio drawdown.
- Funding coverage gaps make the venue comparison invalid.
- The apparent edge disappears under cost/funding stress.

## Problem Book E - Execution And Cost Alpha

Question:

Can we improve net MAR without changing the signal by reducing execution cost,
slippage, or adverse selection?

Why this is high priority:

This axis does not fight the diffuse signal. It can improve returns and
drawdown without deciding which names to drop. It is also essential for any
future real-money path.

Data prerequisites:

- Sub-hourly price/tick coverage.
- Bybit taker-flow 5m coverage.
- Depth snapshots with enough forward history.
- Demo fill logs for realized slippage and fill probability.

Arms:

- `E0_EXECUTION_DATA_AUDIT`: coverage, timestamp, and fill-model audit only.
- `E1_INTRABAR_ENTRY_TIMING`: same signal and entry hour, but wait for a
  predeclared intrabar exhaustion pattern. Measure entry-price improvement net
  of missed fills and adverse selection.
- `E2_POST_ONLY_SNIPER_SHADOW`: shadow-only maker ladder; no claim until fill
  probability and adverse selection are measured from forward fills.
- `E3_LIQUIDITY_AWARE_CLIP_SIZE`: same entries; clip size responds to depth and
  spread, not alpha score.
- `E4_CAPACITY_IMPACT_CURVE`: estimate notional capacity per name and per day;
  required for scaling, not an alpha arm.
- `E5_EXECUTION_HASH_CONTROL`: randomized timing within the same hour.

Falsifiers:

- Better quoted entry price comes from missed losing fills or unfillable quotes.
- Maker savings are eaten by adverse continuation.
- Depth coverage is too thin or forward-only.
- The fill model cannot be reconciled to demo fills.

## Problem Book F - Exit Timing And Risk Controls

Question:

Can exits protect v2 without repeating the live-exit collapse?

What we already know:

- The old daemon live exits destroyed the edge. `stop_approach` was a cliff, and
  `left_decile` was a large drag.
- The current v2 intentionally uses TP/24h only and no server stop. That is not
  real-money-safe, but it is the current demo/paper research object.
- Any future mainnet path needs a different risk-control design. It cannot
  revive the old stop stack.

Arms:

- `F0_EXIT_ATTRIBUTION_REFRESH`: v2 exit-cause attribution under current
  control.
- `F1_FLOW_UNWIND_EXIT_SHADOW`: no-order shadow exit when OI/flow/funding show
  structural squeeze completion. Before a Bybit full-market flow build, any
  flow-driven version is Binance-only exploratory.
- `F2_TIME_DECAY_EXIT_SHADOW`: no-order shadow of hold-time-dependent exit
  after the historical MFE window, not before.
- `F3_HEDGE_FIRST_DRAWDOWN_CONTROL`: risk control acts through hedge intensity
  before cutting positions.
- `F4_PORTFOLIO_KILL_SHADOW`: shadow-only portfolio circuit breaker with
  warm-started state. It cannot submit orders until forward evidence exists.
- `F5_EXIT_HASH_CONTROL`: random exit activation with matched frequency.

Falsifiers:

- Exit cuts TP winners.
- Exit improves one venue by overfitting a month.
- Exit relies on state a live executor would not have.
- Exit turns the strategy into a different book with materially lower trade
  count.

## Problem Book G - Components, Venues, And Sleeve Interaction

Question:

Do component and venue interactions explain where v2 wins or loses?

This is not a permission slip to re-mine component weights. It is a diagnostic
book to understand whether an alpha mechanism is universal, component-specific,
or venue-specific.

Arms and diagnostics:

- `G0_COMPONENT_ATTRIBUTION`: per-component returns, DD, funding, hedge PnL,
  score distribution, and exit cause.
- `G1_COMPONENT_SPECIFIC_FEATURE_RESPONSE`: one feature, one component, one
  intervention. Requires that the feature has a clear component-specific
  mechanism.
- `G2_VENUE_SPECIFIC_HEDGE_DIAGNOSTIC`: compare Bybit and Binance response to
  BTC-vol, book drawdown, and dispersion. Flow squeeze is Binance-only
  diagnostic until Bybit full-market flow exists. Diagnostic only unless a
  per-venue deployment policy is explicitly registered.
- `G3_LONG_CONTINUOUS_REGIME_OVERLAP`: diagnostic only. The old dynamic tilt
  failed; do not re-open allocator work until per-sleeve alphas are clean.
- `G4_COMPONENT_HASH_CONTROL`: random component label or random venue split.

Falsifiers:

- Component response is really a symbol/universe mix artifact.
- Venue-specific result cannot be reconciled to venue data coverage, funding,
  or microstructure.
- Sleeve interaction is just the same BTC regime exposure expressed twice.

## Run Sequencing

Do not run every arm at once. The correct sequence is:

1. Build the checked-in v2 A/B harness.
2. Build the feature almanac.
3. Reproduce `V2_CONTROL` in the same run directory.
4. Run cheap screens for Regime, Composite, Squeeze, and Binance-only Orderflow
   books.
5. Pick at most two candidate-track serious A/B arms from different mechanisms:
   - one overlay arm, preferably regime hedge intensity;
   - one non-overlay arm, preferably score-margin sizing or execution timing.
   A Binance-only flow overlay can run alongside these only as exploratory.
6. Run both venues, full lifecycle, with negative controls for candidate-track
   arms. Run amended C-book flow arms on Binance only and label them
   exploratory.
7. Apply `scripts/r1_robustness.py` to candidate-track arms. For Binance-only
   flow arms, report the same diagnostics where possible but force the verdict
   to exploratory / no Tier-2 candidate pass.
8. Write a dated verdict receipt.
9. Only then decide the next arm.

Recommended first wave:

- `A4_REGIME_HEDGE_INTENSITY`
- `C2_MARKET_FLOW_HEDGE_INTENSITY_BINANCE_ONLY` or
  `C3_FLOW_SQUEEZE_HEDGE_INTENSITY_BINANCE_ONLY` as exploratory flow work
- `B1_SCORE_MARGIN_SIZING`
- `E1_INTRABAR_ENTRY_TIMING` if sub-hourly data coverage passes

Do not run first:

- Plain BTC gate-off as the main event.
- Old live stop stack.
- Dense nonlinear ML without the residualized linear screen.
- Any stacked feature bundle.
- Any order-flow arm that ignores lagged-return residualization.
- Any admission/sizing-up arm without constant-leverage and drawdown controls.

## Multiple Testing Budget

Each problem book gets a limited number of serious A/B arms before a stop or
amendment:

- Regime scoring: 4 serious arms after screens.
- Composite scoring: 4 serious arms after screens.
- Order flow: 5 Binance-only exploratory arms under the 2026-06-19 amendment;
  nonlinear ML counts as 2. No C-book flow arm can enter the candidate budget
  until a later amendment restores two-venue or venue-policy evidence.
- Squeeze/crowding/funding: 4 serious arms.
- Execution/cost: 4 serious arms, because it is not alpha-mining in the same
  way but still needs controls.
- Exit/risk: 4 shadow arms before any order-capable proposal.
- Component/venue interaction: diagnostics only unless explicitly amended.

An arm that fails its falsifier closes that exact mechanism. A later stage needs
a dated amendment explaining the new data, new mechanism, or corrected
lifecycle.

## Decision Receipts

Every completed arm writes a verdict under `docs/preregistration/` with:

- Control and arm config hashes.
- Data-root identities.
- Causal timestamp declarations.
- Full metrics for the claimed venue scope. Candidate-track receipts still need
  full per-venue metrics.
- Pooled MAR delta for candidate-track arms; Binance MAR delta for Binance-only
  flow arms.
- Robustness output from `scripts/r1_robustness.py` where applicable; Binance-only
  flow receipts must state that no Tier-2 candidate pass is possible.
- Negative-control comparison.
- Cost/funding/capacity stress.
- Failure root cause.
- Whether the arm is closed, needs a new stage, or nominates demo/paper shadow.

Also update:

- `docs/research_summary.md` with only the final decision surface.
- `STATE.md` only if current state or operator decisions change.
- `docs/promoted_trading_logic.md` only if demo/paper wiring changes.

Do not promote, deploy, or cite an exploratory run as alpha.

## Plan Stop Conditions

Stop the current line and write the negative receipt if any of these happen:

- PIT membership or feature causality fails.
- Data coverage cannot support the claimed venue scope.
- Negative controls match the feature.
- Costs/funding erase the result.
- Drawdown worsens faster than return improves.
- For candidate-track arms, one month, one venue, or one component carries the
  result.
- For Binance-only flow arms, one month, one liquidity bucket, or one component
  carries the result.
- The mechanism cannot be expressed in live/paper code.
- The result depends on old helper scripts or artifact directories.

## Initial Operator Recommendation

Do not spend the first run on `BTC_TREND_GATE=off`. Include it as a negative
control, but it is not the interesting question.

The highest-value first pass is:

1. Build the v2 harness and feature almanac.
2. Run the regime-score screens and Binance-only order-flow screens with
   residualization and proper nulls.
3. Test one overlay that keeps the whole book:
   `A4_REGIME_HEDGE_INTENSITY` or
   `C3_FLOW_SQUEEZE_HEDGE_INTENSITY_BINANCE_ONLY` as exploratory flow work.
4. Test one non-overlay that does not drop trades:
   `B1_SCORE_MARGIN_SIZING` or `E1_INTRABAR_ENTRY_TIMING`.

That gives us breadth: one tail-protection idea, one score/economic idea, and a
process that can dig deeply without becoming unregistered parameter mining.
