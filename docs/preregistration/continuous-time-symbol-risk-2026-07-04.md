# Pre-Registration: Continuous Blacklist And Entry-Time Risk Controls

Date: 2026-07-04
Stage: proposed
Run label until proven otherwise: exploratory

## Current Read

This overwrites the earlier time-boundary-first plan.

The completed Bybit time-boundary cells underperformed the current control:
`time_control` returned +24.63% with MAR 6.33, while every completed forced
boundary cut/downsize arm had lower return and lower MAR. Binance was stopped
by operator instruction before the two-venue verdict. That means the time-stop
branch is not accepted and is not the next compute priority.

The active research question is now narrower and cleaner:

- Keep the current TP12/24h lifecycle.
- Do not combine blacklist work with forced UTC time stops.
- Test whether causal symbol and entry-time blacklists can avoid repeated bad
  entries without deleting the TP tail.

Any venue-subset run is diagnostic only. The Tier-2 research gate still
requires both Bybit and Binance.

## Hypotheses

### H1: Month-Scale Symbol Toxicity

Some symbols repeatedly damage the continuous fade book after recent realized
losses. A local blacklist can be causal if it uses only prior realized exits
known at the new candidate's `decision_ts`.

Use the canonical month length:

```text
MONTH_DAYS = 365.25 / 12 = 30.4375
```

Expected good behavior:

- Lower repeat symbol drawdowns.
- Lower worst-symbol contribution and top-10 concentration.
- Similar or better MAR and ES99 on both venues.
- TP contribution mostly retained.

Falsifiers:

- The rule works only on one venue.
- It blocks so many candidates that it just turns the book off.
- It removes the TP tail.
- Any blacklist state uses future exits, full-period symbol PnL, or current
  universe knowledge.

### H2: Learned Entry-Time Blacklist

The book may have recurring toxic entry windows: not just a single UTC hour,
but combinations of hour-of-week, component, symbol, crowding, funding
proximity, and regime context where multiple entries historically become
unprofitable. A causal online learner can skip or downsize future entries in
those windows after enough prior evidence accumulates.

This is a blacklist, not a price stop. It decides before entry. It must not
close open trades, and it must not know whether the current candidate will win.

Expected good behavior:

- Bad multi-entry time buckets get skipped/downscaled after prior evidence.
- The model generalizes through shrinkage instead of memorizing one-off hours.
- Improvement survives both venues and hash/permutation controls.

Falsifiers:

- The learner is just a mined clock curve.
- It learns from exits unavailable at decision time.
- Hash time buckets or label-permutation controls perform as well.
- It blocks high-quality crowded TP windows.

### H3: Frozen Permanent Blacklist

A fixed symbol blacklist can support research only if it is frozen from a train
window or justified by external operational facts. A full-period worst-symbol
list is look-ahead and cannot be acceptance evidence.

Expected good behavior:

- Train-frozen exclusions improve validation/OOS on both venues.
- The excluded symbols have stable toxicity or objective venue-specific
  operational reasons.

Falsifiers:

- The list works only inside the train window.
- Venue overlap is weak and no external reason exists.
- Full-period PnL-mined names are used as an acceptance cell.

## Data

- Venues: Bybit and Binance.
- Roots:
  - `/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_full_pit`
  - `/Users/jhbvdnsbkvnsd/SHARED_DATA/binance_full_pit`
- Window: `2023-04-01` through the latest fully closed signal day available at
  dispatch.
- Universe: full PIT only. No current-universe diagnostic may enter a verdict
  table.
- Control:
  - TP12 components.
  - 24h max hold.
  - `BTC_TREND_GATE=uptrend`.
  - inverse-vol sizing.
  - `CTRL_BTC_RISK_70_90_35`.
  - BTC/ETH hedge and BTC-vol regime overlay.
  - current max-active and max-new limits.
- Costs: retain fees, spread, impact, hedge costs, and funding. Label any
  partial funding by venue/window.

## Timing And State Contract

Every treatment must declare:

- `decision_ts`: candidate signal time.
- `data_available_ts`: latest model/state timestamp allowed at decision.
- `order_submit_ts`: simulated entry submit time.
- `fill_window`: existing continuous fill model.
- `exit_activation_ts`: unchanged TP12/24h lifecycle activation.
- `state_initialization_ts`: first timestamp used to initialize blacklist/model
  state.

State updates are allowed only from exits with `exit_ts_ms < decision_ts` plus
an explicit latency buffer. Default buffer: 1 hour. A 24h-delayed copy must be
reported as a robustness diagnostic for the learned entry-time model.

## Stage 0: Control And Tapes

Rebuild or resume the current control on both venues.

Required outputs:

- Full component ledgers.
- Combined equity curve.
- Candidate tape with selected/rejected reasons.
- Per-candidate entry context:
  - UTC hour.
  - UTC day of week.
  - hour of week.
  - component.
  - symbol.
  - candidate count in the same decision hour.
  - funding proximity bucket.
  - BTC trend/regime sizing state.
- Symbol/event state snapshots.

## Stage 1: No-Time-Stop Local Symbol Blacklists

All Stage 1 arms keep `research_time_boundary_rule=off`. They do not close or
downsize open trades by clock time.

Registered local arms:

- `local_loss_1m_1`: block symbol for `MONTH_DAYS` after any prior realized
  trade with raw return <= -20%.
- `local_loss_3m_1`: block symbol for `3 * MONTH_DAYS` after any prior
  realized trade with raw return <= -20%.
- `local_repeat_loss_2m_2`: block symbol for `2 * MONTH_DAYS` after two prior
  net-negative realized trades in the trailing `3 * MONTH_DAYS`.
- `local_boundary_fail_1m`: block symbol for `MONTH_DAYS` after a prior trade
  was still open at 00:00 UTC, had `mfe_at_boundary < 4%`, and later closed
  net-negative. This uses the boundary only as a causal diagnostic feature, not
  as a forced exit.
- `local_toxic_half_2m`: same trigger as `local_repeat_loss_2m_2`, but size at
  50% instead of blocking.

Required diagnostics:

- Symbols quarantined.
- Entries blocked/downscaled.
- Quarantine duration distribution.
- Later performance of quarantined symbols.
- TP bucket retained and removed.

## Stage 2: Learned Entry-Time Blacklist

This is the "multiple entries and certain times keep doing unprofitable things"
branch.

The model is an online, hierarchical, empirical-Bayes blacklist. It is not a
generic high-capacity classifier. It should be boring enough to audit and smart
enough to share information across related buckets.

### Features

All features must be known at `decision_ts`:

- `hour_utc`.
- `day_of_week_utc`.
- `hour_of_week = day_of_week_utc * 24 + hour_utc`.
- `component`.
- `symbol`.
- `candidate_count_same_hour`.
- `same_hour_rank`.
- `funding_proximity_bucket`: before funding, after funding, or not near
  funding.
- `btc_trend_gate_state`.
- `btc_risk_size_bucket`.

No future return, future exit reason, same-hour final basket result, or
full-period symbol contribution may enter a feature.

### Learner

Maintain rolling bucket posteriors from prior realized trades only:

- Response 1: realized `book_net_return`.
- Response 2: loss indicator, `book_net_return < 0`.
- Response 3: TP indicator, `exit_reason == take_profit`.
- Parent buckets:
  - global.
  - hour-of-week.
  - component x hour-of-week.
  - symbol x hour-of-week.
  - funding-proximity x hour-of-week.
- Shrink child buckets toward parent buckets when sample size is small.
- Decay observations with half-life `6 * MONTH_DAYS`.
- Minimum effective sample before action: 40 component rows for parent buckets,
  12 component rows for symbol-child buckets.

Action rule:

- Skip/downsize only if the posterior expected net return is negative and
  `P(bucket_mean < 0) >= 0.80`.
- For crowded windows, require `candidate_count_same_hour >= 2` and a toxic
  parent bucket before blocking multiple entries at once.
- If a child bucket is toxic but the parent bucket is not, prefer 50% downsize
  over skip unless the child has at least 30 effective rows.

### Registered Arms

- `entry_time_hour_week_eb_skip`: skip candidates in toxic hour-of-week buckets.
- `entry_time_hour_week_eb_half`: 50% size in toxic hour-of-week buckets.
- `entry_time_component_hour_eb_skip`: skip toxic component x hour-of-week
  buckets.
- `entry_time_symbol_hour_hier_half`: 50% size toxic symbol x hour-of-week
  child buckets using parent shrinkage.
- `entry_time_crowded_bucket_skip`: when `candidate_count_same_hour >= 2`, skip
  all non-top-ranked candidates in toxic parent buckets.
- `entry_time_hybrid_half_then_skip`: first toxic month downsizes 50%; a second
  toxic month in the same bucket skips until posterior recovery.

### Negative Controls

- `entry_time_hash_bucket_eb`: replace hour-of-week with deterministic hash
  buckets of the same cardinality.
- `entry_time_delayed_state_24h`: same model, but every state update is delayed
  24h. A real edge should degrade gracefully, not disappear only because the
  base model leaked same-day information.
- `entry_time_label_permutation`: keep candidate times fixed, permute realized
  outcomes within month and component. This must not pass.

Required artifacts:

- `entry_time_blacklist_events.csv`
- `entry_time_bucket_scores.csv`
- `entry_time_model_state.parquet`
- `entry_time_state_updates.parquet`
- per-cell `config.json` with feature list, priors, thresholds, and model hash.

## Stage 3: Frozen Permanent Symbol Blacklists

Permanent means fixed after a training cutoff, not mined from the full backtest.

Training freeze:

- Use trades with `entry_ts_ms < 2025-06-01T00:00:00Z`.
- Freeze candidate symbol lists from the train window only.
- Apply fixed lists only from `2025-06-01` onward for validation/OOS scoring.

Registered permanent lists:

- `perm_train_worst_10`: worst 10 symbols by train component-weighted net
  contribution, minimum 5 component rows.
- `perm_train_worst_25`: worst 25 symbols by train component-weighted net
  contribution, minimum 5 component rows.
- `perm_train_tail_10`: top 10 symbols by train worst single-trade loss,
  minimum 3 component rows.
- `perm_structural`: externally justified operator list only. Each symbol must
  have a written reason and `data_available_ts`.

Full-period worst-symbol lists may be printed as diagnostic context only. They
must not be replayed as acceptance cells.

## Stage 4: No-Time-Stop Combined Cells

Do not run a full Cartesian product.

Run only after individual arms finish:

1. Best Stage 1 local symbol rule alone.
2. Best Stage 2 entry-time blacklist rule alone.
3. Best Stage 1 local symbol rule + best Stage 2 entry-time rule.
4. Best train-frozen permanent rule + best Stage 2 entry-time rule, only if the
   permanent rule passes validation/OOS on both venues.

No combined cell may include a forced UTC time stop unless a new dated
pre-registration reopens the time-stop branch with a falsifier that explains
why the Bybit failure should not generalize.

## Metrics

Report per venue and pooled:

- Total return, annualized return, max drawdown, MAR, Sharpe, worst day.
- Split metrics by thirds and by train/validation/OOS.
- Leave-one-month-out MAR delta.
- Bootstrap p5 for pooled MAR delta.
- Daily ES95 and ES99.
- CDaR95.
- Worst symbol contribution and top-10 symbol concentration.
- TP bucket retained and removed.
- Candidate count, blocked count, downscaled count, and block/downscale share.
- For entry-time models:
  - toxic bucket count.
  - bucket sample sizes.
  - posterior expected net return.
  - posterior loss probability.
  - parent-vs-child shrinkage weight.
  - performance after bucket activation.
  - recovery/deactivation counts.
- Funding mode and PIT pass/fail.

## Decision Rule

Default verdict is reject.

A local symbol blacklist may advance only if all are true:

- Full PIT passes on both venues.
- Return is positive on both venues.
- It improves pooled MAR or pooled ES99.
- It does not remove more than 20% of TP contribution on either venue.
- It blocks/downsizes fewer than 25% of candidate entries.
- It is causal at every `decision_ts`.

An entry-time blacklist may advance only if all are true:

- Full PIT passes on both venues.
- Return is positive on both venues.
- Pooled MAR improves or ES99 improves by at least 10%.
- Neither venue's MAR is worse than control by more than 5%.
- TP retained is at least 85% of control contribution on both venues.
- Candidate block/downsize share is below 25%.
- Hash-bucket and label-permutation controls do not match or beat it.
- 24h-delayed state remains directionally similar; if it collapses, treat the
  base result as possible leakage until proven otherwise.

A permanent blacklist may advance only if all are true:

- The list is frozen from train-only data or externally justified.
- Validation/OOS improves on both venues.
- It has at least 60% symbol-overlap logic agreement across venues, or the
  reason is objective and venue-specific.
- Full-period PnL-mined lists are not used for acceptance.

Promotion to live or real money is impossible from this run. A pass authorizes
only an implementation PR or forward shadow in demo/paper.

## Implementation Notes

Use a dated dispatcher. Do not revive generic deleted sweep scripts.

Required engine hooks/artifacts:

- Research-only symbol admission and size-multiplier hook.
- Candidate-sink rows for selected/rejected reasons.
- Causal blacklist state object keyed by venue and component.
- Entry-time model state persisted before and after replay.
- Explicit event CSVs for every skipped/downscaled candidate.

Expected files:

- `config.json`
- `summary.csv`
- `verdict.md`
- `bybit/<cell>/continuous_trades.csv`
- `bybit/<cell>/continuous_equity.csv`
- `bybit/<cell>/symbol_quarantine_events.csv`
- `bybit/<cell>/entry_time_blacklist_events.csv`
- `bybit/<cell>/entry_time_bucket_scores.csv`
- `bybit/<cell>/entry_time_model_state.parquet`
- `bybit/<cell>/permanent_blacklist_train_freeze.csv`
- matching `binance/<cell>/...` files.

## Command

Full two-venue evidence command:

```bash
.venv/bin/python scripts/continuous_time_symbol_risk_2026_07_04.py \
  --bybit-root /Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_full_pit \
  --binance-root /Users/jhbvdnsbkvnsd/SHARED_DATA/binance_full_pit \
  --out /Users/jhbvdnsbkvnsd/SHARED_DATA/continuous_time_symbol_risk_2026-07-04
```

Any venue-subset run must write an inconclusive verdict and be described as a
diagnostic, not acceptance evidence.

## Result

Pending. The prior Bybit-only time-stop results are negative mechanism evidence
and do not authorize a runtime change.
