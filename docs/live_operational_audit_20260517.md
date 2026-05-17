# Live Operational Audit - 2026-05-17

Objective: harden the Bybit demo system as if it is live capital, with emphasis on order-state truth, duplicate-order prevention, restart safety, Telegram noise control, and VPS deployment state.

## System State

- Active path: liquidity-migration event entry loop plus separate websocket risk watchdog.
- Venue mode: Bybit demo only. The private clients still refuse `demo=False`.
- Entry service: `model050426-bybit-demo.service`.
- Risk service: `model050426-bybit-risk.service`.
- VPS repo: `/opt/MODEL050426`.
- Active branch: `codex/vps-demo-risk-controls`.

## Fixes Shipped

- `c5cb150 fix(demo): ledger failed entry submits`
  - Entry leverage/order rejection is now recorded as a failed order row instead of crashing the event cycle.
  - Accepted entry orders with fill-history failures now stay `submitted_unconfirmed` for later reconciliation.
  - Failed entry submission is a material Telegram reason.

- `61f1efd fix(demo): keep unconfirmed exits pending`
  - Event exits and risk exits no longer mark accepted-but-unconfirmed reduce-only orders as hard failures.
  - Limit-chase exits stop chasing when fill confirmation fails after an IOC was accepted, avoiding blind duplicate exits.
  - Pending fill reconciliation records API failures without aborting the cycle.

- `86ac64d fix(risk): retry failed ticker subscriptions`
  - Public ticker subscription timeouts are no longer marked subscribed, so the risk engine can retry.
  - Accepted untracked-position exits stay pending when fill confirmation fails.
  - Untracked exit reconciliation can still mark an order filled when the live position is flat but trade history is unavailable.

- `1dc320b fix(probe): verify demo order cleanup`
  - The demo latency probe now verifies each probe order is cancelled and raises if a probe order remains open.

- `6de043c fix(runtime): preserve live cycle locks`
  - Active runtime wrappers no longer delete live cycle locks on startup.
  - PID-aware lock recovery remains responsible for stale/dead locks.

- `8a65469 fix(runtime): slow entry polling cadence`
  - Entry loop default moved from 60 seconds to 300 seconds.
  - VPS systemd entry service was updated to `Environment=INTERVAL_SECONDS=300`.
  - Rationale: entries are hourly-signal based with a 15-minute stale-entry window; fast exits are handled by the websocket risk service. The old 60-second loop was causing Bybit rate-limit messages while reloading about 160 symbols of 45-day 1h klines.

- `85f2524 fix(runtime): preserve legacy risk lock`
  - The legacy REST risk wrapper no longer deletes its live cycle lock either.

- `2ac31ed perf(demo): cache forward klines`
  - The event entry loop now stores recent forward-demo 1h bars in `event_demo_klines_1h`.
  - Normal cycles reuse cached bars and fetch only missing/new symbol windows.
  - This keeps the demo cache separate from the full-PIT research `klines_1h` dataset.

- `current slice: persist websocket risk audit reports`
  - The websocket risk daemon keeps latest heartbeat reports under `reports/event-risk-ws`.
  - Startup and material risk reports now also get timestamped JSON/Markdown snapshots.
  - CLI risk summaries point to `latest_event_ws_risk_cycle.md` for `event-risk-ws` instead of the legacy REST risk report path.

- `current slice: recover malformed runtime locks`
  - Shared file locks now recover malformed or empty lock payloads after a short invalid-payload grace period.
  - This closes the restart edge case where a process killed between lock creation and owner JSON write could block `event_ws_risk_cycle.lock` forever because that daemon intentionally uses no age-based stale timeout.

- `current slice: persist websocket risk Telegram de-dupe`
  - Websocket risk material alert keys are stored under `reports/event-risk-ws`.
  - A service restart no longer resets Telegram de-dupe state for the same material risk event.
  - Stop-repair alerts de-dupe by symbol and target stop/TP instead of synthetic repair order-link IDs.

- `current slice: recover pending untracked exits after restart`
  - Fresh pending reduce-only `untracked_position` exits are restored into websocket risk state even though they have no ledger trade ID.
  - This preserves the duplicate-order guard if the risk process restarts while an emergency flatten order is still unconfirmed.

- `current slice: block entries on live exchange exposure`
  - The entry loop now snapshots Bybit positions before submitting new entries.
  - Candidate symbols with existing live exchange exposure are skipped even if the ledger is missing that position.
  - In submit mode, a position-snapshot error skips all new entries for that cycle instead of trusting the ledger alone.

- `current slice: block entries on live open orders`
  - The entry loop now snapshots Bybit open orders before submitting new entries.
  - Candidate symbols with live non-reduce-only open orders are skipped even if the local pending-order guard has expired.
  - In submit mode, an open-order snapshot error skips all new entries for that cycle instead of trusting the ledger alone.
  - Event exits also skip symbols that already have an AGC reduce-only exit order live on Bybit, without treating manual/native reduce-only protection as a duplicate event exit.

- `current slice: recover live risk exit open-order guards`
  - The websocket risk watchdog now snapshots Bybit open orders and treats live AGC reduce-only exit orders as active exit submissions.
  - This covers the crash window where an exit order reached Bybit but the local `event_demo_orders` row was not written.
  - Manual/native reduce-only protection orders do not suppress emergency risk exits.

- `current slice: preserve streamed exit ledger context`
  - Websocket execution-stream and order-stream closures now preserve the submitted exit order row's `exit_reason`.
  - The same closure paths preserve `exit_trigger_ts_ms` instead of replacing the risk trigger time with the later fill/stream processing time.
  - Pending reduce-only fill reconciliation also keeps the original exit trigger timestamp when closing a trade after restart.

- `current slice: clear stale flat pending exits`
  - Live VPS verification found one stale local `submitted_unconfirmed` untracked BTC reduce-only exit while Bybit had zero positions and zero open orders.
  - The websocket risk daemon now marks pending reduce-only exits `filled` when successful Bybit position and open-order snapshots show no live position and no live AGC exit order for that symbol.
  - For tracked exits, the same reconciliation closes the trade with the submitted order row's exit reason and trigger timestamp instead of later `bybit_position_missing` cleanup.
  - If the open-order snapshot fails, stale pending exits remain pending instead of being inferred flat from incomplete exchange state.

- `current slice: fail closed on entry reconcile position outages`
  - The event entry loop no longer calls `get_positions()` directly while reconciling open ledger trades.
  - Position snapshot failures during open-trade reconciliation now leave the ledger unchanged, record `position_report_error`, and skip all new submit-mode entries for that cycle.
  - This closes the case where a Bybit position API outage with an existing open ledger trade could crash the cycle before writing the fail-closed entry report.

- `current slice: fail closed on wallet equity outages`
  - The event entry loop now reads wallet equity through a safe wrapper instead of letting a wallet API outage crash the cycle before pending fills, exits, or reports run.
  - In submit mode, a wallet equity snapshot error records `position_report_error`, uses fallback equity only for cycle telemetry, and skips all new entry candidates for that cycle.
  - Existing open trades can still submit reduce-only exits while the wallet endpoint is unavailable.

- `current slice: clear stale flat pending entries`
  - Stale unconfirmed entry order rows are now terminalized as `expired_unconfirmed` only when successful Bybit position and open-order snapshots show no live position and no active non-reduce-only order for that symbol.
  - The event entry loop records `stale_pending_entry_orders_terminalized` in cycle telemetry.
  - The websocket risk watchdog applies the same cleanup during bootstrap and REST reconciliation; stale entries with a live position, live entry order, or snapshot failure remain untouched.

- `current slice: prioritize risk exits before stop repair`
  - Websocket-risk bootstrap and REST reconciliation now evaluate tracked-position stop/take-profit/max-hold exits before attempting exchange-native stop repair.
  - Stop repair skips symbols with pending local exit submissions or live AGC reduce-only exit orders.
  - This prevents a stale-WebSocket fallback or restart from spending a REST call on stop repair for a position that should be flattened immediately.

- `current slice: recover failed websocket order acks`
  - If a WebSocket Trade order ack rejects an exit, the watchdog marks the WS order row `rejected` instead of leaving it `submitted_unconfirmed`.
  - In `ws_then_rest` mode with REST fallback enabled, the watchdog immediately submits one REST reduce-only fallback from the rejected WS order's ledger context.
  - Confirmed REST risk exits now write `status=filled` order rows, so closed trades are not paired with merely `submitted` exit orders.

## Verification

Local:

- Focused streamed/pending exit ledger tests passed after the streamed exit context change.
- Focused stale flat pending-exit tests passed after the live stale ledger row finding.
- Focused event-demo position outage tests passed after the open-trade reconciliation guard.
- Focused event-demo wallet outage tests passed after the wallet equity guard.
- Focused stale pending-entry terminalization tests passed after the flat-entry cleanup.
- Focused websocket-risk exit-before-stop-repair tests passed after the risk-priority cleanup.
- Focused failed websocket order-ack fallback tests passed after the rejected-ack recovery change.
- `tests/test_aggression_carry_event_demo.py tests/test_aggression_carry_ws_risk.py`: 90 passed after the flat-entry cleanup.
- `tests/test_aggression_carry_ws_risk.py`: 40 passed after the risk-priority cleanup.
- `pytest -q`: 204 passed after the risk-priority cleanup.
- `tests/test_aggression_carry_event_demo.py tests/test_aggression_carry_ws_risk.py`: 95 passed after the failed-ack recovery change.
- `pytest -q`: 207 passed after the failed-ack recovery change.

VPS:

- Focused event-demo file: 50 passed after deploying the wallet equity guard.
- Focused event-demo + websocket-risk files: 90 passed after deploying the flat-entry cleanup.
- Focused websocket-risk file: 40 passed after deploying the risk-priority cleanup.
- `pytest -q`: 204 passed after deploying the risk-priority cleanup.
- Focused websocket-risk file: 36 passed after deploying the snapshot-failure guard.
- Focused event-demo + websocket-risk files: 95 passed after deploying the failed-ack recovery change.
- `pytest -q`: 207 passed after deploying the failed-ack recovery change.
- Services after restart:
  - `model050426-bybit-demo.service`: active/running, `INTERVAL_SECONDS=300`.
  - `model050426-bybit-risk.service`: active/running.
  - Direct live state after the risk-priority cleanup deployment: `active_positions=0`, `open_orders=0`, `ledger_open_trades=0`, `ledger_pending_orders=0`, latest websocket-risk reason `startup`, `risk_stop_repairs=0`, and latest demo mode `submit` with `demo_entries=0`.
- Services after failed-ack recovery restart:
  - `model050426-bybit-demo.service`: active/running.
  - `model050426-bybit-risk.service`: active/running.
  - Direct live state: `active_positions=0`, `open_orders=0`, `ledger_open_trades=0`, `ledger_pending_orders=0`, latest websocket-risk reason `startup`, `risk_positions=0`, `risk_ledger=0`, `risk_untracked=0`, `risk_live_exit_open_orders=0`, `risk_stop_repairs=0`, `risk_error=""`, latest demo mode `submit`, `demo_entries=0`, and `demo_error=""`.
- Failed websocket order-ack drill:
  - In isolated VPS `ws_then_rest` mode, a rejected async WebSocket Trade ack marked the original WS row `rejected`, submitted exactly one REST reduce-only fallback, recorded the fallback order `filled`, closed the trade with `exit_reason=stop_loss`, preserved the original trigger timestamp, and cleared the pending-submission guard.
  - In isolated VPS pure `ws` mode without REST fallback, the same rejected ack marked the WS row `rejected`, submitted `0` REST orders, left the trade open, and cleared the pending-submission guard for later recovery.
- Risk exit before stop-repair drills:
  - An isolated VPS websocket-risk bootstrap with an open ledger short, missing exchange stop, and mark already beyond the ledger stop submitted exactly one reduce-only exit, closed the trade with `exit_reason=stop_loss`, sent `0` stop repairs, and cleared the stale position snapshot from engine state.
  - An isolated VPS websocket-risk bootstrap with a pending `agc-ex-*` exit and missing exchange stop submitted `0` new exits, sent `0` stop repairs, and kept the symbol in the pending-submission guard.
- Stale flat pending-entry cleanup:
  - An isolated VPS event-demo cycle with a stale `agc-en-*` entry row, no Bybit position, and no Bybit open order marked the row `expired_unconfirmed`, recorded `stale_pending_entry_orders_terminalized=1`, and made no trade-history calls.
  - The same isolated event-demo cycle with a live non-reduce-only open order for the symbol left the stale row `submitted_unconfirmed`.
  - An isolated VPS websocket-risk bootstrap with a stale entry row and flat exchange state marked the row `expired_unconfirmed` and submitted `0` orders.
  - The same websocket-risk bootstrap with a live non-reduce-only open order left the stale row `submitted_unconfirmed` and submitted `0` orders.
- Entry wallet-outage drill:
  - An isolated VPS event-demo cycle with a forced entry candidate and failing Bybit wallet API submitted `0` orders, left `event_demo_orders` empty, recorded `skipped_wallet_snapshot_error=1`, and wrote `position_report_error="wallet equity unavailable: wallet unavailable"`.
  - An isolated VPS event-demo cycle with an existing open ledger trade, a forced entry candidate, and the same wallet outage submitted one reduce-only `agc-ex-*` exit, filled it, closed the trade, skipped the new entry, and recorded the same wallet error.
- Entry position-outage drill:
  - An isolated VPS event-demo cycle with an existing open ledger trade and a failing Bybit position API wrote a report, kept the existing trade open, skipped the forced new entry, submitted `0` orders, and recorded `position_report_error="positions unavailable"`.
- Stale flat pending-exit cleanup:
  - Live verification found stale local `agc-ux-BTC-tf5z45-0` with `status=submitted_unconfirmed` while Bybit had `active_positions=0` and `open_orders=0`.
  - After deployment and risk-service restart, the row was `status=filled`, `filled_qty=0.001`, `error="filled inferred from flat Bybit position"`, and direct state was `ledger_pending_orders=0`.
  - Isolated VPS drills confirmed stale untracked and stale tracked pending exits terminalize with `0` new orders when the exchange is flat; tracked trades preserve `exit_reason=stop_loss` and the original trigger timestamp.
  - A snapshot-failure drill confirmed stale pending exits remain `submitted_unconfirmed` when the Bybit open-order snapshot fails.
- Streamed ledger-context drill:
  - Isolated VPS execution-stream and order-stream closures both closed the trade with `exit_reason=stop_loss` and preserved the submitted order row's trigger timestamp.
- Risk live-open-exit guard drill:
  - An isolated tracked-position stop breach with a live `agc-ex-*` reduce-only open order submitted `0` duplicate exits and recorded `bybit_live_exit_open_orders=1`.
  - An isolated untracked-position restart with a live `agc-ux-*` reduce-only open order submitted `0` duplicate exits and recorded `bybit_live_exit_open_orders=1`.
  - A manual reduce-only open order did not suppress an emergency stop exit.
- Entry live-open-order guard drill:
  - An isolated cycle with a forced entry candidate and a live non-reduce-only open order for the same symbol submitted `0` entry orders and recorded `skipped_live_open_entry_order=1`.
  - An isolated submit-mode cycle with an open-order snapshot error submitted `0` entry orders and recorded `skipped_open_order_snapshot_error=1`.
  - A helper drill confirmed event exits are blocked by own AGC reduce-only exit links, while manual/native reduce-only protection links do not suppress an event exit.
- Entry live-position guard drill:
  - An isolated cycle with a forced entry candidate and a live Bybit position for the same symbol submitted `0` entry orders and recorded `skipped_live_position_entry=1`.
  - An isolated submit-mode cycle with a position-snapshot error submitted `0` entry orders and recorded `skipped_position_snapshot_error=1`.
- Pending untracked-exit restart drill:
  - An isolated VPS data root with a fresh pending `untracked_position` reduce-only order and a still-open Bybit position loaded the existing order on bootstrap.
  - The restarted risk engine submitted `0` duplicate orders and kept `AAAUSDT` in the pending-submission guard.
- Telegram de-dupe drill:
  - An isolated VPS restart simulation sent the first material websocket-risk alert.
  - A second engine instance using the same isolated report directory suppressed the same alert as `duplicate_material_event`.
  - The persisted `telegram_dedupe_keys.json` contained the material alert key.
  - A repeated stop-repair report with the same symbol and target stop/TP but a different synthetic order-link ID was suppressed as `duplicate_material_event`.
  - A changed stop target still alerted; the isolated stop-repair drill sent 2 alerts across 3 reports.
- Lock recovery drill:
  - An isolated empty `event_ws_risk_cycle.lock` with `stale_seconds=0` was recovered on the VPS.
  - The lock existed while held by the new owner PID and was removed after release.
- Websocket risk report evidence:
  - latest heartbeat report path: `data/bybit-demo-event/reports/event-risk-ws/latest_event_ws_risk_cycle.md`.
  - startup snapshot persisted as a timestamped `event_ws_risk_cycle_ws-risk-*.json`/`.md` pair.
- Forward-demo cache evidence:
  - first post-deploy cycle seeded `159648` kline rows for `148` symbols.
  - next scheduled cycle reported `kline_fetch_symbols=0` and `kline_fetched_rows=0`.
- Post-deploy state checks:
  - `active_positions=0`
  - `open_orders=0`
  - `open_ledger=0`
  - latest reports were quiet with `telegram_sent=false` and `quiet_no_material_event`.

Latency probe:

- Two-order BTCUSDT demo probe before final cleanup:
  - place: 288.1 ms, 171.2 ms
  - cancel: 174.4 ms, 171.6 ms
- One-order BTCUSDT demo probe after final cleanup:
  - place: 227.4 ms
  - cancel: 171.1 ms
- Post-probe state: `active_positions=0`, `open_orders=0`, `open_ledger=0`.

## WebSocket Latency Interpretation

Bybit demo currently supports demo private websocket streams, but demo WebSocket Trade order entry is unavailable. The VPS therefore runs `ORDER_SUBMIT_MODE=ws_then_rest`: websocket first for market/position/order/execution state, REST fallback for actual demo reduce-only order submission. In this setup, websocket is the fast detection path, not the final demo order transport.

The observed demo REST order path, around 170-290 ms for place/cancel from the VPS, is plausible. If a websocket timing test appears slower than REST, the likely causes are startup/subscription handshake timing, pybit callback dispatch, demo endpoint behavior, or measuring a path that falls back to REST. It should not be interpreted as production mainnet WebSocket Trade latency evidence.

## Remaining Risks

- Bybit demo is not mainnet. Demo liquidity, latency, and WebSocket Trade limitations do not prove real-money execution quality.
- The event entry loop still rebuilds features from a 45-day window, but it now avoids redownloading the whole window every cycle by caching forward-demo 1h bars.
- Native exchange stop/take-profit remains the primary emergency protection. The local websocket risk engine is the repair/enforcement layer, not a substitute for venue-native stops.
- Telegram is notification-only. It must not become an order approval or order submission path.
