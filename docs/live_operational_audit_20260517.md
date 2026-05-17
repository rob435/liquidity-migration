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

## Verification

Local:

- `pytest -q`: 189 passed after the live open-order entry guard change.

VPS:

- `pytest -q`: 189 passed after deploying the live open-order entry guard change.
- Services after restart:
  - `model050426-bybit-demo.service`: active/running, `INTERVAL_SECONDS=300`.
  - `model050426-bybit-risk.service`: active/running.
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
