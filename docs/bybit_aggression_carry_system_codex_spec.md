# Bybit Demo Trading System Spec

## Objective

The project focus is a profitable Bybit demo-account system built around the selected full-PIT liquidity-migration short strategy. The old fixed daily-close short fade is retired and removed from the active repo surface.

Work should improve one of four things:

- point-in-time data quality
- event feature quality
- event-driven backtest and risk realism
- demo execution parity for the selected event lifecycle

The current private Bybit client is demo-only and refuses `demo=False`. Real-money support is a separate implementation, not a hidden mode.

## Active Strategy

Canonical command:

```bash
python -m aggression_carry \
  --data-root DATA_ROOT \
  --config configs/volume_alpha.default.yaml \
  volume-events
```

`volume-events` defaults are the selected full-PIT strategy:

```text
event: liquidity_migration
side: reversal / short
threshold: top 40% dollar-volume rank migration
PIT liquidity rank: 31-150
excluded: stable/peg perps only, including failed peg remnants such as USTCUSDT
rank improvement: >= 150 versus prior 7d liquidity rank
turnover expansion: current turnover / prior 7d mean >= 6.0
event rank fraction cap: <= 0.90
event rank middle-band skip: disabled
coin day-return gate: daily_return_1d >= 0%
idiosyncratic move gate: daily_return_1d - market_median_return_1d >= +8%
regime gate: market_pct_up_1d <= 0.65 OR coin daily_return_1d clears the adaptive 16% +/- 1.5% hot-market band
signal close-location gate: close location >= 0.45
PIT/listing age gate: >= 90 days
crowding veto: union_pathology same-entry-hour pathology filter
entry policy: promoted_quality_squeeze
standard entry: 1 hour after daily signal close
promoted-grade squeeze entry: if the first completed 1h post-signal bar is up >= 50 bps from signal close and closes >= 0.85 inside its own high-low range, wait for a causal high-since-signal giveback of 25 bps after at least a 25 bps pop, otherwise enter on the 4h deadline
research-only execution variants: execution_pullback_guard, tiered_execution_sniper, and entry_execution_veto_close_location_max exist for audits only and are not deployed
max hold: 3 days
stop: 12% fixed
take profit: 25% fixed
gross exposure: 1.00
max active symbols: 5
symbol cooldown: 5 days
stop-pressure throttle: pause after 7 realized stops inside 10 days
realized-loss throttle: pause after 6 realized losses inside 5 days
cost multiplier: 3x base round-trip cost
```

The strategy is short-only because the best full-PIT evidence is in reversal after liquidity migration. Long continuation is not promoted unless a fresh full-PIT run clears costs, splits, drawdown, and report gates.

## Reference Evidence

Current promoted frontier after the same-hour crowding audit, gross cleanup, and conservative quality-squeeze entry router:

```text
report: /Users/jhbvdnsbkvnsd/agc-bybit-fullpit-funded-20230503-20260503/reports/entry_signal_cross_strategy_20260517/quality_tier_stress/quality_tier_stress_report.md
event: liquidity_migration
side: reversal / short
threshold: top 40% dollar-volume rank migration
gross exposure: 1.00
max active symbols: 5
trades: 444
total return: +2285.54%
max drawdown: -11.05%
max no-new-high stretch: 51 days
worst 90d return: -5.02%
worst split return: +118.81%
average split Sharpe-like: 3.78
OOS return: +210.35%
promotion gate: pass
```

Those frontier metrics use exact stop fills, 3x base round-trip costs, and no
funding model. Funding stress on the same conservative router produced 444
trades, +1853.99% total return, -13.72% max drawdown, -6.29% worst 90d,
+122.17% worst split, and +175.32% OOS. The event set, exits, cooldowns,
crowding decisions, and gross exposure remain unchanged; only entry timing
within the post-signal causal window changed.

`model_v1` is a research-only cross-sectional crowding classifier, not the
active promoted filter. Its first full-PIT run traded only idiosyncratic /
liquidity-migration classes and stayed profitable, but return fell too far
versus the promoted strategy, so `union_pathology` remains the deployed
crowding veto.

Superseded baseline full-PIT report after the earlier hold/exit frontier
confirmation:

```text
data/research_reports/research_20260516_promoted_default_after_patch
```

Full-PIT result on `2023-05-03` to `2026-05-03`:

```text
trades: 516
total return: +1218.79%
max drawdown: -14.54%
max no-new-high stretch: 90 days
worst 90d return: -5.89%
worst split return: +75.64%
average split Sharpe-like: 2.67
train return: +75.64%
validation return: +254.58%
OOS return: +111.75%
default chart: volume_event_best_equity_btc.png with BTC overlay and monthly/growth gridlines
promotion gate: pass
```

Top coins are no longer manually blacklisted. In the promotion comparison,
BTC/ETH/SOL/BNB/XRP/TRX took no direct trades, but including them improved the
point-in-time rank universe and market context used by the alpha.

Funding is not modeled in that root because funding data is missing. Fees and slippage are stress-tested through the 3x cost multiplier; funding remains a required demo/forward parity item before real-money work.

## Data Contract

Serious strategy runs must use:

- `archive_trade_manifest` as point-in-time symbol/date membership
- `klines_1h` coverage for every manifest symbol/date in the run window
- causal daily signal features, with standard entry delayed to the next configured 1h bar and promoted-grade squeeze entries delayed only by the causal giveback/deadline router
- full trade ledger, basket ledger, equity curve, BTC overlay chart, monthly returns, JSON config, and Markdown report

`volume-events` requires full PIT coverage by default. `--allow-partial-pit` is only for explicitly biased diagnostics and must not be used as promotion evidence.

## Execution Status

The old daily-close demo executor was intentionally removed because it encoded the retired calendar-clock candidate scan and sleeve assumptions. Do not revive it.

The active demo execution path is `event-demo-cycle` plus the continuous runner:

```bash
python -m aggression_carry \
  --data-root data/bybit-demo-event \
  --config configs/volume_alpha.default.yaml \
  event-demo-cycle
```

```bash
SUBMIT_ORDERS=1 CONFIRM_DEMO_ORDERS=1 TELEGRAM_ENABLED=1 bash scripts/run_bybit_demo_event_engine.sh
```

The runner loops every `INTERVAL_SECONDS=300` by script default. The VPS
systemd entry service intentionally overrides this to `INTERVAL_SECONDS=60` and
`STRATEGY_PROFILE=demo_relaxed`.
Each cycle:

1. Pulls the current Bybit USDT perpetual universe through rank 300 in demo_relaxed mode so the relaxed rank 11-260 test profile can be evaluated forward.
2. Excludes only stable/peg perps, including failed peg remnants such as USTCUSDT, before ranks/features are built.
3. Rebuilds recent 1h volume features from a 45-day lookback, using the forward-demo kline cache so normal cycles fetch only missing/new 1h bars instead of the whole window.
4. Exits existing demo positions first on fixed-stop/take-profit reconciliation, event decay, rank exit, or 3-day max hold.
5. Enters accepted liquidity-migration events through the `promoted_quality_squeeze` router: standard events enter after the 1-hour signal delay, while promoted-grade squeeze events wait for the completed-bar giveback trigger or the 4h deadline. Entries remain subject to max-active, cooldown, stop-pressure, day-return, residual-return, stale-entry, pending-order, live-open-order, live-position, and wallet-equity gates. In demo_relaxed mode those gates are intentionally relaxed to rank 11-260, no extra current 24h turnover floor, 80-rank improvement, 3.0 turnover ratio, -3% day-return floor, +3% residual-return floor, 0.25 close-location floor, 10 max active symbols, and 2-day cooldown. The same `union_pathology` crowding veto stays active. Stale entries are skipped after 15 minutes by default so demo fills stay close to the backtest entry timestamp. In submit mode, the runner snapshots current Bybit positions, open orders, and wallet equity before entries; a live exchange position or non-reduce-only open order for the candidate symbol blocks a new entry, and a position/open-order/wallet snapshot error blocks all new entries for that cycle. Stale unconfirmed entry rows are normally not polled forever, but if a current Bybit position or active open order proves the stale row may still represent live exposure, fill reconciliation keeps polling it and reconstructs the missing trade ledger if Bybit reports a fill. Position and wallet snapshot failures during open-trade handling are reported and keep the cycle alive instead of crashing before exits, reports, or the entry guard can run.
6. Sizes each accepted coin from the same weight used by the active profile backtest: `gross_exposure / max_active_symbols`, currently `1.00 / 10 = 10.00%` of current Bybit demo USDT equity in demo_relaxed mode. If wallet equity cannot be read in submit mode, the cycle uses fallback equity only for telemetry and does not submit new entries. `--max-order-notional-pct-equity` is only an explicit override. The continuous runner defaults entry leverage to 2x so the 100% gross target can be submitted without changing notional sizing.
7. Attaches exchange-native stop/take-profit to entry orders, then recomputes the ledger stop/take-profit from confirmed fill price. If the confirmed fill moves the rounded protection levels, the runner immediately updates Bybit trading-stop state and records the update status.
8. Sends Telegram only for material events when enabled: entries, exits, failed entry stop updates, position reconciliation, or position-report errors. Quiet cycles still write local reports but do not notify.
9. Writes expected/submitted order state into `event_demo_orders`, trade state into `event_demo_trades`, cycle telemetry into `event_demo_cycles`, and Markdown/JSON reports under `reports/event-demo`. Stale unconfirmed entry rows are terminalized only when successful Bybit position and open-order snapshots prove the symbol has no live position and no active entry order, preventing false pending ledger state without erasing possible live exposure.

Order submission is still fail-closed: `--submit-orders` requires
`--confirm-demo-orders`, `BYBIT_DEMO_API_KEY`, and `BYBIT_DEMO_API_SECRET`.
Without those, the command is a dry-run scan. In the champion/challenger stack,
the live runner also refuses `SUBMIT_ORDERS=1` unless
`STRATEGY_PROFILE=demo_relaxed` or its deprecated `observe` alias is used.
Promoted, no-crowding, sniper, execution-only, and hedge variants are shadow
challengers until the manifest and Model Court evidence are intentionally
updated.

Telegram may notify on material events, but it must not approve or submit orders. The continuous runner still fails startup when `TELEGRAM_ENABLED=1` but Telegram or Bybit demo credentials are missing, because event alerts include position/PnL context.

The `demo_relaxed` profile is explicitly a demo-only test system, not a replacement
promotion. Its full-PIT funded evidence is summarized in `docs/system_status.md`
and reports under
`/Users/jhbvdnsbkvnsd/agc-bybit-fullpit-funded-20230503-20260503/reports/observe_mode_sweep_20260517/observe_c`.

The exit-only risk watchdog is separate from the alpha loop:

```bash
SUBMIT_ORDERS=1 CONFIRM_DEMO_ORDERS=1 TELEGRAM_ENABLED=1 bash scripts/run_bybit_demo_ws_risk_engine.sh
```

It runs `event-risk-ws` by default. The priority order is:

1. Exchange-native stop/take-profit attached to the position.
2. WebSocket streams for live state: demo private position/order/execution streams plus the mainnet public ticker stream.
3. REST only as the demo fallback for order submission and periodic reconciliation.

This loop never opens positions and never approves orders through Telegram. It reads the demo trade ledger plus current Bybit positions, evaluates tracked-position stop/take-profit/max-hold exits before attempting stop repair, repairs missing or mismatched exchange-native stop/take-profit settings only for positions without an active exit, subscribes to active-position tickers, and sends reduce-only exits when a live streamed mark price crosses the ledger stop, streamed mark crosses the take-profit, or the max-hold timestamp has passed. WebSocket execution/order messages can close the ledger without REST polling and preserve the original exit reason plus trigger timestamp from the submitted exit order row; streamed partial fills reduce the open ledger quantity immediately and keep the duplicate-order guard active until the target quantity is filled. Confirmed partial event/risk exit fills immediately reduce the open ledger quantity instead of waiting for a later reconciliation pass. Limit-chase risk exits record each IOC/fallback child order with its own target quantity, filled quantity, status, and notional so one aggregate partial fill is not copied onto every child order. Failed risk-exit submissions keep the trade id, exit reason, trigger timestamp, target quantity, and planned price on the failed order row, so rejected emergency exits remain auditable and tied to the still-open trade. Fresh pending reduce-only untracked-position exits are restored after restart even though they have no ledger trade ID, so the duplicate-order guard survives process loss. Streamed partial fills for those untracked exits also keep the guard active instead of marking the emergency flatten complete early. Stale pending reduce-only exits are also terminalized when successful Bybit position and open-order snapshots show no live position and no live AGC exit order for that symbol, preventing old local rows from leaving false pending exposure after the exchange is flat. The same successful flat/no-open-order evidence terminalizes stale pending entry rows; stale pending entries with a live position or active entry order are reconciled before stale cleanup, so a delayed fill-history recovery can rebuild the missing trade ledger instead of leaving live exposure outside the tracked strategy. The watchdog also snapshots Bybit open orders and treats live AGC reduce-only exit orders as active exit submissions, covering crashes that placed an exit but lost the local order row; manual/native reduce-only protection orders do not suppress emergency exits. Quiet cycles update the latest local report but do not spam system logs; startup and material risk events also keep timestamped JSON/Markdown audit copies under `reports/event-risk-ws`. Material Telegram alert de-duplication is persisted under that report directory so a service restart does not resend the same alert; stop-repair alerts are keyed by symbol and target stop/TP rather than synthetic repair order-link IDs.

Bybit's current demo docs state that demo WebSocket supports private streams only, public data is the same as mainnet public WebSocket data, and WebSocket Trade order entry does not support demo trading. For that reason the demo VPS uses `ORDER_SUBMIT_MODE=ws_then_rest`: the risk decision path is WebSocket-first, but actual demo reduce-only order submission falls back to REST when the demo WS trade socket is unavailable. If a future WebSocket Trade path is enabled and then rejects an order asynchronously, the watchdog marks the WS order row rejected and submits the configured REST fallback once instead of leaving a false pending exit. The demo VPS also uses the normal private execution stream because `execution.fast` is not accepted by the demo private socket. Do not claim demo WS order-entry or fast-execution evidence unless these limitations are retested and cleared.

WebSocket startup is bounded by `STREAM_START_TIMEOUT_SECONDS` / `--stream-start-timeout-seconds`, default 3 seconds per startup operation. If private or public socket startup blocks, the risk daemon records the timeout, writes reports, and keeps REST reconciliation plus exchange-native stops active instead of hanging before the watchdog loop starts.

Cycle lock files record the owning PID and are recovered when that PID is no longer alive, including locks configured with no age-based stale timeout. Malformed or empty lock files are also recovered after a short invalid-payload grace period. This is required so a killed probe or service process cannot permanently block the risk daemon after restart.

`EXIT_ORDER_MODE=market` is the default and fastest emergency exit. `EXIT_ORDER_MODE=limit_chase` uses bounded reduce-only IOC limits, controlled by `LIMIT_CHASE_ATTEMPTS`, `LIMIT_CHASE_INITIAL_BPS`, `LIMIT_CHASE_STEP_BPS`, `LIMIT_CHASE_MAX_BPS`, and `LIMIT_CHASE_WAIT_SECONDS`, then falls back to market unless `LIMIT_CHASE_FALLBACK_MARKET=0`. Exchange-native stops remain the primary fast protection; the local watchdog is the repair/enforcement layer when venue state and ledger state diverge.

Current live demo order-path proof, deployment state, and remaining caveats are
summarized in `docs/system_status.md`. The old one-off proof/probe scripts were
removed after their evidence was summarized because they were not part of the
active runtime surface.
