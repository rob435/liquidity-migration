# Decisions

## 2026-05-03

- Add automated current-universe discovery, but label it as survivorship-biased
  until point-in-time delisted/dead-contract data is added.
- Test lower-cap exposure through daily liquidity-rank buckets inside a broad
  downloaded universe instead of hardcoding "shitcoin" lists.
- Keep core, mid, and tail bucket results separate. The broad one-year test
  showed the tail bucket's best direction is `short_high_long_low`, not the
  original `long_high_short_low` direction.
- Treat the tail-liquid reversal as a separate research lead. Do not blend it
  with the original 16-symbol result until it survives longer history,
  point-in-time universe checks, and execution/funding stress.
- Treat current-symbol daily-close fade runs as biased benchmarks only. Proper
  confirmation requires Bybit public archive symbol/date membership plus
  archive-derived 1m bars so delisted/dead contracts are present.
- Add `archive-manifest`, `archive_klines_1m`, `archive-download-klines`, and
  `--require-archive-membership` as the first point-in-time universe controls
  for the daily-close fade alpha.
- Correct daily-close fade short PnL for Bybit USDT linear perps. Short returns
  are `(entry_price - exit_price) / entry_price`; inverse-style math is wrong
  for this contract type.
- Lock the current daily-close fade stop at 20% per symbol. The 5% stop cuts
  drawdown but damages returns, 40% is too loose, and no-stop exposes the book
  to unacceptable squeeze damage. Use gross exposure, not a tighter stop, to
  dial risk down until the point-in-time archive run says otherwise.
- Keep the volume-alpha detailed backtester conservative for ambiguous hourly
  bars: if a stop and take-profit are both touched inside the same 1h bar, the
  stop wins.
- Keep repo-local Codex hooks and AO/Composio setup out of this repo. The repo
  should contain research code, configs, tests, and docs only; global agent
  tooling belongs in the user's Codex/tool environment.
- Add paper forward testing for daily-close fade before any demo execution.
  Forward testing may scan live public Bybit data and send Telegram
  notifications, but it must not use private Bybit keys or submit orders.
- Do not add a fixed TP to the daily-close default. The one-year TP sweep showed
  fixed TP caps too many winners, and VWAP-reversion exits had the same problem.
  The daily-close paper-forward default is now the adaptive exit that survived
  the current local checks: no fixed TP, baseline liquidity ranks 31-150, 20%
  disaster stop, `0.25x` daily-vol trail active after the 15-minute stop delay,
  and 20% MFE giveback after +1% favorable excursion.
- Use dynamic baseline liquidity rank instead of extending the static ignore
  list. The rank uses prior 7-day average quote turnover, not same-day pump
  turnover. This keeps top 20/30-type names out without stale manual symbol
  maintenance.
- Do not call the simple daily-close entry alpha confirmed. Without adaptive
  exits, the three-year current top-160 sample lost -60.96% with -90.38% max
  drawdown. The promising result is the entry plus adaptive exit stack, and it
  remains cost-sensitive and current-universe biased until the archive
  walk-forward universe is complete.
- Do not use fixed TP on the volume-alpha tail bucket yet. The 3-year sweep
  showed the best tail result uses no TP. The earlier one-year reversed-tail
  result is unstable until it survives the longer side-mode check.
- Treat daily-close rank 151+ as an experimental microcap sleeve, not as part
  of the core 31-150 book. Microcap tests must use turnover floors and
  capacity-limited sizing from account equity, day-to-date turnover, and prior
  baseline turnover; otherwise the backtest can invent fills that a small live
  account still could not get cleanly.

## 2026-05-02

- Strip the repo down instead of continuing to patch the old system.
- Make `docs/volume_alpha.md` the current implementation plan.
- Keep `docs/bybit_aggression_carry_system_codex_spec.md` as Bybit data/source
  reference material, not as permission to rebuild a blended composite.
- Delete the old live runtime and old root backtest stack because it depended on
  `SignalEngine`, `ExecutionEngine`, `MarketState`, old config, and alerting.
- Delete the old aggression/carry/momentum/quality/OI composite modules from the
  new package.
- Keep only the research pieces needed for the current alpha: downloaders,
  archive parsing, Bybit public data, ingestion helpers, storage, math utilities,
  and `volume_alpha.py`.
- Keep Codex/Graphify/AO tooling outside runtime dependencies.
- Keep phase one research-only: no live execution, alerts, kill switches,
  deployment, or exchange order submission.
- Add a separate `volume-backtest` command instead of overloading
  `volume-alpha`. The sweep tests signal evidence; the backtest records actual
  trade entries, exits, exit reasons, stops, costs, basket returns, and
  attribution.
- Keep the first detailed lifecycle non-overlapping: `rebalance_days` must be
  greater than or equal to `hold_days`. Overlapping sleeves can be added later
  only after the simple trade ledger is understood.
- Default the detailed test to the current lead from the corrected sample:
  `dollar_volume_rank`, 50% long/short buckets, 7-day hold/rebalance, 1.0x
  gross, 8% fixed stop, no take-profit cap.
- Add `volume-grid` for lifecycle parameter testing instead of manually running
  one-off backtests. The grid tests no/fixed/volatility stops, rank exits,
  hold/rebalance periods, quantiles, costs, and optional side reversal.
- Use process-level CPU concurrency for grid variants. Do not use the RTX GPU
  until/unless the simulation is rewritten around vectorized CUDA primitives.

## 2026-05-01

- Add the Bybit aggression-carry spec as the first authoritative overhaul
  reference.
- Build phase-one research code in `aggression_carry/` instead of importing the
  old live runtime.
- Use partitioned Parquet, Polars, pyarrow, pybit, and direct Bybit
  archive handling for research data.
