# Journal

## 2026-05-03

- Added current Bybit universe discovery from instruments/tickers with filters
  for USDT linear perps, trading status, prelisting exclusion, listing age,
  current 24h turnover, liquidity rank, and symbol exclusions.
- Added daily dynamic liquidity-rank filters to the detailed backtest and grid
  so broad downloaded universes can be split into core/mid/tail buckets without
  hand-editing symbol lists.
- Added `scripts/run_volume_bucket_sweep.py` to run core/mid/tail grids from a
  real Python file, avoiding macOS multiprocessing issues with stdin snippets.
- Ran a current top-150 Bybit one-year broad-universe pass:
  - core ranks 1-20: best +67.86%, Sharpe-like 1.14, `long_high_short_low`
  - mid ranks 21-80: best +38.92%, Sharpe-like 1.30, `short_high_long_low`
  - tail ranks 81-150: best +154.10%, Sharpe-like 2.08, `short_high_long_low`
- Reran the tail best row as a full trade-ledger report: 528 trades, +154.10%,
  Sharpe-like 2.08, max drawdown -16.97%.

## 2026-05-02

- Added an isolated daily volume-alpha research path.
- Ran the corrected 3-month Bybit sample.
- Found that increasing-volume variants failed, while `dollar_volume_rank` was
  the only useful lead.
- Officially stripped the repo down around the single-alpha rebuild:
  - removed old live runtime files
  - removed old root backtest/replay/report/runtime modules
  - removed old composite aggression/carry/momentum/quality/OI modules
  - removed tests that only protected deleted behavior
  - simplified config, CLI, docs, and Windows runner around `volume-alpha`
- Added `volume-backtest`, a detailed trade-ledger backtester for the isolated
  volume alpha. It records entries, exits, exit reasons, fixed stops, costs,
  MAE/MFE, basket returns, equity, and attribution so strategy tweaks can be
  judged from actual trades rather than only forward-return spreads.
- Added `volume-grid` with process-pool concurrency for one-year lifecycle
  sweeps. It tests no/fixed/volatility stops, rank exits, hold/rebalance
  periods, quantiles, and optional side reversal, with a Windows one-year runner.

## 2026-05-01

- Installed and documented Codex companion tools: Composio skills, Caveman
  skills, Graphify, AO, Composio CLI, GitHub CLI, and tmux.
- Generated a Graphify code graph for the repo.
- Added `docs/bybit_aggression_carry_system_codex_spec.md`.
- Added the first `aggression_carry/` research package with fixture data,
  Bybit download skeleton, archive parsing, signed-flow aggregation, Parquet
  storage, alpha reports, and costed portfolio tests.
- Fixed Bybit REST pagination and archive handling.
- Added Windows setup and 3-month run scripts.
