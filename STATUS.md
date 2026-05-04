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
- Point-in-time universe groundwork now exists: `archive-manifest` scans Bybit's
  public trading archive into symbol/date membership, and `archive_klines_1m`
  plus `archive-download-klines` can build 1m bars directly from public trade
  archives.
- The old live runtime and old blended signal stack have been deleted.
- Repo-local Codex hooks and AO/Composio helper installer files have been
  deleted.
- Paper forward testing is implemented for the daily-close fade path via
  `forward-scan`, `forward-run`, and `forward-report`. It uses public Bybit
  data only and writes a paper ledger.
- Bybit demo order plumbing is isolated in `bybit-demo-probe` and
  `bybit-demo-sync`. These commands are hard-capped demo-only paths with their
  own reports/ledger; they are not called by the paper forward runner.
- Bybit demo shadow orchestration targets `bybit-demo-cycle`: it runs all paper
  sleeves, then mirrors each sleeve into a separate capped Bybit demo ledger.
  The cycle has a process lock, a default `22:05-02:30 UTC` active window,
  pause-file support, and demo-only emergency cancel/flatten commands. This
  remains demo-only and real-money execution is not implemented.
- Daily-close fade now has a three-sleeve comparison path:
  `major_control` ranks 1-30, `core` ranks 31-150, and experimental `microcap`
  ranks 151+ with turnover floors and capacity-limited sizing.
- No real-money live execution, kill switches, or production exchange order
  submission exist in the active code. Telegram is notification-only for paper
  and demo-status reporting.

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
- Daily-close fade current-universe results are promising only with adaptive
  exits; final proof still requires the archive-based walk-forward path in
  `docs/walk_forward_universe.md`.
- Corrected daily-close short PnL to USDT-linear math. The simple 22:15 UTC
  top-5 pump short with 180m hold, 20% stop, no fixed TP, and no adaptive exit
  is not confirmed: +86.69% on the biased one-year current-universe sample but
  -60.96% on the three-year current top-160 sample.
- Current daily-close lead: no fixed TP, baseline liquidity ranks 31-150, 20%
  disaster stop, `0.25x` daily-vol trailing stop active after the 15-minute
  stop delay, and 20% MFE giveback after a +1% favorable move. The liquidity
  filter uses prior 7-day average quote turnover, not same-day pump volume.
  Current-universe reads for 31-150: +126.33% over 1y and +249.03% over 3y
  current top-160, with 3y max drawdown -8.08%.
- Rank 151+ is now treated as a separate microcap sleeve, not a blind extension
  of the core book. The first implementation requires prior baseline turnover,
  day-to-date turnover, last-hour turnover, and position-size caps tied to a
  configured account equity assumption.
- Latest exit sweeps say fixed take-profits are not the answer yet. Daily-close
  fixed TP and VWAP-reversion exits cut too much right tail; volume-alpha fixed
  TP hurt the 3-year tail bucket. Daily-close adaptive exits are cost-sensitive:
  the 31-150 3y current top-160 bucket is +249.03% at base costs, +50.51% at
  2x costs, and -35.15% at 3x costs.
- The 3-year current-universe volume tail bucket now points to
  `long_high_short_low`, 7d hold, 50% quantile, 12% stop, no TP, no rank exit:
  +120.10%, Sharpe-like 1.09, max drawdown -21.19%. The earlier 1-year
  `short_high_long_low` tail reversal did not survive the 3-year side-mode
  check.

## Remaining Risks

- Three months and 16 symbols is not enough evidence.
- Current-universe discovery is survivorship-biased until delisted/dead
  contracts are incorporated.
- Current top-160 daily-close fade tests are explicitly biased benchmarks until
  the archive manifest and archive-derived 1m bars cover every eligible
  symbol/date.
- Earlier daily-close fade reports from before the USDT-linear short PnL fix
  are invalid and should not be used for decisions.
- Bybit-only data may miss the venue where price discovery actually happens.
- Dollar-volume rank may be a hidden size/liquidity effect, not the podcast's
  claimed increasing-volume alpha.
- Funding, borrow constraints, squeezes, and live execution are intentionally out
  of scope until the standalone alpha is proven.
- Demo execution proves authentication/order plumbing only. It does not validate
  the alpha, and it can still produce unrealistic fills compared with real
  liquidity.
- Demo shadowing can diverge from paper when post-only entries miss, fills are
  partial, or Bybit demo liquidity differs from real markets. Use the demo
  ledger as execution plumbing evidence, not performance proof.
- Overlapping baskets are intentionally blocked for now; hold period and
  rebalance period should match until the simple lifecycle is understood.
- GPU acceleration is intentionally not implemented; this workload is currently
  CPU/process-parallel Python simulation, not a vectorized CUDA pipeline.
