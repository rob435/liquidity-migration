# Live System Audit - 2026-05-17

Audit timestamp: 2026-05-17T17:00:43Z.
Sizing cleanup verified on the VPS at 2026-05-17T17:54:33Z.

## Scope

Audited the deployed Bybit demo trading system on `root@116.202.15.128:/opt/MODEL050426` and the local source branch `codex/final-union-risk-system`.

This was a demo-account audit only. The private client still refuses `demo=False`.

## Validation

- Local test suite: `219 passed`.
- VPS test suite using `/opt/MODEL050426/.venv`: `219 passed`.
- GitHub CI was missing before this audit; `.github/workflows/ci.yml` now runs offline pytest on push and PR.
- Deployed services after the order tests:
  - `model050426-bybit-demo.service`: enabled, active/running, `NRestarts=0`.
  - `model050426-bybit-risk.service`: enabled, active/running, `NRestarts=0`.

## Live Strategy State

Latest demo cycle:

- Strategy id: `liqmig_union_q40_h3_tp25_g100_crowd_union`.
- Scenario: `liquidity_migration-q40-reversal-h3-s1200-tp2500-c3`.
- Core config: threshold `0.40`, hold `3d`, stop `12%`, take profit `25%`, gross exposure `1.00`, max active symbols `5`.
- Crowding filter: `union_pathology`.
- Current cycle: `symbols=140`, `features=6146`, entries `0/0`, exits `0/0`, open trades `0`.
- Runtime sizing: per-entry notional `20.00%` of equity, target gross `100.00%`, target initial margin `50.00%` at 2x.

Latest websocket risk cycle:

- Mode: `ws_risk_submit`.
- Open ledger trades: `0`.
- Bybit live positions: `0`.
- Untracked positions: `0`.
- Open exit orders: `0`.
- WebSocket order entry unavailable, REST fallback active for demo reduce-only exits.

## Test Orders

Pre-test account state:

- Live positions: `0`.
- Open orders: `0`.
- Wallet equity: `9996.8067461 USDT`.

Post-only place/cancel probe:

- BTC buy probe: `0.001 BTC` at far post-only price, order `c57a26ac-7438-403d-b96f-96c177bb8c34`, place `257.6ms`, cancel `173.1ms`.
- BTC sell probe: `0.001 BTC` at far post-only price, order `4a9f8bfa-bdc5-4d4d-8dba-451132e0b884`, place `255.3ms`, cancel `181.5ms`.

Filled entry and reduce-only exit:

- Symbol: `DOGEUSDT`.
- Entry: market buy `54 DOGE`, order `46c544f7-b6f2-4fe9-bbe9-872730bf4925`, average fill `0.10996`, place `171.495ms`.
- Protection attached and visible after entry: stop loss `0.10447`, take profit `0.11547`.
- Exit: production reduce-only risk helper submitted order `2268af5f-d9de-45e1-b3c3-6a18dd70547f`.
- Delayed execution history confirmed exit fill: `54 DOGE` at `0.10997`, value `5.93838`, fee `0.00326611`.
- Final account state: live positions `0`, open orders `0`.

Audit artifact on VPS:

- `data/bybit-demo-event/reports/system-audit/latest_filled_order_audit.json`.

## Findings

1. Pass: demo auth, private REST order placement, cancellation, market entry, attached protection, reduce-only exit, delayed execution history, and cleanup all worked on the Bybit demo account.
2. Pass: live services recovered cleanly after controlled stop/start and stayed active with `NRestarts=0`.
3. Fixed: `scripts/probe_bybit_demo_order_latency.py` sized by `minOrderQty` only and failed cheap symbols below Bybit's 5 USDT minimum. It now respects `minNotionalValue`; the DOGE buy/sell post-only probes pass after the fix.
4. Watch: immediate trade-history lookup for the reduce-only exit returned empty, while delayed lookup confirmed the fill and the position was already flat. The system's reconciliation path covers this, but immediate reports can briefly show `submitted_unconfirmed`.
5. Watch: risk service reports `ws_order_unavailable` and uses REST fallback for demo reduce-only exits. This is acceptable for the current deployment because the fallback path was live-tested, but it should stay visible in monitoring.
6. Low severity: manual service stops emitted one systemd warning, `Failed to kill control group ... ignoring: Invalid argument`. Services still stopped, restarted, and ran normally.

## Decision

The deployed demo system is operational after this audit. The order path is safe for demo trading, the account is flat after testing, and the active strategy configuration matches the promoted union liquidity-migration system.
