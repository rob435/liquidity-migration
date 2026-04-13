# Backtest Research Plan

This repo now has enough backtest machinery to do real research. That does not mean it is safe to brute-force parameters until something looks pretty.

This document is the testing discipline for this system.

## Goal

Find a configuration that is:

- profitable enough after costs
- robust enough across different windows
- selective enough to avoid obvious chop / anti-momentum days
- simple enough that the edge is still believable

The plan is designed to reduce three failure modes:

- overfitting one recent window
- lying to ourselves with giant unfocused parameter grids
- optimizing exits before fixing entry quality

## Core Principles

1. Freeze the market history for a test batch.
   Use the same cached replay plan for all variants in that batch.

2. Optimize one parameter family at a time first.
   This is the correct first pass. It is not the final pass.

3. Check interactions only after pruning.
   Some knobs interact materially. Blind full-factor grids across everything are a waste.

4. Use fast mode for search, full mode for validation.
   `--research-fast` is for ranking candidates, not for final trust.

5. Do not claim a result from the same windows used to choose the parameters.
   Finalists must survive a holdout or walk-forward pass.

6. Prefer robust winners over peak winners.
   A parameter set that wins slightly less but survives more windows is better than a flashy overfit.

## Dataset Policy

For the current system, the honest baseline is:

- current live logic
- current default universe
- current minute-aware replay
- current fee / slippage assumptions

Recommended research stages:

1. Short sampled windows for pruning
   Purpose: eliminate bad ideas cheaply.

2. Medium continuous windows for sanity
   Purpose: catch churn, clustering, and ugly drawdowns.

3. Full 1-year validation on finalists only
   Purpose: judge whether the candidate survives a serious horizon.

Do not start with full-year giant sweeps across dozens of variants. That is expensive and encourages cargo-cult interpretation.

For long grids, use the runner's own throughput diagnostics instead of guessing from CPU graphs. It now prints per-variant completion timing, running average variant time, and ETA for the remaining pending variants.

## Metrics That Matter

Primary:

- `net_pnl_usd`
- `total_return_pct`
- `max_drawdown_pct`
- `profit_factor`
- `expectancy_usd`
- `trade_count`

Trade-quality diagnostics:

- `avg_mfe_pct`
- `avg_mae_pct`
- `avg_post_exit_best_pct`
- `avg_post_exit_worst_pct`
- `avg_volatility_pct`

Selection / throttle diagnostics:

- `entry_ready_signals`
- `entries_filled`
- `skipped_max_open_positions`
- `skipped_max_entries_per_rebalance`
- `skipped_daily_stop_losses`

Do not optimize on win rate alone. That is how bad systems get dressed up as good ones.

## Parameter Priority

### Tier 1: Optimize First

These are the highest-leverage knobs for the current system.

1. `hurst_cutoff`
   Current default: `0.55`
   First-pass range:
   - `0.45`
   - `0.50`
   - `0.55`
   - `0.60`

2. Intraday regime filter
   Current defaults:
   - `intraday_regime_min_breadth=0.55`
   - `intraday_regime_min_efficiency=0.35`
   - `intraday_regime_min_basket_return=0.00`
   - `intraday_regime_min_leadership_persistence=0.25`
   - `intraday_regime_min_pass_count=4`

   First-pass ranges:
   - `intraday_regime_min_breadth`: `0.45`, `0.55`, `0.65`
   - `intraday_regime_min_efficiency`: `0.25`, `0.35`, `0.45`
   - `intraday_regime_min_basket_return`: `-0.002`, `0.00`, `0.002`
   - `intraday_regime_min_leadership_persistence`: `0.15`, `0.25`, `0.35`
   - `intraday_regime_min_pass_count`: `3`, `4`

3. `entry_ready` selectivity
   Current defaults:
   - `entry_ready_min_observations=3`
   - `entry_ready_min_rank_improvement=2`
   - `entry_ready_min_composite_gain=0.00`
   - `entry_ready_top_n=4`

   First-pass ranges:
   - `entry_ready_min_observations`: `3`, `4`, `5`
   - `entry_ready_min_rank_improvement`: `1`, `2`, `3`
   - `entry_ready_min_composite_gain`: `0.00`, `0.05`, `0.08`
   - `entry_ready_top_n`: `2`, `3`, `4`

### Current Baseline From the 30-Day Grid

Use this as the active research baseline until a later out-of-sample window disproves it:

- `entry_ready_min_composite_gain=0.00`
- `entry_ready_min_observations=3`
- `intraday_regime_min_pass_count=4`

The strongest signal from the completed 30-day grid was not "tighten everything." It was: keep the intrabar confirmation modest, and make the session-quality gate stricter.

4. Residual momentum / clustering
   Current defaults:
   - `momentum_reference_mode=cluster_relative`
   - `momentum_reference_blend_btc_weight=0.35`
   - `cluster_assignment_mode=dynamic`
   - `cluster_correlation_lookback_bars=48`
   - `cluster_correlation_threshold=0.70`

   First-pass ranges:
   - `momentum_reference_mode`: `basket_relative`, `cluster_relative`, `hybrid_relative`
   - `momentum_reference_blend_btc_weight`: `0.25`, `0.35`, `0.50`
   - `cluster_assignment_mode`: `dynamic`, `hybrid`
   - `cluster_correlation_lookback_bars`: `32`, `48`, `64`
   - `cluster_correlation_threshold`: `0.60`, `0.70`, `0.80`

### Tier 2: Optimize After Tier 1

These matter, but only after the engine is selecting cleaner trades.

1. Exit structure
   Current defaults:
   - `take_profit_pct=0.02`
   - `stop_loss_pct=0.02`

   First-pass ranges:
   - `take_profit_pct`: `0.015`, `0.02`, `0.025`, `0.03`
   - `stop_loss_pct`: `0.015`, `0.02`, `0.025`

2. Portfolio throttles
   Current defaults:
   - `max_open_positions=3`
   - `max_entries_per_rebalance=0`
   - `max_daily_stop_losses=0`

   First-pass ranges:
   - `max_open_positions`: `2`, `3`, `4`
   - `max_entries_per_rebalance`: `1`, `2`, `0`
   - `max_daily_stop_losses`: `1`, `2`, `3`, `0`

### Tier 3: Optimize Last

Only touch these after the above families stop producing meaningful gains.

- `momentum_lookback`
- `momentum_skip`
- `top_n`
- `momentum_weight`
- `curvature_weight`
- `momentum_z_clip`
- `curvature_z_clip`
- `btc_realized_vol_threshold`

These are more likely to waste time or overfit if the higher-level filters are still weak.

## What Not To Optimize Early

Do not start by tuning:

- fee assumptions
- slippage assumptions
- cooldowns
- macro refresh timing
- websocket or queue settings
- logging / analytics switches

Those are not the main source of strategy edge.

## Research Workflow

### Phase 0: Reproducible Baseline

Run one baseline batch and save the exports.

Example:

```bash
python backtest.py \
  --cycles 96 \
  --research-fast \
  --export-dir ./research/baseline-96
```

Then run a longer baseline:

```bash
python backtest.py \
  --cycles 96 \
  --sweep-lookback-days 365 \
  --sweep-step-days 30 \
  --research-fast \
  --export-dir ./research/baseline-sweep
```

For full-year or overnight work, pin the horizon explicitly:

```bash
python backtest.py \
  --cycles 35040 \
  --end-date 2026-04-11 \
  --export-dir ./research/year-baseline
```

Purpose:

- prove the current default still behaves sanely
- establish comparison numbers for every future experiment

### Phase 1: Single-Family Pruning

This is where one-at-a-time optimization is correct.

Recommended order:

1. `hurst_cutoff`
2. intraday regime thresholds
3. `entry_ready` thresholds
4. residual momentum / clustering

Example:

```bash
python backtest.py \
  --cycles 96 \
  --research-fast \
  --variant-workers 4 \
  --grid-setting hurst_cutoff=0.45,0.50,0.55,0.60
```

Then:

```bash
python backtest.py \
  --cycles 96 \
  --research-fast \
  --variant-workers 4 \
  --grid-setting intraday_regime_min_breadth=0.45,0.55,0.65 \
  --grid-setting intraday_regime_min_pass_count=3,4
```

And:

```bash
python backtest.py \
  --cycles 96 \
  --research-fast \
  --variant-workers 4 \
  --grid-setting entry_ready_min_observations=3,4,5 \
  --grid-setting entry_ready_min_composite_gain=0.00,0.05,0.08
```

And:

```bash
python backtest.py \
  --cycles 96 \
  --research-fast \
  --variant-workers 4 \
  --grid-setting momentum_reference_mode=basket_relative,cluster_relative,hybrid_relative \
  --grid-setting cluster_assignment_mode=dynamic,hybrid
```

Keep only the top two or three candidates from each family.

If a large grid is interrupted, resume it instead of rerunning everything:

```bash
python backtest.py \
  --cycles 35040 \
  --end-date 2026-04-11 \
  --variant-workers 4 \
  --export-dir ./research/year-grid \
  --resume-variants \
  --grid-setting momentum_reference_mode=basket_relative,cluster_relative,hybrid_relative \
  --grid-setting cluster_assignment_mode=dynamic,hybrid
```

### Phase 2: Interaction Checks

This is where one-at-a-time stops being enough.

Check only interactions that are likely to matter:

- `hurst_cutoff` x intraday regime strictness
- `entry_ready` strictness x intraday regime strictness
- residual reference mode x cluster assignment mode
- residual reference mode x intraday regime strictness
- `take_profit_pct` x `stop_loss_pct`
- `max_open_positions` x `max_entries_per_rebalance`

Do not test every possible pair. Test survivors only.

Example:

```bash
python backtest.py \
  --cycles 96 \
  --research-fast \
  --variant-workers 4 \
  --grid-setting entry_ready_min_composite_gain=0.00,0.05 \
  --grid-setting intraday_regime_min_pass_count=4
```

### Phase 3: Exit Optimization

Do this only after entry quality is cleaner.

Use:

- `avg_mfe_pct`
- `avg_mae_pct`
- `avg_post_exit_best_pct`
- `avg_post_exit_worst_pct`
- `avg_volatility_pct`

Interpretation:

- high `avg_post_exit_best_pct` after TP means TP may be too tight
- deeply negative `avg_post_exit_worst_pct` after SL can mean the stop saved you
- small `avg_post_exit_worst_pct` and strong positive `avg_post_exit_best_pct` after SL means the stop may be too tight
- high post-exit volatility means the path is noisy and simple TP widening may not help

Recommended first-pass exit grid:

```bash
python backtest.py \
  --cycles 96 \
  --research-fast \
  --variant-workers 4 \
  --grid-setting take_profit_pct=0.015,0.02,0.025,0.03 \
  --grid-setting stop_loss_pct=0.015,0.02,0.025
```

Use the built-in stress profiles on finalists before pretending the result is robust:

```bash
python backtest.py \
  --cycles 96 \
  --research-fast \
  --stress-profile costly \
  --stress-profile hostile \
  --grid-setting hurst_cutoff=0.50,0.55
```

### Phase 4: Portfolio Controls

After entry and exit candidates are narrowed, tune risk clustering controls.

Example:

```bash
python backtest.py \
  --cycles 96 \
  --research-fast \
  --variant-workers 4 \
  --grid-setting max_open_positions=2,3,4 \
  --grid-setting max_entries_per_rebalance=1,2,0 \
  --grid-setting max_daily_stop_losses=1,2,3,0
```

### Phase 5: Final Validation

Do not declare victory from sampled windows alone.

Finalists must pass:

1. medium continuous validation
2. full 1-year validation
3. holdout or walk-forward validation

Recommended final protocol:

1. Pick the top `2` to `5` parameter sets from Phases 1 to 4.
2. Rerun them in full mode, not `--research-fast`.
3. Run a continuous 90-day test.
4. Run a continuous 180-day test.
5. Run the full 1-year test on finalists only.
6. Run walk-forward validation and keep `walk_forward_candidates.csv`.
7. Run `reconcile.py` or `backtest.py --reconcile-telegram-html ...` against any known forward-trade windows.
8. Inspect `ticker_reconciliation.csv`, not just the aggregate summary, before trusting the alignment story.

For live forward-testing operations, prefer the daily wrapper:

```bash
./deploy/run_daily_forward_reconcile.sh
```

That keeps daily alignment checks outside the trading loop and persists the summary into the live SQLite DB.
8. Keep one untouched holdout period for the final decision.

## One-At-A-Time Optimization: When It Is Right And When It Is Wrong

### Correct Use

One-at-a-time is correct for:

- early pruning
- understanding sensitivity
- finding dead ranges quickly
- identifying which families actually matter

### Wrong Use

One-at-a-time becomes wrong when:

- you pretend parameters do not interact
- you optimize exits before entries
- you change five things after seeing one lucky sample
- you declare a final winner without interaction checks and holdout validation

The honest workflow is:

- one family at a time first
- then targeted pairwise interaction checks
- then full validation on finalists

## Suggested Acceptance Rules

A candidate should not advance unless it:

- beats the baseline on `net_pnl_usd`
- does not worsen `max_drawdown_pct` materially
- keeps a believable `trade_count`
- does not rely on one absurd outlier window
- improves or at least preserves `profit_factor`

Suggested rejection signs:

- trade count collapses to near zero
- profits come from one giant outlier day
- drawdown improves only because the strategy barely trades
- post-exit data shows the exits are obviously leaving too much on the table

## Storage And Naming

Use a clear export layout for every serious batch:

- `research/YYYY-MM-DD-phase0-baseline`
- `research/YYYY-MM-DD-phase1-hurst`
- `research/YYYY-MM-DD-phase1-regime`
- `research/YYYY-MM-DD-phase1-entry-ready`
- `research/YYYY-MM-DD-phase2-interactions`
- `research/YYYY-MM-DD-phase3-exits`
- `research/YYYY-MM-DD-phase4-portfolio`
- `research/YYYY-MM-DD-phase5-finalists`

Do not overwrite serious result folders. Cheap reruns are fine. Final comparisons need a paper trail.

## Brutally Honest Bottom Line

Yes, optimize one parameter family at a time first.

No, do not stop there.

The correct sequence for this repo is:

1. prune with one-family sweeps
2. test only the important interactions
3. optimize exits using excursion and post-exit data
4. validate finalists over longer continuous windows
5. keep a holdout period for the final decision

That is the fastest path that is still intellectually honest.
