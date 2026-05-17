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

## Verification

Local:

- `pytest -q`: 174 passed after the runtime cadence/lock changes.

VPS:

- `pytest -q`: 174 passed after deployment.
- Services after restart:
  - `model050426-bybit-demo.service`: active/running, `INTERVAL_SECONDS=300`.
  - `model050426-bybit-risk.service`: active/running.
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
- The event entry loop still rebuilds a large 45-day, multi-symbol feature set. The 5-minute cadence reduces pressure, but a cached incremental data path would be cleaner.
- Native exchange stop/take-profit remains the primary emergency protection. The local websocket risk engine is the repair/enforcement layer, not a substitute for venue-native stops.
- Telegram is notification-only. It must not become an order approval or order submission path.
