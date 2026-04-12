# MODEL050426

Real-time crypto momentum trader built around Bybit V5 candle streams, intrabar `entry_ready` selection, residual momentum ranking, and research-grade backtesting.

## Canonical Spec

The current source-of-truth system specification is [SPEC.md](SPEC.md).
Use that document for the actual runtime contract. This README is now an operator overview.

## What it does

- Bootstraps 15m history for a manual universe of midcap USDT contracts plus `BTCUSDT`.
- Subscribes to Bybit public `kline.15` WebSocket updates and processes both intrabar updates and confirmed candle closes.
- Recomputes cross-sectional scores on intrabar `emerging` cycles off provisional current-candle prices.
- Uses a simple intrabar state ladder for live decisions:
  - `WATCHLIST`: ticker enters the broad intrabar leaders
  - `EMERGING`: ticker is strengthening across recent intrabar observations
  - `ENTRY_READY`: the only tradeable signal tier
- Uses a short settle delay before each processing cycle so ranking runs on a mostly complete 15m snapshot instead of the first symbol that arrives.
- Throttles `emerging` processing with a minimum interval so intrabar updates stay event-driven without recalculating on every raw WebSocket packet.
- Uses BTC daily regime as a threshold modulator, not a hard block.
- Uses a separate intraday tradeability gate on top of the BTC macro score so `entry_ready` is blocked on weak breadth / low-efficiency / anti-momentum sessions instead of pretending every day is a momentum day.
- Uses residual momentum by default (`MOMENTUM_REFERENCE_MODE=cluster_relative`) so the ranking is less likely to collapse into raw alt beta.
- Supports `absolute`, `btc_relative`, `basket_relative`, `cluster_relative`, and `hybrid_relative` momentum-reference modes.
- Uses dynamic correlation clusters by default, so both residual momentum and the cluster-exposure cap can react to recent co-movement instead of only a static hand-maintained map.
- Uses Binance BTCDOM futures history as the dominance rotation signal, normalized into `falling / neutral / rising` with a `+-0.2%` neutral zone.
- Logs every evaluated ticker on each cycle to SQLite and optionally sends Telegram alerts with cooldown control.
- Logs the top-ranked names each cycle so you can see the leaders even when no symbol passes the final alert filters.
- Keeps provisional intrabar prices isolated from the confirmed 15m history so early alerts do not contaminate the closed-bar history used for the next intrabar cycle.
- Executes the current signal stack as a momentum long strategy: buy to open, sell to exit, TP at `2%` up, SL at `2%` down.
- Caps correlated pile-up with cluster-level exposure control as well as raw position count.
- Persists analytics for closed trades, open-position marks, post-exit follow-through, portfolio snapshots, and richer entry diagnostics so TP/SL and capacity can be studied from real runtime data instead of screenshots.

## Current Position

- `EMA200` requires more than 30 daily BTC closes, so the runtime stores 220 BTC daily closes.
- BTCDOM is implemented through Binance futures history as a practical public proxy, not literal spot market-cap dominance.

## Next Tuning Pass

The current tuning priorities are:

- Validate the simpler `entry_ready`-only live path before making it more complex again.
- Tune residual momentum, the intraday regime gate, and entry selectivity before touching exits.
- Use walk-forward and reconciliation tooling instead of trusting pretty in-sample runs.

## Layout

- `config.py`: environment-driven settings
- `universe.py`: manual universe list
- `state.py`: in-memory rolling state
- `indicators.py`: pure numeric routines
- `exchange.py`: Bybit REST bootstrap + WebSocket ingestion
- `signal_engine.py`: ranking, signal generation, cooldown, logging, alert dispatch
- `database.py`: SQLite persistence
- `alerting.py`: Telegram output
- `runtime_validation.py`: startup config checks for the live service
- `runtime_monitor.py`: run manifests, health snapshots, and drift monitoring
- `monitor.py`: read-only CLI for recent runtime manifests, snapshots, and events
- `main.py`: process wiring and supervision

## Run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp deploy/production.env.example .env
python main.py
```

For a local research machine, do not copy the VPS-oriented template verbatim unless you enjoy debugging `/opt/...` path garbage later. Generate a local-safe `.env` instead:

```bash
python deploy/prepare_local_env.py --output .env
```

That localizes the persistent paths and disables live order submission by default.

On Windows, use the bundled bootstrap:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\setup_windows.ps1
```

That script creates `.venv`, installs requirements, writes a localized research `.env`, optionally restores a cache bundle, and runs `pytest -q`.

## Test

```bash
pytest
```

## Live Runtime

Before the live service starts, it now runs explicit config validation instead of blindly trusting `.env`.

The runtime also writes a lightweight control plane into SQLite:

- `run_manifests`: one row per service run with git commit and config fingerprint
- `runtime_health_snapshots`: periodic liveness / queue / position summaries
- `runtime_events`: startup, shutdown, control-block, and drift events

Inspect that data with:

```bash
python monitor.py --db signals.sqlite3
```

## Replay

Use the replay harness to drive the production engine over recent Bybit history without waiting days for live validation:

```bash
python replay.py --cycles 96 --db replay-signals.sqlite3
```

This warms state with the first `STATE_WINDOW` candles, then replays the remaining cycles in market-event time.

## Backtest

Use the backtest harness to drive the live signal engine over historical candles and compare filter-on vs filter-off behavior on the same sample:

```bash
python backtest.py --cycles 192 --db backtest.sqlite3 --compare-intraday-regime-filter
```

The default mode is a minute-aware intrabar replay:

- it warms state with real closed 15m history
- it replays each future 15m bar minute by minute using historical `1m` OHLC
- `emerging` / `entry_ready` logic sees a provisional 15m close that evolves through the bar
- existing positions can hit TP/SL on historical minute highs and lows before the next entry decision
- portfolio sizing is equity-based, with configurable fees, slippage, and gross exposure caps

The fallback mode is still available if you only want a lightweight plumbing check:

```bash
python backtest.py --mode close-proxy --cycles 192 --db backtest.sqlite3
```

The minute-aware mode is materially more honest, but it is still a candle replay, not order-book reconstruction. Same-minute TP/SL conflicts are resolved conservatively, fees and slippage are model assumptions, and live fill quality can still differ.

For longer research, use sweep mode to sample repeated windows through time and compare the intraday regime filter on vs off:

```bash
python backtest.py \
  --cycles 96 \
  --sweep-lookback-days 365 \
  --sweep-step-days 90 \
  --compare-intraday-regime-filter \
  --export-dir ./backtest-sweep
```

That runs multiple disjoint minute-aware windows, prints which side won each window, and exports sweep summaries plus CSVs. Older windows may be skipped if parts of the current universe were not listed yet.

For repeated research, warm the on-disk historical candle cache first:

```bash
python backtest.py --prefetch-lookback-days 365 --prefetch-end-date 2026-04-10
```

That downloads and stores the current universe's required `15m`, intrabar, BTC daily, and BTCDOM series into `BACKTEST_CACHE_PATH`. Brutally honest caveat: a full year of `1m` candles across the whole universe is large. It saves repeated network time, but it will consume real disk space.

If you already warmed the cache on another machine, transfer it instead of redownloading it:

```bash
python deploy/cache_bundle.py pack \
  --source .cache/backtest_candles.sqlite3 \
  --archive ./backtest_candles.sqlite3.gz
```

Then restore it on the target machine:

```bash
python deploy/cache_bundle.py unpack \
  --archive ./backtest_candles.sqlite3.gz \
  --destination .cache/backtest_candles.sqlite3
```

Inspect a restored cache with:

```bash
python deploy/cache_bundle.py inspect --path .cache/backtest_candles.sqlite3
```

For wide parameter sweeps, use the explicit fast-research mode:

```bash
python backtest.py --cycles 96 --research-fast --compare-intraday-regime-filter
```

That keeps the trade and equity simulation intact but skips signal-row persistence and report SQL so repeated sweeps spend less time on audit-grade bookkeeping. Use full mode again before trusting a shortlisted configuration.

For generic variable sweeps, use `--grid-setting`. This fetches and builds one replay plan, then runs every variant against that same in-memory history:

```bash
python backtest.py \
  --cycles 96 \
  --research-fast \
  --variant-workers 4 \
  --grid-setting hurst_cutoff=0.45,0.50,0.55 \
  --grid-setting take_profit_pct=0.02,0.03
```

That is the plan-reuse path. The market history is fetched once, the replay plan is built once, and only the settings vary between runs. Brutally honest caveat: this removes repeated setup churn, but each variant still pays for its own full replay loop.

`--variant-workers` runs those variants in separate worker processes. Use it on real research hardware, not on the same small VPS that is running live trading.

For long runs, pin the horizon explicitly so the warmed cache and the backtest are talking about the same window:

```bash
python backtest.py \
  --cycles 35040 \
  --end-date 2026-04-11 \
  --variant-workers 4 \
  --grid-setting momentum_reference_mode=basket_relative,cluster_relative,hybrid_relative \
  --grid-setting cluster_assignment_mode=dynamic,hybrid
```

The runner now prints blunt phase messages and cache/network stats while it works, so you can tell whether it is fetching history, building the replay plan, running variants, or exporting. Single-run comprehensive backtests now also emit replay progress lines with a text progress bar, completed bar count, elapsed time, and ETA during the actual simulation loop. Variant runs emit per-variant completion lines with the completed count, per-variant runtime, total elapsed time, average variant time, and an ETA for the remaining pending variants.

Use `./backtest-runs/` for local research outputs instead of dumping huge SQLite and CSV artifacts into the repo root. That directory is kept in the repo as a tracked placeholder, but its contents are ignored by git.

On memory-constrained machines, the grid runner may also reduce the requested worker count after writing the replay snapshot. That is deliberate. A huge annual replay plan plus too many worker processes is how you get a late `MemoryError` and waste a day.

For interrupted long grids, use checkpoint/resume:

```bash
python backtest.py \
  --cycles 35040 \
  --end-date 2026-04-11 \
  --variant-workers 4 \
  --export-dir ./backtest-runs/year-grid \
  --resume-variants \
  --grid-setting hurst_cutoff=0.50,0.55,0.60 \
  --grid-setting intraday_regime_min_pass_count=2,3
```

That resumes from `./backtest-runs/year-grid/variant_summary.csv` and skips variants that already completed.

For built-in stress testing, add one or more `--stress-profile` flags:

```bash
python backtest.py \
  --cycles 96 \
  --research-fast \
  --stress-profile costly \
  --stress-profile hostile
```

Available profiles are:

- `costly`: worse fees and slippage
- `liquidity_crunch`: worse costs plus tighter exposure assumptions
- `hostile`: the harshest bundled cost/liquidity stress

For the actual optimization workflow, read [BACKTEST_RESEARCH_PLAN.md](/Users/jhbvdnsbkvnsd/Desktop/MODEL050426/BACKTEST_RESEARCH_PLAN.md). That document is the repo's explicit research discipline for parameter pruning, interaction checks, TP/SL tuning, and final validation.

For walk-forward validation on shortlisted variants:

```bash
python backtest.py \
  --walk-forward-lookback-days 365 \
  --walk-forward-train-days 90 \
  --walk-forward-test-days 30 \
  --research-fast \
  --variant-workers 4 \
  --grid-setting hurst_cutoff=0.50,0.55,0.60
```

Walk-forward exports now also include `walk_forward_candidates.csv`, so the full train-window leaderboard is preserved instead of only the final selected variant.

For live-vs-backtest reconciliation against a Telegram export plus `backtest_trades.csv`:

```bash
python reconcile.py \
  --telegram-html "/path/to/messages.html" \
  --backtest-trades-csv ./exports/backtest_trades.csv \
  --tolerance-minutes 30
```

The reconciliation output now includes:

- entry and exit precision / recall
- average timestamp deltas
- exit-reason agreement
- unique-ticker precision / recall
- `ticker_reconciliation.csv` for per-ticker mismatch diagnosis
- `matched_entries.csv` and `matched_exits.csv` alongside the unmatched rows

If you already want a normal exported backtest, `backtest.py` can run the reconciliation step directly:

```bash
python backtest.py \
  --cycles 35040 \
  --end-date 2026-04-11 \
  --export-dir ./backtest-runs/year-backtest \
  --reconcile-telegram-html ./messages.html
```

For grid runs, the built-in reconciliation targets the current best variant and writes the result under `best_variant_reconciliation/`.

For daily forward testing, use the shipped wrapper instead of assembling the command by hand:

```bash
./deploy/run_daily_forward_reconcile.sh
```

That defaults to:

- previous UTC day
- `signals.sqlite3` as both the live trade source and reconciliation log DB
- `96` cycles
- exports under `./reconciliation-daily/YYYY-MM-DD/`

You can also pin a specific UTC day:

```bash
./deploy/run_daily_forward_reconcile.sh 2026-04-11
```

This is the sane cron/systemd entrypoint for end-of-day forward-vs-backtest checking. It does not touch the live trading loop.

## Universe Validation

Validate the configured universe against Bybit’s live instruments list before a deployment:

```bash
python universe_validator.py
```

This uses Bybit `GET /v5/market/instruments-info` with pagination because `linear` instruments exceed the default 500-row response.

## Reporting

Summarize a replay or live SQLite log without hand-writing SQL:

```bash
python report.py --db replay-signals.sqlite3 --top 10
```

The report includes a stage and signal-kind breakdown so you can see how much of the log came from `watchlist`, `emerging`, `entry_ready`, and inert non-trade rows.

It also summarizes:

- daily trade results with wins, losses, stop-loss count, take-profit count, and net PnL
- closed-trade analytics such as holding time, MFE, MAE, post-exit follow-through, volatility, and preserved entry diagnostics in `notes`
- portfolio snapshots with open-position count, gross notional, balance-based capacity estimates, and daily stop-loss totals

Export CSVs for VPS retrieval:

```bash
python report.py --db signals.sqlite3 --export-dir exports
```

## VPS Sync

Use the VPS as the source of truth and pull analytics exports back to this Mac on a schedule.

The intended flow is:

1. A local script at `deploy/sync_exports.sh` creates a safe SQLite backup on the VPS at `root@204.168.202.167`.
2. The script pulls the backup down to the Mac and also captures the latest `systemctl status` and `journalctl` tail.
3. The same sync also pulls the VPS `reconciliation-daily/` tree so forward-vs-backtest daily checks do not stay stranded on the server.
4. `report.py --export-dir ...` then regenerates local CSVs for analysis from the pulled SQLite copy.

The shipped launchd template is [deploy/model050426-export-sync.plist](deploy/model050426-export-sync.plist). It runs once per day at `09:00` local time and writes logs to `~/Library/Logs/model050426-export-sync.log` and `~/Library/Logs/model050426-export-sync.err`.

Use the installer on macOS instead of pointing launchd at the repo checkout directly. A repo living under `~/Desktop` is TCC-protected, and launchd can fail with `Operation not permitted`.

Run one sync manually:

```bash
./deploy/sync_exports.sh
```

Default local output paths:

- SQLite backup: `~/MODEL050426-sync/db/signals.sqlite3`
- CSV exports: `~/MODEL050426-sync/exports/latest/`
- daily reconciliation exports: `~/MODEL050426-sync/reconciliation-daily/`
- rendered report text: `~/MODEL050426-sync/reports/latest.txt`
- remote service status: `~/MODEL050426-sync/logs/vps-status.txt`
- remote journal tail: `~/MODEL050426-sync/logs/journal-tail.log`

Install the launch agent and copy the sync assets to `~/Library/Application Support/MODEL050426-sync/`:

```bash
./deploy/install_export_sync.sh
```

If you want a different daily time, set it before install:

```bash
MODEL050426_SYNC_HOUR=6 MODEL050426_SYNC_MINUTE=30 ./deploy/install_export_sync.sh
```

## Smoke Run

Run universe validation plus a short live-data replay in one command:

```bash
python smoke.py --cycles 4 --db smoke-replay.sqlite3 --strict-universe
```

## Benchmark

Measure signal-engine cycle latency against synthetic data:

```bash
python benchmark.py --tickers 100 --cycles 20
```

On this machine, the current engine path measured about `36ms` average for `100` synthetic tickers including SQLite writes, which is inside the `<100ms` target.

## Bounded Live Run

Run the actual service for a fixed time window with Telegram disabled:

```bash
python main.py --run-seconds 300 --disable-telegram
```

This is the honest soak-test path for startup, WebSocket stability, macro refresh, queue handling, and clean shutdown. The process logs a runtime summary on exit.

## Deployment

`deploy/model050426.service` is the shipped `systemd` unit for a checkout at `/opt/MODEL05042026`.

For the first VPS validation run, use [SOAK_RUN.md](deploy/SOAK_RUN.md) and start from [production.env.example](deploy/production.env.example).

## Operational notes

- Bybit REST `GET /v5/market/kline` returns candles in reverse chronological order and includes the still-open candle; the bootstrapper normalizes to ascending closed candles only.
- Bootstrap is the heaviest REST phase. Repeated local restarts can hit Bybit `retCode=10006`; the client now backs off and retries automatically.
- The WebSocket connection will drop eventually. The supervisor reconnects, reboots state through REST, and resumes the stream.
- Intrabar cycles will fire repeatedly during an open 15m candle whenever Bybit pushes provisional kline updates. That is intentional; alert sends are transition-gated into `WATCHLIST` and `EMERGING` states instead of firing on every pass.
- `EMERGING` requires strengthening, not just presence. The intrabar state machine looks for repeated watchlist observations with improving rank and rising composite score before promoting a ticker from `WATCHLIST` to `EMERGING`.
- `ENTRY_READY` is the only tradeable signal kind. It is the trader-oriented label for the strongest intrabar candidate and should be read as "this is strong enough to trade now."
- `ENTRY_READY` has explicit tuning knobs in the env templates: `ENTRY_READY_TOP_N`, `ENTRY_READY_COOLDOWN_MINUTES`, `ENTRY_READY_MIN_OBSERVATIONS`, `ENTRY_READY_MIN_RANK_IMPROVEMENT`, and `ENTRY_READY_MIN_COMPOSITE_GAIN`. Use those to keep the live entry tier tighter than `EMERGING` without adding another fake confirmation stage.
- Execution is currently aligned with the momentum ranking again. The bot buys the strongest `entry_ready` names, exits them at `+2%` take profit or `-2%` stop loss by default, and can now ratchet one still-strong winner outward with `PROFIT_PROTECTION_*` instead of TP-ing and immediately re-entering.
- The anti-churn layer now has explicit operator knobs too: `REENTRY_COOLDOWN_AFTER_PROFIT_MINUTES` blocks immediate same-ticker re-entry after a profitable close, `MAX_TICKER_LOSING_TRADES_PER_DAY` cuts off repeat losers on the same name for the rest of the UTC day, and `STALE_WINNER_*` lets a profit-protected winner exit cleanly once the strength signal has faded instead of lingering indefinitely.
- Trade analytics are richer now on exits. The trade ledger records exit-time rank/composite/momentum/curvature/Hurst plus the active TP/SL levels and profit-protection adjustment count, so later tuning can use actual exit-state evidence instead of guessing from entry-only data.
- `INTRADAY_REGIME_*` is now the practical no-trade layer. It only blocks `entry_ready` promotion; the engine still logs broad intrabar context on bad days so you can see what it wanted to do without actually entering.
- `MAX_OPEN_POSITIONS` is back as a blunt portfolio safety net. It caps the total number of simultaneously open positions, while `MAX_ENTRIES_PER_REBALANCE` separately caps how many new names one rebalance pass may add.
- `MAX_POSITIONS_PER_CLUSTER` is the more useful anti-pileup control. It stops the book from filling with multiple names from the same current cluster even when raw position count still looks safe.
- `MAX_ENTRIES_PER_REBALANCE` is now the clean way to cap how many fresh positions one `emerging` rebalance pass may open. It does not change ranking; it just limits how many top-ranked `entry_ready` names are allowed through in that batch.
- `TELEGRAM_SIGNAL_ALERTS_ENABLED=false` is the clean execution-only Telegram mode. With that off, chat can be reduced to entry/exit execution messages only.
- `OPERATOR_PAUSE_NEW_ENTRIES=true` is the blunt manual brake. The bot keeps running, logging, and snapshotting, but it will stop opening new positions.
- `monitor.py` is the fast way to check whether the service is actually alive, drifting from venue state, or repeatedly blocking entries for control reasons without reading raw logs.
- If a candle gap is detected mid-stream, the supervisor falls back to a fresh REST bootstrap instead of pretending state is intact.
- Cooldown only advances after a successful alert send; a failed Telegram request does not silently suppress the next valid signal.
- The confirmed-cycle consumer still exists because closed bars still need to advance state cleanly. It no longer emits tradeable or operator-facing confirmed tiers.
- Analytics env knobs now include `MAX_DAILY_STOP_LOSSES`, `ANALYTICS_POST_EXIT_BARS`, `ANALYTICS_LOG_POSITION_MARKS`, and `ANALYTICS_LOG_PORTFOLIO_SNAPSHOTS`.
- Runtime control knobs now include `RUNTIME_HEALTH_SNAPSHOT_*` and `RUNTIME_DRIFT_CHECK_*`, which govern the SQLite control-plane cadence rather than alpha logic.
- Execution throttles now include `MAX_ENTRIES_PER_REBALANCE` for per-batch entry control and `MAX_DAILY_STOP_LOSSES` for the UTC-day kill switch.
- For VPS analysis, prefer pulling the SQLite backup to the Mac and regenerating CSVs locally rather than grepping raw logs. Example:

```bash
./deploy/sync_exports.sh
python report.py --db ~/MODEL050426-sync/db/signals.sqlite3 --export-dir ~/MODEL050426-sync/exports/latest
```

- Raw `signals.csv` export is now opt-in because it gets huge fast. If you want it anyway, add `--include-signals-export`.
- The runtime uses the `certifi` CA bundle for outbound TLS, which avoids common macOS Python certificate-store breakage.
- If you run from a US-routed host, Bybit mainnet access may be blocked. Use a compliant region or testnet/base URL override.

## Docs used

- Bybit Get Kline: https://bybit-exchange.github.io/docs/v5/market/kline
- Bybit WebSocket Connect: https://bybit-exchange.github.io/docs/v5/ws/connect
