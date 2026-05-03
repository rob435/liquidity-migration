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
- Added the point-in-time universe groundwork for the daily-close fade alpha:
  `archive-manifest` scans Bybit public trading archive symbol/date coverage,
  `archive_klines_1m`/`archive-download-klines` build 1m OHLCV from public trades, and
  `--require-archive-membership` gates candidate selection on archive
  symbol/date membership.
- Fixed daily-close fade short PnL to use Bybit USDT-linear math and added
  `basket_stop_loss_pct` plus gross-exposure grid support. The corrected
  one-year current-universe risk slice invalidated the earlier explosive
  returns. Current candidate: 22:15 UTC, top 5 pump-like shorts, 180m hold, 20%
  per-symbol stop, `0.5x` gross: +42.46%, Sharpe-like 1.39, max drawdown
  -15.26%. Basket stops did not improve enough to become the lead variant.
- Locked the daily-close research default to the best current 20% stop setup:
  22:15 UTC, top 5 pump-like shorts, 180m hold, 20% per-symbol stop. The
  max-return biased one-year read is +86.69% at `1.0x` gross with -28.95% max
  drawdown; `0.5x` gross remains the cleaner risk-scaled comparison.
- Audited the volume-alpha detailed TP/SL logic and added direct tests for
  linear long/short returns, side-aware stops, side-aware take-profits, and
  conservative same-bar stop precedence.
- Removed the stale global `crypto-momentum-system` Codex skill because it
  pointed at an old repo and contradicted this repo's short-capable research
  scope.
- Removed repo-local Codex hooks, AO/Composio helper files, and Python cache
  folders so the repo stays focused on research code, data tooling, docs,
  configs, and tests.
- Added `forward-scan`, `forward-run`, and `forward-report` for daily-close
  fade paper forward testing. The path scans live public Bybit data, writes a
  paper ledger, and supports notification-only Telegram updates without Bybit
  private keys or order submission.
- Added fixed take-profit support to the daily-close fade backtester, grid, CLI,
  config, paper forward tester, and tests. TP is active immediately after entry;
  hard stop/trailing stop can still use `stop_delay_minutes`; when both stop and
  TP are active in the same 1m bar, the stop wins.
- Ran a 320-variant one-year daily-close TP/trailing sweep. Fixed TP did not
  improve raw return. Best raw return stayed no TP/no trailing at +86.69%.
  Better risk-adjusted candidate was no fixed TP plus a 1.5% trailing stop after
  +2% favorable excursion: +79.80%, Sharpe-like 1.80, max drawdown -20.69%.
- Ran focused 3-year volume-alpha TP/SL sweeps across core, mid, and tail
  liquidity buckets. Core and mid still failed. Tail with both side modes found
  `long_high_short_low`, 7d hold, 50% quantile, 12% stop, no TP, no rank exit:
  +120.10%, Sharpe-like 1.09, max drawdown -21.19%. Fixed TP hurt the tail
  bucket, and the earlier 1-year reversed-tail result did not survive the
  3-year side-mode check.
- Added adaptive daily-close exits: volatility-scaled trailing stops,
  MFE-giveback exits, and VWAP-reversion exits. The paper forward tester now
  stores the exit config on each opened paper trade so later config changes do
  not rewrite open-trade exit behavior.
- Ran daily-close exit validation. Fixed TP and VWAP-reversion were rejected
  because they improved hit rate by cutting too much right tail. The simple
  22:15 top-5 pump short lost -60.96% on the three-year current top-160 sample.
  The best current adaptive candidate is no fixed TP, 20% disaster stop,
  `0.25x` daily-vol trail after the 15-minute stop delay, and 20% MFE giveback
  after +1% favorable excursion: +7.31% over 30d, +151.22% over 1y, and
  +330.03% over 3y current top-160. At 3x costs the 3y result turns negative,
  so execution cost is now a gating risk.
- Added dynamic baseline liquidity filtering to the daily-close path. It ranks
  symbols by prior average quote turnover and applies rank buckets at candidate
  selection time, so grids can test top 1-30, 31-80, 81-150, and combined
  ranges without static ignore-list churn. The 31-150 bucket is now the default:
  1y +126.33%, Sharpe-like 4.48, max drawdown -6.48%; 3y current top-160
  +249.03%, Sharpe-like 2.92, max drawdown -8.08%. Top 1-30 was weaker, and
  151-300 failed on the available current-universe data.
- Added daily-close capacity controls and a `daily-close-fade-sleeves` command.
  The sleeve report compares `major_control` ranks 1-30, `core` ranks 31-150,
  and experimental `microcap` rank 151+ separately. Microcap defaults to top 3,
  0.50x gross, turnover floors, and caps actual weight by account equity,
  day-to-date turnover, and prior baseline turnover.

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

- Generated a Graphify code graph for the repo.
- Added `docs/bybit_aggression_carry_system_codex_spec.md`.
- Added the first `aggression_carry/` research package with fixture data,
  Bybit download skeleton, archive parsing, signed-flow aggregation, Parquet
  storage, alpha reports, and costed portfolio tests.
- Fixed Bybit REST pagination and archive handling.
- Added Windows setup and 3-month run scripts.
