# Liquidity-Migration Strategy Plan

This repo is now focused on the event-driven full-PIT liquidity-migration
strategy. The old fixed daily-close short fade is retired, and fixed-day
volume rebalance sweeps are legacy benchmarks only.

## Objective

Turn the selected Bybit perp liquidity-migration short strategy into a
demo-account system with reproducible full-PIT backtests, clear reports, and
event-lifecycle execution parity.

## Decision

The current priority is event-driven volume entries:

```text
enter only when a symbol has a fresh volume event
exit when the event decays, reverses, risk is hit, or max hold expires
compare against a fixed-rebalance benchmark only as a control
```

Do not promote headline backtest returns from current-universe tests. Those are
biased benchmarks unless tradable membership is point-in-time.

`volume-events` now requires full PIT universe coverage by default. It should
raise before running if `archive_trade_manifest` is missing or if `klines_1h`
does not cover every manifest symbol. Use `--allow-partial-pit` only for an
explicitly biased diagnostic, not for real research decisions.

A "top 150" universe is acceptable only when it is point-in-time, for example
running on the completed full-PIT root with `--universe-rank-max 150`. That
means top 150 by historical daily liquidity rank on each signal date, not the
top 150 coins by today's turnover.

The default tradable universe excludes only stable/peg perps, including failed
peg remnants such as `USTCUSDT`, before ranks/features are built. Do not
manually blacklist top coins such as BTC, ETH, SOL, BNB, XRP, or TRX unless a
fresh full-PIT run proves the blacklist improves the promoted objective.

## Retired

These workflows are intentionally removed from the active research path:

```text
fixed daily-close short fade
old daily candidate scan and sleeve runner
old forward paper/demo executor
fixed 7d/14d split-grid helper scripts
liquidity bucket sweep helper scripts
5950X overnight fixed-grid runner
old "run every combination overnight" workflow
```

The legacy Python entrypoints are removed. The active shared code is now:

```text
liquidity_migration/volume_features.py
liquidity_migration/trade_lifecycle.py
```

`volume_features.py` builds the causal daily liquidity/volume features.
`trade_lifecycle.py` contains the event strategy's exit, basket, and equity
helpers. The old fixed-rebalance alpha/grid backtest APIs are no longer part of
the repo surface.

## Event Definitions

First implementation should test these event families separately:

```text
fresh_volume_spike:
  volume_change_1d crosses into top 20% or 30%
  previous day was outside that bucket

persistent_volume_breakout:
  volume_persistence crosses into top 20% or 30%
  persistence was below median within the last 3 days

tail_liquidity_jump:
  symbol is in liquidity ranks 81-160
  dollar_volume_rank improves sharply versus its 7d baseline
  exclude symbols without point-in-time tradability proof

volume_exhaustion:
  extreme volume spike after strong price extension
  test both continuation and reversal sides separately

volume_absorption:
  volume spikes while same-day price movement stays muted
  tests hidden absorption before a delayed continuation/reversal

dryup_reacceleration:
  volume re-accelerates after a low-persistence, quiet-price regime
  tests coil-break behavior without forcing calendar rebalances

liquidity_migration:
  dollar-volume rank jumps sharply across the whole PIT universe
  unlike tail_liquidity_jump, this is not restricted to ranks 81-160

selloff_exhaustion:
  extreme volume spike after a strong negative same-day move
  tests panic continuation versus snapback separately
```

## Trade Lifecycle

Each event-driven test needs a full trade ledger:

```text
signal timestamp:
  daily close after all inputs are known

entry:
  next available 1h bar after signal close, or configured causal entry router
  no duplicate entry while symbol already has exposure

side:
  test continuation and reversal as separate hypotheses

exit:
  event decay below threshold
  rank reversal
  fixed or volatility stop
  max hold of 3, 7, or 14 days
  optional cooldown before re-entry

risk:
  cap active symbols
  cap gross exposure
  account for fees, slippage, and funding
```

## Required Gates

A candidate is not demo-ready unless it passes:

```text
point-in-time symbol membership
no current-universe survivorship dependency
positive train, validation, and OOS splits
worst split return >= 0
worst drawdown no worse than -25%
average Sharpe-like >= 0.5
cost multipliers of 1x and 3x reported
trade ledger, equity curve, monthly table, and failure reasons saved
equity chart with BTC overlay, monthly/growth gridlines, and a monthly
performance table
```

## Implementation Order

1. Add an event table builder:

```text
symbol
signal_ts_ms
event_type
score
side_hypothesis
prior_rank
current_rank
liquidity_rank
tradable_membership_flag
```

2. Add an event-driven ledger backtester:

```text
one row per actual event-triggered trade
no calendar-forced basket if no event fired
cooldown and max-active-symbol logic included
```

Current command, using the selected strategy defaults:

```bash
python -m liquidity_migration \
  --data-root DATA_ROOT \
  --config configs/volume_alpha.default.yaml \
  volume-events
```

After any serious run, audit the report with the model court:

```bash
python -m liquidity_migration \
  --data-root DATA_ROOT \
  strategy-tribunal \
  --report-dir DATA_ROOT/reports/volume_event_research \
  --comparison-csv DATA_ROOT/reports/stress_summary.csv \
  --comparison-family promoted_funding \
  --pre-registered-window train:2023-05-03:2024-05-03,validation:2024-05-03:2025-05-03,oos:2025-05-03:2026-05-03 \
  --execution-data-root DATA_ROOT
```

`strategy-tribunal` is the formal adversarial model court for this repo. It
does not search for a better parameter; it tries to falsify a candidate by
checking report artifacts, promotion gates, explicit preregistered test
windows, recomputed-vs-reported path consistency, bootstrap left tails,
random-sign, inverted-edge, shuffled-symbol, shuffled-time, and shuffled-event
negative controls, pairwise parameter heatmap CSVs, stress matrices filtered by
`--comparison-family`, cost/funding/slippage diagnostics, monthly regime
coverage, live-vs-backtest execution drift, symbol concentration, and same-hour
entry crowding. A strategy with a beautiful equity curve but one-row evidence,
bad negative controls, missing execution-drift evidence, stale/mixed stress
evidence, or clustered loss pathology should remain blocked or at least
`WATCH`.

Portfolio hedge candidates should also be checked as overlays instead of only
standalone long curves:

```bash
python -m liquidity_migration \
  --data-root DATA_ROOT \
  portfolio-hedge \
  --short-report-dir DATA_ROOT/reports/current_qsqueeze_promoted_20260518 \
  --long-report-dir DATA_ROOT/reports/hedge_volume_shelf_q20_h3_20260518 \
  --hedge-weights 0.25,0.5,1.0 \
  --report-dir DATA_ROOT/reports/portfolio_hedge
```

The first useful hedge candidate is `volume_shelf_reclaim` q20/h3. It is not a
promoted standalone long because model court failed it, but as a 0.25-0.50
overlay it reduced the promoted short book's daily-overlay drawdown and had
negative common-day correlation. Keep it as a shadow challenger only.

Overnight full-PIT runner:

```bash
bash scripts/run_fullpit_volume_overnight.sh
```

PowerShell runner:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_fullpit_volume_overnight.ps1
```

Default full-PIT runner behavior:

```text
sync repo to origin/main
install/update local venv
run focused smoke tests
build/resume full Bybit USDT public archive manifest
fill/resume full PIT 1h klines from Bybit v5 API with 16 workers
validate manifest-to-parquet coverage
run the selected liquidity-migration reversal backtest only
```

The canonical full-PIT research root is
`~/SHARED_DATA/bybit_fullpit_1h`. It is shared by serious
research runs and now includes the recent `2026-04-18` to `2026-05-18`
Bybit-native download. Keep live demo ledgers separate under
`data/bybit-demo-event`.

The old background creative watcher and event-grid runner path are removed from
the active workflow. New ideas should be run as named, foreground
`volume-events` commands with explicit event families, parameters, and report
directories.

`volume-events` also exposes research controls for entry delay, entry policy,
rank-decay exit threshold, global liquidity filters, tail-liquidity rank bounds,
rank improvement, failed-fade exits, absorption move caps, dry-up quiet-regime
filters, and exhaustion day-return thresholds. Use these to test whether an edge
is immediate, delayed, concentrated in tails, or only an exhaustion artifact.
`--liquidity-migration-crowding-filter model_v1` enables the research-only
cross-sectional crowding classifier. It labels isolated idiosyncratic events,
liquidity-migration idiosyncratic events, sector/theme waves, broad-market
impulses, exchange/liquidity artifacts, and uncertain clusters. The first
full-PIT `model_v1` run was profitable but rejected as a promoted replacement
because it cut too much return from the current strategy.

Full PIT data build for event research:

```bash
python -m liquidity_migration \
  --data-root ~/SHARED_DATA/bybit_fullpit_1h \
  --config configs/volume_alpha.default.yaml \
  archive-manifest \
  --name canonical-pit-all-usdt-20230503-20260518 \
  --start 2023-05-03 \
  --end 2026-05-18 \
  --workers 32

python -m liquidity_migration \
  --data-root ~/SHARED_DATA/bybit_fullpit_1h \
  --config configs/volume_alpha.default.yaml \
  archive-download-klines-1h-api \
  --name canonical-fullpit-1h-all-usdt-20230503-20260518 \
  --start 2023-05-03 \
  --end 2026-05-18 \
  --workers 16 \
  --min-existing-bars 1 \
  --limit 1000 \
  --retries 8 \
  --timeout-seconds 30 \
  --request-sleep-seconds 0.02
```

3. Add split evaluation:

```text
train:      2023-05-03 to 2024-05-03
validation: 2024-05-03 to 2025-05-03
OOS:        2025-05-03 to 2026-05-03
```

4. Add promotion report:

```text
event family
side
thresholds
split returns
drawdowns
Sharpe-like
trade count
turnover
fees/funding
top failure reason
```

## Current Status

Fixed-rebalance volume results are archived as exploratory only.
They are not the path forward.

The event-driven runner now exists as `volume-events`. It writes scenario
summary, best-scenario trades, baskets, equity, monthly returns, JSON, and
Markdown reports. Promotion requires full point-in-time membership and full
PIT universe coverage. Do not use current-universe subsets for strategy
research decisions. The survivorship-free event run must use a data root whose
`klines_1h` coverage was built from the full `archive_trade_manifest` with
`archive-download-klines-1h-api`; raw trade archive repair remains the fallback
for any API gaps.

Forward demo testing now uses `event-demo-cycle`, not the retired daily-close
executor. The continuous runner is:

```bash
SUBMIT_ORDERS=1 CONFIRM_DEMO_ORDERS=1 TELEGRAM_ENABLED=1 bash scripts/run_bybit_demo_event_engine.sh
```

The runner checks every 5 minutes by script default; the VPS systemd entry
service intentionally overrides this to `INTERVAL_SECONDS=60` and
`STRATEGY_PROFILE=demo_relaxed`. The `demo_relaxed` profile is a higher-frequency demo-only
test system: rank 11-260, 80-rank improvement, 3.0 turnover ratio, -3% day-return
floor, +3% residual-return floor, 0.25 close-location floor, no extra current
24h turnover floor, 10 max active symbols, 2-day cooldown, and the
`union_pathology` crowding veto still enabled. It uses the same conservative
`promoted_quality_squeeze` entry router for promoted-grade events and keeps the
normal 1h entry for lower-tier `demo_relaxed` events.
It sizes each accepted coin from the backtest weight (`gross_exposure /
max_active_symbols`, currently 10.00% of current Bybit demo USDT equity), exits
before entries, sends Telegram only for material events when enabled, and records
`event_demo_trades`, `event_demo_orders`, and `event_demo_cycles` ledgers. It is
a current-universe forward tester, so it is allowed for demo evidence and
operations, not for historical promotion evidence.
Before submitting entries, the runner snapshots current Bybit positions, open
orders, and wallet equity. It blocks candidates whose symbols already have live
exchange exposure or non-reduce-only open orders. In submit mode, a
position/open-order/wallet snapshot error blocks all new entries for that cycle
rather than trusting the ledger alone or sizing from stale equity.
Position and wallet snapshot failures during open-trade handling are surfaced
in the cycle report and keep the cycle alive, so an outage cannot crash exits,
report writing, or the entry guard.
`demo_relaxed` mode full-PIT funded evidence on 2023-05-03 to 2026-05-03 with the
conservative entry router: 1,268 trades, +221.29% total return, -21.32% max
drawdown, -18.90% worst 90d, +12.36% worst split, +142.92% OOS, and promotion
gate pass. Stress report:
`/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/entry_signal_cross_strategy_20260517/quality_tier_stress/quality_tier_stress_report.md`.
Research-only execution variants now exist behind
`--entry-policy execution_pullback_guard`,
`--entry-policy tiered_execution_sniper`, and
`--entry-execution-veto-close-location-max`. They are disabled in the active
profile. The 2026-05-18 execution-alpha audit rejected them for deployment:
pullback gating damaged the book, the first tiered-pop result contained a
lookahead fallback that was fixed, the causal tiered-pop result did not beat the
current relaxed profile, and the high-close veto failed in real backtests
despite looking attractive in a static ledger slice.
Recent 1h bars are cached in `event_demo_klines_1h`, keeping the forward-demo
cache separate from the full-PIT research `klines_1h` dataset.
Entry orders attach native stop/TP immediately, then confirmed fills recompute
the ledger stop/TP from actual fill price and repair Bybit trading-stop state
when rounding moves the protection levels.
Stale unconfirmed entry rows are terminalized only after successful Bybit
position and open-order snapshots prove the symbol is flat and has no active
entry order. Live exposure or a snapshot failure keeps the pending row intact.
When live exposure or an active open order exists for an old unconfirmed entry,
fill reconciliation keeps polling that stale row and rebuilds the missing trade
ledger once Bybit trade history reports the fill. Stale rows without live
exchange evidence are still skipped instead of being polled forever.

Fast exit enforcement is handled by the separate exit-only risk watchdog:

```bash
SUBMIT_ORDERS=1 CONFIRM_DEMO_ORDERS=1 TELEGRAM_ENABLED=1 bash scripts/run_bybit_demo_ws_risk_engine.sh
```

The watchdog now defaults to `event-risk-ws`: exchange-native stops first,
demo private WebSocket position/order/execution streams plus the mainnet public
ticker stream second, and REST only for demo fallback/reconciliation. It does not scan for entries. It repairs
exchange-native stops/TPs, subscribes to active-position ticker streams, forces
reduce-only exits on streamed stop, take-profit, or max-hold breaches, and can
mark the ledger closed from WebSocket execution messages. Bybit currently does
not support WebSocket Trade order entry for demo trading, so the demo VPS uses
`ORDER_SUBMIT_MODE=ws_then_rest`: WebSocket decides, REST submits only when demo
WS order entry is unavailable.
If a future WebSocket Trade path is enabled and an order ack rejects the exit
asynchronously, the watchdog marks the WS order row rejected and submits the
configured REST fallback once. Confirmed REST risk exits record filled order
rows instead of leaving closed trades paired with merely `submitted` orders.
On startup and REST reconciliation, tracked-position exits are evaluated before
stop repair, and stop repair skips symbols with pending or live AGC exit orders.
The risk path should flatten a breached position before spending a REST call on
repairing protection for that same position.
The demo private socket rejects `execution.fast`, so the VPS uses the normal
private execution stream unless that limitation is retested and cleared.
Socket startup is bounded by `STREAM_START_TIMEOUT_SECONDS` so a blocked
private/public subscription reports an error and leaves REST reconciliation plus
exchange-native stops running instead of hanging the watchdog before startup.
The watchdog writes latest reports under `reports/event-risk-ws` every heartbeat
and keeps timestamped JSON/Markdown snapshots for startup and material risk
events, so exit decisions survive later quiet heartbeat overwrites.
WebSocket execution/order-stream closures preserve the original exit reason and
trigger timestamp from the submitted exit order row, so the ledger remains
auditable when REST polling is not the closing path.
WebSocket-streamed partial fills reduce the open ledger quantity immediately,
record partial-exit context, and keep the submitted-symbol duplicate guard
active until the target exit quantity is filled; untracked emergency exits get
the same guard treatment on partial stream fills.
Confirmed partial event/risk exits immediately reduce the open ledger quantity
and record partial-exit context. Limit-chase risk exits keep per-child IOC and
fallback order status, target quantity, filled quantity, and notional instead
of stamping the aggregate fill state onto every child order row.
Failed tracked risk-exit submissions also keep the trade id, exit reason,
trigger timestamp, target quantity, and planned exit price on the failed order
row. A rejected emergency exit therefore stays tied to the open trade in the
audit ledger instead of becoming an anonymous failed order.
Fresh pending reduce-only untracked-position exits are restored after restart
even though they have no ledger trade ID, so the watchdog does not duplicate a
still-pending emergency flatten order after process loss.
Stale pending reduce-only exits are terminalized only when successful Bybit
position and open-order snapshots show no live position and no live AGC exit
order for the symbol, so old local rows do not keep reporting false pending
exposure after the exchange is already flat.
The same flat/no-open-order evidence terminalizes stale pending entry rows;
stale entries with a live position or active entry order are reconciled first,
so delayed fill-history recovery can rebuild a missing trade ledger before the
watchdog treats the position as untracked.
Live AGC reduce-only exit open orders on Bybit are also treated as active exit
submissions, covering crashes that placed an exit but lost the local order row.
Manual/native reduce-only protection orders do not suppress emergency exits.
Material Telegram alert keys are persisted in the same report directory, so
restarting the watchdog does not resend the same alert.
Stop-repair alerts are keyed by symbol and target stop/TP rather than synthetic
repair order-link IDs, so repeated confirmations for the same protection target
do not create notification noise.

Current live demo order-path proof and deployment status are summarized in
`docs/system_status.md`. The old one-off proof/probe scripts were retired after
their evidence was captured there.

## Retired Research Summary

Creative full-PIT controls tested on 2026-05-17 included 7d momentum,
proximity-to-high, prior-month MAX/salience, prior return volatility, and
intraday range. None beat the current liquidity-migration short on strict
full-PIT improvement criteria. The closest sleeve lowered drawdown near the
30-day high but sacrificed too much return and was delay sensitive, so the
selected demo strategy did not change.

## Current Promoted Full-PIT Result

Active defaults after the same-hour crowding audit and gross cleanup:

```text
event: liquidity_migration
side: reversal (short)
threshold: top 40% dollar-volume rank migration
filters:
  point-in-time liquidity rank 31-150
  liquidity-rank improvement >= 150
  turnover / prior 7d mean >= 6.0
  event rank fraction <= 0.90
  coin daily_return_1d >= 0%
  coin daily_return_1d - market_median_return_1d >= +8%
  market_pct_up_1d <= 0.65 OR coin daily_return_1d clears the adaptive 16% +/- 1.5% hot-market band
  signal-day close location >= 0.45
  PIT/listing age >= 90 days
  union_pathology crowding veto
entry policy: promoted_quality_squeeze
standard entry: 1 hour after signal close
promoted-grade squeeze entry: if the first completed 1h post-signal bar is up >= 50 bps from signal close and closes >= 0.85 inside its own high-low range, wait for a 25 bps high-since-signal giveback after at least a 25 bps pop, otherwise enter on the 4h deadline
hold: 3 days max
stop: 12% fixed
take profit: 26% fixed
gross exposure: 1.00
capacity: max 5 active symbols
cooldown: 5 days
stop-pressure throttle: pause new entries after 7 realized stops inside 10 days
realized-loss throttle: pause new entries after 6 realized losses inside 5 days
cost: 3x base round-trip cost
```

Equivalent explicit command:

```bash
python -m liquidity_migration \
  --data-root DATA_ROOT \
  --config configs/volume_alpha.default.yaml \
  volume-events \
  --event-types liquidity_migration \
  --thresholds 0.4 \
  --hold-days 3 \
  --sides reversal \
  --stop-loss-pcts 0.12 \
  --take-profit-pcts 0.26 \
  --cost-multipliers 3 \
  --entry-delay-hours 1 \
  --entry-policy promoted_quality_squeeze \
  --entry-quality-squeeze-h1-return-bps 50 \
  --entry-quality-squeeze-h1-close-location-min 0.85 \
  --entry-quality-squeeze-pop-bps 25 \
  --entry-quality-squeeze-giveback-bps 25 \
  --entry-quality-squeeze-wait-hours 4 \
  --gross-exposure 1.0 \
  --max-active-symbols 5 \
  --cooldown-days 5 \
  --rank-exit-threshold 0.55 \
  --universe-rank-min 31 \
  --universe-rank-max 150 \
  --liquidity-migration-rank-improvement-min 150 \
  --liquidity-migration-turnover-ratio-min 6.0 \
  --liquidity-migration-event-rank-fraction-max 0.90 \
  --liquidity-migration-event-rank-fraction-exclude-min 0 \
  --liquidity-migration-event-rank-fraction-exclude-max 0 \
  --liquidity-migration-day-return-min 0.0 \
  --liquidity-migration-residual-return-min 0.08 \
  --liquidity-migration-market-pct-up-max 0.65 \
  --liquidity-migration-hot-market-day-return-min 0.16 \
  --liquidity-migration-hot-market-day-return-band 0.015 \
  --liquidity-migration-close-location-min 0.45 \
  --liquidity-migration-pit-age-days-min 90 \
  --liquidity-migration-crowding-filter union_pathology \
  --stop-pressure-window-days 10 \
  --stop-pressure-stop-count 7 \
  --realized-loss-pressure-window-days 5 \
  --realized-loss-pressure-loss-count 6
```

Promoted TP26 result on the canonical full-PIT root through `2026-05-18`:

```text
report: /Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/exit_alpha_20260519/promoted_tp_fine_245_280/volume_event_research_report.md
trades: 448
total return: +2022.17%
max drawdown: -13.72%
max no-new-high stretch: 54 days
worst 90d return: -6.29%
worst split return: +126.03%
average split Sharpe-like: 3.62
train return: +126.03%
validation return: +224.90%
OOS return: +183.27%
default chart: volume_event_best_equity_btc.png with BTC overlay,
monthly/growth gridlines, and a monthly performance table
promotion gate: pass
```

TP26 replaced TP25 because it improved exact-stop return, minimum split, OOS,
and split Sharpe without worsening exact-stop max drawdown or worst-90d. It
also beat TP25 under 1x/3x/5x cost stress and adverse hourly stop-fill stress,
although the adverse stop-fill run still fails the formal drawdown gate for
both TPs. Candidate selection, entries, cooldowns, crowding decisions, and
gross exposure remain unchanged.

Funding and execution-realism update from `2026-05-17`:

```text
funded default report: data/research_reports/research_20260517_default_with_funding_oi_features
funded default return: +957.82%
funded default max drawdown: -18.62%
funding return drag: -22.03%

adverse stop-fill report: data/research_reports/research_20260517_default_funding_bar_extreme_stops
adverse stop-fill return: +141.34%
adverse stop-fill max drawdown: -35.88%
adverse stop-fill OOS return: -13.69%
adverse stop-fill promotion gate: fail
```

The strongest new research candidate is the same liquidity-migration short
strategy gated to `funding_7d_sum >= 0` at signal close. It is not the active
demo default yet because there is no forward/demo evidence and the demo cycle
does not currently compute the 7d funding gate. Full report:

```text
data/research_reports/research_20260517_funding7_positive_bar_extreme_stops
return: +213.66%
max drawdown: -16.61%
min split return: +12.10%
funding return: +3.13%
promotion gate: pass under adverse hourly stop fills
```

In the promotion comparison, BTC/ETH/SOL/BNB/XRP/TRX took no direct trades, but
including them improved the PIT rank map and market context. That is why the
default exclusion rule is now stable/peg-only rather than a manual top-coin
blacklist.

## Feature Factory

`feature-factory` is a shadow audit command for controlled feature research. It
reads a completed `volume-events` report, checks candidate feature coverage,
scores high-vs-low tertile edges against shuffled-feature controls, and writes
split/interaction diagnostics. It does not change live trading.

Current feature-factory promoted rerun:

```bash
python -m liquidity_migration \
  --data-root /Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h \
  volume-events \
  --report-dir /Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/feature_factory_promoted_20260518

python -m liquidity_migration \
  --data-root /Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h \
  feature-factory \
  --report-dir /Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/feature_factory_promoted_20260518 \
  --output-dir /Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/feature_factory_promoted_20260518/feature_factory \
  --min-rows 24 \
  --shuffle-samples 128
```

Result:

```text
promoted rerun: 444 trades, +1853.99% return, -13.72% max drawdown, -6.29% worst 90d, +175.32% OOS
feature coverage: 16/27 non-null audited features
report: /Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/feature_factory_promoted_20260518/feature_factory/feature_factory_report.md
```

New causal columns now written to trade ledgers include:

```text
event_uniqueness_score
prior1_liquidity_rank / prior3_liquidity_rank / prior7_liquidity_rank
liquidity_rank_improvement_1d / 3d / 7d
liquidity_rank_speed_3d
prior7_intraday_range_mean
intraday_range_expansion_7d
mark_index_basis_* and premium_index_* when datasets exist
```

Honest read: the new rank-speed, volatility-expansion, and uniqueness columns
are useful audit dimensions, but they did not beat shuffled-feature controls on
the promoted ledger. Funding features, residual return, and close-to-30d-high
screened strongest, but that is still shadow evidence. Any future feature gate
must go through the Model Court with data coverage and stress evidence.

## Alpha Sweep 2026-05-18

After the feature-factory screen, three alpha branches were tested against the
current funded promoted baseline:

```text
baseline: +1853.99% total, -13.72% max DD, -6.29% worst 90d, +122.17% min split, +175.32% OOS, 444 trades
```

Funding/extension gates were rejected. They made the book cleaner but not
better:

```text
funding_7d_sum >= 0: +718.21%, -9.07% max DD, +90.90% min split, +90.90% OOS, 256 trades
funding_rate_last >= 0: +705.26%, -7.87% max DD, +74.31% min split, +74.31% OOS, 244 trades
residual_return_1d >= 15%: +825.33%, -14.28% max DD, +66.29% min split, +123.74% OOS, 353 trades
```

A late-day turnover concentration control was added as disabled-by-default
research tooling:

```text
--liquidity-migration-signal-last6h-turnover-share-max
```

It rejects single-name blowoff events where too much of the signal-day turnover
arrives in the final six hours. The idea was plausible because high final-6h
turnover-share trades had a materially higher stop rate in the promoted ledger,
but exact lifecycle tests still failed to beat the baseline:

```text
last6h share <= 0.90: +1671.00%, -14.00% max DD, +129.21% min split, +140.74% OOS, 427 trades
last6h share <= 0.80: +1319.62%, -14.00% max DD, +99.85% min split, +99.85% OOS, 398 trades
```

Wider strict PIT rank bands were also rejected. They added trades and sometimes
lifted OOS, but worsened path risk:

```text
rank 31-220 strict: +1807.79%, -16.85% max DD, -16.21% worst 90d, +209.43% OOS, 450 trades
rank 11-260 strict: +1768.80%, -17.28% max DD, -14.95% worst 90d, +228.44% OOS, 451 trades
```

Combining wider rank bands with the late-turnover guard did not rescue the
idea:

```text
rank 31-220 + last6h <= 0.90: +1614.37%, -15.68% max DD, +133.05% min split, +169.89% OOS
rank 11-260 + last6h <= 0.90: +1545.37%, -16.61% max DD, +132.46% min split, +180.69% OOS
```

Decision: reject these as alpha upgrades. The useful lesson is that the current
edge depends on keeping a surprisingly broad set of ugly-looking trades; most
obvious filters improve average trade quality while damaging compounding. Future
alpha work should prioritize new entry/exit mechanics or genuinely new data
surfaces such as OI, basis, and signed flow, not more hard filters on the
existing daily ledger.

### Exit Alpha 2026-05-19

Failed-fade exit research on the current `demo_relaxed` VPS profile found a
real candidate, but not a clean formal promotion. The current live demo profile
exits a short after 6 completed post-entry hours when MFE is below 1% and the
trade is already losing more than 4%, and uses 21% take-profit. Current
Full-PIT report:
`~/SHARED_DATA/bybit_fullpit_1h/reports/exit_alpha_20260519/demo_relaxed_failedfade_ff6_tp_sl_fine`.

Result versus the older VPS baseline: TP21 + FF6 produced +353.46% total
return vs +225.63%, -16.72% max DD vs -21.32%, -12.72% worst 90d vs -18.90%,
+23.53% min split vs +12.36%, +1.370 avg split Sharpe vs +1.042, and +165.57%
OOS vs +142.92%. Focused model court verdict remains `WATCH`, not `PASS`:
normal cost stress is strong, negative controls pass, but adverse
hourly-extreme stop-fill stress remains the hard failure mode. Treat this as
demo-only execution research, not real-money promotion.

## Serious Data Layer

`data-layer-audit` is the gatekeeper for any new OI, basis, premium, funding, or
flow research. It writes exact coverage tables for Bybit-native datasets and
Binance USD-M proxy datasets, then labels each feature pack as full-window,
partial-window, missing, or proxy-only.

```bash
python -m liquidity_migration \
  --data-root /Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h \
  data-layer-audit \
  --start 2023-05-03 \
  --end 2026-05-03 \
  --name fullpit_aux_coverage
```

Binance proxy downloads are intentionally stored in separate tables such as
`binance_usdm_mark_price_1h` and `binance_usdm_funding`; they do not overwrite
or pretend to be Bybit-native datasets:

```bash
python -m liquidity_migration \
  --data-root /Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h \
  download-binance-proxy \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT \
  --start 2025-05-03 \
  --end 2026-05-03 \
  --datasets klines_1h,funding,mark_price_1h,index_price_1h,premium_index_1h,open_interest,taker_flow_1h \
  --workers 1
```

Research rule: Binance proxy features can justify building a hypothesis, but
they cannot promote a Bybit strategy on their own. Promotion still requires
Bybit-native PIT coverage or a separately argued Model Court exception. Binance
USD-M open-interest and taker-flow REST history are recent-window only in the
official docs, so treat them as live calibration / short-window validation
unless archived continuously.

Current `2026-05-18` data-layer audit after the pilot pull:

```text
report: /Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/data_layer_post_pilot_aux_coverage/data_layer_audit.md
full-PIT reference symbol-days: 367,533
Bybit klines/funding: full coverage
Bybit native basis/premium pilot: 5 symbols, 2025-05-03..2026-05-02, 0.47% full-root symbol-day coverage
Bybit signed_flow_1h: missing
Binance basis/funding proxy pilot: BTCUSDT/ETHUSDT, 2025-05-03..2026-05-02
Binance OI/taker proxy pilot: BTCUSDT/ETHUSDT, 2026-04-18..2026-05-02 only
decision: usable for exploratory feature tests only, not promotion evidence
```

Full OOS Bybit-native basis/premium expansion on `2026-05-18`:

```text
coverage report: /Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/data_layer_full_oos_basis_coverage_20260518/data_layer_audit.md
window: 2025-05-03 to 2026-05-03 end-exclusive
reference symbol-days: 123,421
mark_price_1h / index_price_1h / premium_index_1h: 99.86% native OOS coverage
open_interest: 24.23% native OOS coverage
signed_flow_1h: missing
decision: native basis/premium is usable for OOS feature falsification; promotion still needs full-window native coverage and Model Court evidence
```

Auxiliary alpha readout from the expanded OOS store:

```text
feature report: /Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/aux_alpha_full_oos_pit_20260518/feature_factory/feature_factory_report.md
baseline OOS: +175.32%, -13.72% max DD, -2.47% worst 90d, 161 trades
basis_3d_mean >= -0.00037613: +74.47%, -6.53% max DD, 57 trades
basis_3d_mean >= 0.00008507: +70.93%, -9.42% max DD, 36 trades
premium_3d_mean >= -0.00003028: +63.15%, -4.85% max DD, 35 trades
volume_to_oi_quote >= 6.55: +60.05%, -4.96% max DD, 45 trades
```

Decision: reject basis, premium, and OI-ratio gates as promoted alpha. They
create cleaner small sleeves, but they cut too many trades and do not beat the
current book. The result also invalidated the earlier traded-symbol-only basis
screen, which was selection-biased because auxiliary data had first been pulled
only for symbols the baseline already traded.

Rank/shape follow-up from the same research pass:

```text
OOS sweep: /Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/aux_alpha_full_oos_pit_20260518/research_sweep/candidate_sweep.csv
full sweep: /Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/alpha_rank_sensitivity_20260518/full_research_sweep/full_candidate_sweep.csv
shape sweep: /Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/alpha_shape_filters_20260518/shape_filter_full_sweep.csv
```

The tempting OOS result was `liquidity_migration_rank_improvement_min=184`:
`+159.67%` OOS with `-9.62%` max DD versus the baseline `+175.32%` and
`-13.72%`. Full PIT rejected it: `+769.06%`, `-16.21%` max DD, `+51.68%`
train return, and `+159.67%` OOS versus the current baseline `+1853.99%`,
`-13.72%` max DD, `+122.17%` train return, and `+175.32%` OOS. The failure is
exactly why OOS-only parameter promotion is not allowed.

Additional full-window shape filters were also rejected:

```text
rank1_improvement >= 99: +1246.05%, -12.32% max DD, +75.25% min split
residual_return_1d >= 12%: +1309.78%, -12.91% max DD, +101.17% min split
prior30_max_daily_return >= 7.3%: +947.50%, -10.62% max DD, +91.67% min split
event_uniqueness_score >= 0.87705: +608.15%, -20.95% max DD, +40.79% min split
signal_day_last6h_return >= 0.00769: +600.49%, -21.56% max DD, +69.17% min split
```

Decision: keep the active promoted strategy unchanged. The useful discovery is
negative: the current edge is broad and compounding-sensitive, so most obvious
"quality" gates improve per-trade averages but damage full-period growth or
older splits. Future alpha should focus on execution mechanics, new Bybit-native
signed-flow/OI archives, or portfolio overlay construction, not another hard
filter on the existing daily ledger unless it clears the full Model Court.

## Champion / Challenger Stack

`champion-challenger` writes the current live research stack manifest:

```bash
python -m liquidity_migration \
  --data-root data/bybit-demo-event \
  champion-challenger
```

The active order-submitting champion is `demo_relaxed` only. Shadow challengers
exist for current promoted, relaxed-without-crowding, tiered sniper execution,
pullback-guard execution, and the volume-shelf hedge overlay. Their commands do
not contain `--submit-orders` or `SUBMIT_ORDERS=1`, and the live runner refuses
order submission for non-`demo_relaxed` profiles. This is intentionally
conservative: challengers are evidence generators, not trading authority.
