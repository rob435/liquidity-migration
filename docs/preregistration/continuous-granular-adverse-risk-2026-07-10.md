# CONTINUOUS Granular Adverse-Risk Study — Pre-registration

Date registered: 2026-07-10
Status: registered, not run
Permitted run label before all gates pass: `exploratory`
Target machine: larger research PC
Deployment effect: none; this receipt cannot change demo, paper, or mainnet

## Question and falsifier

The hypothesis is narrow: a causally observed, cross-sectionally extreme 5-minute
adverse-continuation pulse identifies a small subset of active CONTINUOUS shorts
whose 24-hour hold has worse tail economics. Reducing entry size or exiting on the
next executable 5-minute bar should improve pooled tail risk without destroying
two-venue return.

The hypothesis is false if the primary exit cell fails either venue, fails the
frequency-matched null, materially reduces return, or only works when timestamps,
latency, costs, PIT membership, missing rows, or one-venue auxiliary data are
treated optimistically. The recent 1000TAGUSDT loss motivates the question but
does not define a winning threshold and is not an acceptance window.

This is not a broad feature search. The matrix, thresholds, timing, null, metrics,
and decision rule below are frozen before reading treatment PnL.

## Frozen object and data identity

- Strategy object: the registration-time `continuous_ensemble_v2` control below.
  “Latest” or “then-current at execution” is forbidden. The canonical compact JSON
  serialization (UTF-8, sorted keys, separators `(',', ':')`) is exactly:

```json
{"btc_risk":{"arm_id":"CTRL_BTC_RISK_70_90_35","components":["btc_trend_30d","btc_return_7d","btc_vol_30d","btc_trend_delta_7d"],"directions":{"btc_return_7d":"low","btc_trend_30d":"low","btc_trend_delta_7d":"low","btc_vol_30d":"high"},"high":0.9,"low":0.7,"min_prior":50,"tail_mult":0.35},"component_tp":0.12,"control_component_config_hashes":{"turn3p3":"f4f75d9e0547","turn4p3":"6e5f7336851e","turn4p5":"89011515e462"},"frozen_forward_config":{"entry_sizing":{"mode":"inverse_vol","target_vol_per_name":0.01,"vol_weight_clamp":2.0},"hedge":{"beta_min_obs":60,"beta_window_days":90,"cost_bps":5.0,"hedge_cap":2.0,"instrument":"BTCUSDT","instrument2":"ETHUSDT","regime":{"kind":"btcvol","lam":0.5,"pct_window":250,"vol_window":30}},"inception_day_ms":1680307200000,"object":"continuous_winner_uptrend_ensemble_btc_hedged","rebalance":{"drawdown_half_threshold":-0.04,"drawdown_zero_threshold":null,"enabled":false,"max_scale":4.0,"realized_vol_window_days":90,"resize_cost_bps":10.0,"strategy_momentum_window_days":0,"target_daily_vol":0.045},"weights":{"turn3p3":0.3333333333333333,"turn4p3":0.2222222222222222,"turn4p5":0.4444444444444444}},"max_hold_hours":24,"signal_entry_delay_hours":1,"sniper_enabled":false}
```

  Its registration-object SHA-256 is
  `f51d13b08dabe32d60d06e6ad187279fb9114ae562e0555a29baf543a63a3f5b`;
  the nested frozen-forward-config hash is
  `c4eb2eed1658697aa1239afd847e0de9d04f87ffe98080d4607ea6c1fd86a4f6`.
  Any byte-level canonical-config or component-hash drift is a hard refusal and
  requires a new dated amendment before compute.
- Venues: Bybit linear USDT perpetuals and Binance USD-M perpetuals. Every serious
  cell runs both venues. A one-venue result cannot pass.
- Universe: `archive_trade_manifest` at each decision timestamp, including dead,
  renamed, migrated, and prelisted instruments. Current `exchangeInfo` or a
  current-symbol intersection is forbidden.
- Root identity: exact resolved root, selected parquet metadata fingerprint,
  manifest hash, feature-panel hash, funding hash, and the JSON receipt emitted by
  `scripts/granular_data_surface.py`. Roots must be frozen read-only for the run.
- Registered working window: use the full common window that passes the readiness
  gates below. Do not shrink the window after seeing treatment returns. If there is
  no common valid window of at least 365 calendar days and 500 control trades per
  venue, stop with `exploratory/data_insufficient`.
- Forward OOS: remains the post-fix demo/paper clock. Internal history is a working
  dataset, not pristine OOS.

## Readiness gate before compute

Run the operator audit for the exact proposed window and save its receipt:

```bash
.venv/bin/python scripts/granular_data_surface.py \
  --venue both --start <START> --end <END_EXCLUSIVE> \
  --datasets klines_5m,tick_ohlc_1m,funding,open_interest,premium_index_1h,taker_flow,metrics_5m,bookdepth_1h \
  --max-symbol-days <EXPLICIT_BOUND> \
  --output research/granular_adverse_risk/data_readiness.json
```

Hard gates:

1. Canonical 5-minute price paths have exactly 288 valid, unique bars on every
   symbol-day required by an included signal warm-up or active-trade path. The
   root-wide PIT symbol-day coverage must be at least 99.5%; every incomplete
   source day is frozen in the exclusion ledger before treatment PnL is read.
   Duplicate `(venue,symbol,ts_ms)`, non-finite/inconsistent OHLC, off-grid
   timestamps, or mixed flat/partitioned layout invalidates the run.
2. Every requested manifest date exists, contains readable parquet, and has stored
   `date` equal to its `date=YYYY-MM-DD` path. Every dataset fragment has the
   required schema; stored symbol/date identity matches its path; all fragments in
   a symbol-day are read together; duplicate observation keys invalidate the run.
3. Dataset-specific content gates match the operator audit exactly:
   - Bybit `tick_ohlc_1m`: 1,440 unique aligned timestamps/day with finite positive,
     internally consistent OHLC; Bybit and Binance `klines_5m`: 288 under the same
     rules.
   - Funding: unique timestamps and finite `funding_rate`; the trade-level ledger
     must prove every settlement in every held interval.
   - OI proxy: at least 20 unique observations/symbol-day, finite non-negative
     `open_interest` and `open_interest_value`, and one non-empty stamped interval;
     mixed intervals inside a cell are forbidden.
   - Premium: at least 20 unique observations/symbol-day with finite,
     high/low-consistent OHLC; premium values may be negative or zero.
   - Bybit taker flow: exactly 288 unique 5-minute rows/day with non-negative finite
     buy/sell quote amounts and counts. Binance FAPI taker flow: at least 20 unique
     hourly rows/day, non-negative finite buy/sell volumes and ratio, finite signed
     volume, imbalance in `[-1,1]`, and a non-empty interval stamp.
   - Binance Vision metrics: exactly 288 unique aligned rows/day with finite
     non-negative OI value/quantity and taker long/short ratio.
   - Binance bookDepth: exactly 24 unique aligned hours, exactly 10 unique bands
     per hour, and 240 unique `(timestamp,percentage)` rows/day, with finite
     non-negative depth/notional and positive snapshot counts. It remains capacity
     context, not a signal gate.
4. The feature builder writes an explicit exclusion ledger. No missing 5-minute,
   OI, premium, or flow row may be forward-filled across an unknown gap. A cell may
   use only event rows for which every declared input is present and causal.
5. Funding covers every trade interval or the run stops. A dataset-directory or
   symbol-day presence check alone is not proof of every settlement.
6. Bybit and Binance feature definitions, units, sign conventions, and availability
   delays are reconciled in a schema receipt. Binance Vision metrics may supply
   historical OI/taker ratios; recent-window FAPI OI/taker tables cannot be
   represented as long-history coverage.
7. The existing Binance flat-per-symbol `klines_5m` layout must not be extended by
   the date-partitioned backfiller. Normalize into a new frozen root, prove numerical
   equivalence with matching NaN positions, then atomically promote the root, or
   stop. Mixed layouts are invalid.
8. Bybit/Binance resolved roots and selected child scopes are disjoint; equality,
   nesting, symlink aliases, or shared dataset targets are fatal.
9. Any root refresh uses `--execute`, explicit start/end, `--symbols` or
   `--all-pit-symbols`, and a new immutable `.json` receipt outside both roots and
   downloader manifests. Audit mode performs no network calls and never overwrites
   an existing receipt.

## Causal timestamps and execution

All source candles are indexed by bar-open time. Let `signal_ts` be the close of
the frozen 1-hour CONTINUOUS signal bar and let `common_entry_fill_ts` equal
`signal_ts + 1 hour`, preserving the registered one-hour entry delay.

- Entry `observation_cutoff_ts` is `common_entry_fill_ts - 5 minutes`. The latest
  price/flow bar ends at that cutoff and becomes available 60 seconds later. The
  latest hourly OI/premium observation must have closed by
  `common_entry_fill_ts - 1 hour` and becomes available five minutes after its
  close.
- Entry `decision_ts` is `common_entry_fill_ts - 4 minutes`, after every required
  base observation is available. `order_submit_ts = decision_ts`; every cell,
  including C0, receives the same submit time and fills at the open at
  `common_entry_fill_ts` plus the same adverse slippage. C1 therefore changes only
  notional, never timing, population, or fill price.
- For exit evaluation, `observation_cutoff_ts` is the close of a post-entry
  5-minute bar. Its price/flow row becomes available 60 seconds later;
  `decision_ts` and `order_submit_ts` are no earlier than that. Because OHLC cannot
  prove a part-bar fill, the exit fills at the open of the first full 5-minute bar
  beginning strictly after submission, plus adverse slippage.
- The mandatory latency-stress copy adds 10 minutes to source availability and
  selects older observations that were available by the same entry decision time;
  it may not delay only a treatment cell or change the common fill.
- Feature eligibility always requires `data_available_ts <= decision_ts`. The last
  six price/flow bars are fully closed and available; hourly inputs use the latest
  fully closed hour whose delay elapsed. No signal-close, data-release instant, or
  same-bar fill is permitted.
- `state_initialization_ts` is the actual common entry fill. For a short, MAE at
  evaluation time `t` is frozen as
  `max(0, max(high from entry fill through observation_cutoff_ts) / entry_fill_price - 1)`.
  It accumulates **since entry**, not since activation, in every cell and null tape.
- `exit_activation_ts` is entry fill plus 60 minutes. The MAE state above continues
  to accumulate from entry, but no new adverse-exit decision may fire before
  activation. Holding age, cooldown, TP, hedge, and max-hold state also initialize
  at the actual fill; no state is warm-started at activation.
- Standing TP12 remains live. If it fills before a new adverse-exit order can be
  submitted, the trade is already closed. Unknown intrabar ordering uses the
  conservative outcome for the treatment, with the ambiguity count reported.

Base execution costs use the venue/tier taker fee, realized funding path, and 8 bps
adverse slippage per new aggressive fill. Mandatory stresses use 16 bps and 2x fees.
Notional is capped by the existing portfolio rules, 10 bps of prior closed 5-minute
quote turnover, and 1 bp of latest causal OI value. Binance historical bookDepth may
add a stricter capacity/slippage stress. It cannot loosen the Bybit assumption.

## Frozen pulse

Direction is defined from the short's perspective; positive values are adverse.
At each eligible timestamp compute, only from available observations:

- adverse 30-minute return from the six prior closed 5-minute bars;
- 1-hour change in OI value;
- latest confirmed premium-index level;
- 30-minute taker-buy imbalance.

For each feature separately, the percentile denominator is every symbol in that
venue's PIT manifest at the timestamp whose feature value is finite, whose complete
source window is available by `decision_ts`, and whose instrument is tradable under
the manifest. The subject symbol is included. At least 20 valid peers (including the
subject) are required for **each** feature; otherwise that feature and the pulse are
missing. Missing peers are excluded only from that feature's denominator and their
counts are recorded; they are never assigned a median/zero. A missing subject input
means `pulse=false` with an explicit reason.

Ranks are ascending in adverse-risk direction and use deterministic midranks for
ties. With `n >= 20`, percentile is `(midrank - 1) / (n - 1)`. No future
normalization, current-survivor denominator, or full-sample z-score is allowed. The
crowding score is the unweighted mean of all four percentiles.

The pulse is true only when adverse-return percentile is at least 0.90 and crowding
score is at least 0.80. These thresholds are frozen. No neighboring threshold grid
is permitted in this experiment.

## Frozen matrix

| Cell | Entry action | Post-entry action | Role |
| --- | --- | --- | --- |
| `C0_CONTROL` | frozen selection/sizing at the common submit/fill clock | TP12 / 24-hour lifecycle unchanged | common control |
| `C1_ENTRY_HALF` | pulse computed before the common fill halves component notional; timing/fill unchanged | unchanged | secondary entry-risk test |
| `C2_NEXT5_EXIT` | same entry as C0 | after 60 minutes, pulse plus since-entry MAE >= 5% submits one full reduce-only exit using the timing above | **primary** adverse-exit test |
| `C3_COMBINED` | same as C1 | same as C2 | secondary interaction test |

There is no parameter selection across cells. `C2_NEXT5_EXIT` decides the registered
hypothesis. C1/C3 cannot rescue a failed primary cell and cannot be called a winner
without a new preregistration and untouched evidence.

## Frequency-matched null

For each venue and treatment cell, create 500 deterministic null tapes. Entry risk
has one decision and at most one intervention per trade. Exit risk is a sequential
risk set: evaluate chronologically only trades still open in that cell/tape and not
previously intervened on; the first qualifying pulse may submit one exit, after
which that trade leaves all later risk sets. No trade can be sampled twice.

Each null tape has exactly the observed distinct-trade intervention count and is
sampled without replacement from eligible C0 states, matched on calendar month,
signal component, entry hour, and BTC-risk regime. Exit nulls additionally match
holding-age bucket (`1-3h`, `3-6h`, `6-12h`, `12-24h`) and the same since-entry MAE
bucket (`<2%`, `2-5%`, `5-10%`, `>=10%`). Entry nulls match pre-entry 30-minute
volatility decile. All positions are shorts, so side is fixed rather than a vacuous
matching field.

Insufficient strata are handled before PnL: a month-stratum with fewer than 10
eligible distinct trades is deterministically pooled to its calendar quarter while
every other matching key stays fixed. If the quarter-stratum still has fewer
distinct eligible trades than the observed interventions it must supply, the null
and primary verdict are `data_insufficient`; there is no further pooling, sampling
with replacement, seed retry, or dropped stratum. All 500 seeds (`0..499`) must
complete. Null interventions use the exact treatment timing/action/costs. Report the
full distribution, not only a p-value.

## Artifacts and metrics

Required artifacts, separately for each venue and cell:

- immutable data-readiness receipt, root/manifest/config/code hashes, and schema
  reconciliation;
- PIT event population and excluded-row ledger with reason counts;
- feature panel containing source timestamp, availability timestamp, decision
  timestamp, submit timestamp, fill timestamp, all percentile inputs, per-feature
  valid/missing peer counts, midranks, and denominator sizes;
- sequential risk-set ledger containing every eligible evaluation, open/closed
  state before decision, since-entry MAE, first-intervention marker, and exclusion
  reason; one intervention per trade is asserted;
- order/trade ledger, rejected/missed/capacity-clamped rows, funding ledger, hedge
  ledger, and daily marked-to-market equity curve;
- split metrics by venue, calendar year, thirds, component, symbol concentration,
  pulse/no-pulse, holding-age, BTC regime, and worst synchronized cluster;
- total/annualized return, max drawdown, MAR, daily Sharpe, worst day, CDaR95,
  daily ES99, worst 30/90-day return, time under water, intervention count, return
  removed/saved, turnover, fees, funding, slippage, capacity utilization, and TP vs
  adverse-exit collision count;
- 8/16 bps, 1x/2x fee, delayed-auxiliary, leave-one-month-out, and 500-tape null
  results;
- `scripts/r1_robustness.py` Tier-2/fragility output and the strict reference output
  from `scripts/apply_decision_rule.py`;
- one final verdict receipt with exactly one allowed run label.

### Prospective governance amendment (2026-07-10; no treatment run executed)

The original text dynamically inherited the “current” Tier-2 thresholds from a
script at execution time. That did not actually freeze the decision rule. Before
any treatment result was produced, this amendment replaced the dynamic reference
with the exact `legacy-tier2-mar-v1` preset below.

The registered `_tier2_verdict` function source SHA-256 is
`91b7908799314334ca97433d02246db69fe2c00bda9941ada7c7b408e7dcb6ae`.
The contract remains the numeric rule below if unrelated script text changes; a
change to the function hash requires a prospective amendment before execution.

## Decision rule

The primary cell passes investigation only if all conditions hold:

1. `scripts/r1_robustness.py` returns `DEMO-ELIGIBLE` under the frozen
   `legacy-tier2-mar-v1` preset: cell and control are full-PIT on both venues;
   Bybit, Binance, and pooled MAR deltas are finite; both venue returns are
   positive; neither venue drawdown is worse than -70%; pooled MAR delta (the
   unweighted mean of venue deltas) is strictly greater than +0.10; the weaker
   venue MAR delta is at least -0.50; and trade counts are at least 30 Bybit / 20
   Binance. The thresholds may not be inherited from a later script version.
2. C0 and all treatment cells have identical signal population, common entry submit
   and fill timestamps/prices, and pre-treatment notional inputs; only C1/C3's
   registered half-size action may alter entry notional. Any timing/population drift
   invalidates the experiment.
3. Total return is positive on both venues, with at least 30 primary interventions
   per venue. Less is `data_insufficient`, not a pass.
4. Max drawdown, worst day, and daily ES99 are no worse than control on either venue;
   pooled CDaR95 loss improves by at least 10%.
5. The pooled CDaR95 improvement exceeds the 95th percentile of the frequency-
   matched null, and the return delta is not below the null's 5th percentile.
6. All 500 null tapes complete under the frozen sequential/stratum rules. Any
   insufficient quarter-stratum is `data_insufficient`, not a waived comparison.
7. The result keeps its direction under 16 bps slippage, 2x fees, the auxiliary
   latency stress, every calendar third, and leave-one-month-out. One venue cannot
   compensate for the other.
8. No single symbol contributes more than 25% of the tail improvement, and removing
   the 1000TAGUSDT incident does not reverse the sign.

Failure of any condition rejects this exact mechanism. A pass permits only a
separately reviewed demo/paper **shadow** that emits hypothetical exit/size decisions
without orders. It is not `candidate`, `paper_ready`, mainnet evidence, or permission
to change the deployed profile. No legacy Tier-3 diagnostic is mainnet evidence
or authorization; any mainnet proposal is governed separately by
`docs/governance.md`.

## Checkpoints and resume contract

1. `00_readiness`: audit/schema/root receipts; stop on any hard gate.
2. `01_population/<venue>/<month>`: atomic event/exclusion parquet plus hash.
3. `02_control/<venue>`: control ledger/equity/metrics completed before treatments.
4. `03_cells/<venue>/<cell>`: one atomic receipt per cell; failed cells remain in the
   matrix and are never omitted from the verdict table.
5. `04_null/<venue>/<cell>/<batch>`: batches of 50 seeds with cumulative manifest;
   resume skips only hash-matching complete batches.
6. `05_robustness`: split/stress/null aggregation and canonical decision tools.
7. `06_verdict`: final human-readable receipt. No automatic config edit, promotion,
   deployment, or live order action.

Every checkpoint writes to a temporary file then atomically renames it, records its
input hashes, and refuses a mixed hash on resume. A crash may recompute an incomplete
checkpoint but may not silently merge it with another root/config/code identity.

## Liquidation and depth boundary

Bybit liquidation broadcasts and order-book depth snapshots are forward-only,
sampled/contextual tapes. They may annotate post-registration demo-shadow events and
help explain execution after the fact. They are prohibited from historical feature
construction, backfilled labels, threshold selection, the frequency-matched null,
or any acceptance condition.

Binance Vision bookDepth is historical but one-venue. It may tighten Binance
capacity/slippage stress; it cannot be a primary signal or substitute for missing
Bybit history. This boundary is non-negotiable.
