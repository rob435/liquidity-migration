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

## Strategy-Sized Proof Tests

After cleaning the VPS checkout, ran `scripts/prove_bybit_demo_order_lifecycle.py`
with the autonomous demo/risk services stopped and restarted by shell trap.
The script refuses to start if any live position or open order exists, uses
the production entry helper for a forced short lifecycle, verifies native
exchange stop/take-profit fields, exits through the production reduce-only
risk helper, and always attempts symbol cleanup before writing its report.

BTCUSDT proof:

- Artifact: `data/bybit-demo-event/reports/system-audit/prove_unproven_20260517T182940Z.json`.
- Entry: strategy-sized demo short, `0.025 BTC`, notional `1952.7075 USDT`, `19.53%` of equity.
- Entry status: filled; native stop loss `87481.3`, native take profit `58581.2`.
- Entry execution history was visible after `181.628ms`.
- Exit: production reduce-only market helper; execution history was visible after `1925.515ms`.
- Final state: `0` positions, `0` open orders.

DOGEUSDT proof:

- Artifact: `data/bybit-demo-event/reports/system-audit/prove_unproven_20260517T183044Z.json`.
- Entry: strategy-sized demo short, `18161 DOGE`, notional `1998.61805 USDT`, `20.00%` of equity.
- Entry status: filled; native stop loss `0.12326`, native take profit `0.08253`.
- Entry execution history was visible after `609.757ms`.
- Exit: production reduce-only market helper; execution history was visible after `3201.353ms`.
- Final state: `0` positions, `0` open orders.

WebSocket Trade proof:

- Attempted demo WebSocket Trade order entry with a far post-only BTC order.
- Result: `unavailable_or_rejected`; Bybit demo WebSocket Trade auth failed at `wss://stream-demo.bybit.com/v5/trade`.
- Decision: keep `ORDER_SUBMIT_MODE=ws_then_rest`; the risk decision path remains WebSocket-first, but demo order submission must continue using REST fallback.

## Findings

1. Pass: demo auth, private REST order placement, cancellation, market entry, attached protection, reduce-only exit, delayed execution history, and cleanup all worked on the Bybit demo account.
2. Pass: live services recovered cleanly after controlled stop/start and stayed active with `NRestarts=0`.
3. Fixed: `scripts/probe_bybit_demo_order_latency.py` sized by `minOrderQty` only and failed cheap symbols below Bybit's 5 USDT minimum. It now respects `minNotionalValue`; the DOGE buy/sell post-only probes pass after the fix.
4. Watch: immediate trade-history lookup for the reduce-only exit returned empty, while delayed lookup confirmed the fill and the position was already flat. The system's reconciliation path covers this, but immediate reports can briefly show `submitted_unconfirmed`.
5. Watch: risk service reports `ws_order_unavailable` and uses REST fallback for demo reduce-only exits. This is acceptable for the current deployment because the fallback path was live-tested, but it should stay visible in monitoring.
6. Pass: strategy-sized short lifecycle was live-tested on BTCUSDT and DOGEUSDT at roughly 20% equity per entry. Both attached native protection, exited reduce-only, and ended flat.
7. Low severity: manual service stops emitted one systemd warning, `Failed to kill control group ... ignoring: Invalid argument`. Services still stopped, restarted, and ran normally.

## Decision

The deployed demo system is operational after this audit. The order path is safe for demo trading, strategy-sized demo order behavior has been tested on both BTC and DOGE, the account is flat after testing, and the active strategy configuration matches the promoted union liquidity-migration system.

This still does not prove future profitability or real-money venue behavior.
It proves the demo execution and risk plumbing far more strongly than the
initial tiny-order audit did.
