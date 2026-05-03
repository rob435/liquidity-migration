# Status

## Current

- The repo is now a stripped-down research lab, not a live trading bot.
- Current implementation plan: `docs/volume_alpha.md`.
- Bybit venue/data reference: `docs/bybit_aggression_carry_system_codex_spec.md`.
- The active alpha paths are `volume-alpha` for statistical sweeps,
  `volume-backtest` for trade-ledger testing, and `volume-grid` for concurrent
  lifecycle parameter grids.
- Universe tooling now exists: `discover-universe` builds current Bybit symbol
  snapshots and the backtester/grid can filter daily liquidity-rank buckets.
- The old live runtime and old blended signal stack have been deleted.
- No live execution, Telegram alerts, kill switches, production deployment, or
  exchange order submission exist in the active code.

## Active Path

- Download Bybit public `instruments` and `klines_1h`.
- Build daily volume-only features from 1h quote turnover.
- Test 1d, 3d, and 7d forward returns.
- Run long/short costed portfolios at configured quantiles.
- Write `volume_alpha_report.md`, JSON report, feature Parquet, metrics Parquet,
  and portfolio Parquet.
- Run a detailed `dollar_volume_rank` trade backtest with entry timestamps,
  exit timestamps, fixed stops, costs, exit reasons, basket returns, equity, and
  symbol/month attribution.
- Write `volume_backtest_report.md`, `volume_backtest_trades.csv`, and Parquet
  datasets for trades, baskets, and equity.
- Run concurrent process-pool grids over hold period, quantile, fixed/no/vol
  stops, rank exits, side reversal, take profits, and cost multipliers.
- Write `volume_grid_report.md`, `volume_grid_results.csv`, and
  `volume_backtest_grid`.
- Run bucket grids with `scripts/run_volume_bucket_sweep.py` for core ranks
  1-20, mid ranks 21-80, and tail ranks 81-150.

## Current Research Read

- Increasing-volume variants failed the corrected 3-month sample.
- `dollar_volume_rank` is the only promising lead so far.
- The 3-year 16-symbol result is positive and balanced enough to keep studying,
  but it still has survivorship and beta risks.
- The first one-year current top-150 bucket sweep found a stronger tail-liquid
  result with the direction reversed: ranks 81-150, `short_high_long_low`,
  14-day hold, 20% buckets, no rank exit, +154.10%, Sharpe-like 2.08, max
  drawdown -16.97%.
- That tail result is a separate lead, not confirmation that the original
  high-volume-long rule simply gets stronger in lower caps.
- The detailed backtester now exists to test whether that lead survives actual
  trade lifecycle assumptions instead of only forward-return buckets.
- The next intended validation is a longer broad-universe run with point-in-time
  universe improvements, plus funding/spread/impact stress on the tail bucket.

## Remaining Risks

- Three months and 16 symbols is not enough evidence.
- Current-universe discovery is survivorship-biased until delisted/dead
  contracts are incorporated.
- Bybit-only data may miss the venue where price discovery actually happens.
- Dollar-volume rank may be a hidden size/liquidity effect, not the podcast's
  claimed increasing-volume alpha.
- Funding, borrow constraints, squeezes, and live execution are intentionally out
  of scope until the standalone alpha is proven.
- Overlapping baskets are intentionally blocked for now; hold period and
  rebalance period should match until the simple lifecycle is understood.
- GPU acceleration is intentionally not implemented; this workload is currently
  CPU/process-parallel Python simulation, not a vectorized CUDA pipeline.
