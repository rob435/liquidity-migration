# System Status

Current as of 2026-05-17.

## Active System

- Strategy: full-PIT liquidity-migration short, `union_pathology` crowding veto.
- Strategy id: `liqmig_union_q40_h3_tp25_g100_crowd_union`.
- Gross exposure: `1.00`, split across `5` max active symbols.
- Per-entry target: `20.00%` of current Bybit demo USDT equity.
- Entry service: `model050426-bybit-demo.service`.
- Risk service: `model050426-bybit-risk.service`.
- Operational canary: `model050426-bybit-canary.timer`.
- Venue mode: Bybit demo only. `demo=False` is still refused by the private client.
- Paper shadow was intentionally skipped by user decision; keep the risk contained to demo-only trading.

## Promoted Evidence

Promoted report:

```text
data/research_reports/frontier_union_crowding_promoted_20260517
```

Full-PIT result on `2023-05-03` to `2026-05-03`:

```text
trades: 444
total return: +2143.28%
max drawdown: -11.05%
max no-new-high stretch: 51 days
worst 90d return: -4.80%
worst split return: +118.65%
average split Sharpe-like: 3.72
OOS return: +186.06%
promotion gate: pass
```

This is a `1.00` gross exposure rescale of the promoted full-PIT ledger. Candidate selection, exits, cooldowns, and crowding decisions are unchanged by the gross cleanup.

## Live Demo Proof

The demo execution path has been tested on the VPS with the production entry helper, production reduce-only risk helper, native stop/take-profit attachment, and cleanup back to flat state.

BTCUSDT strategy-sized short proof:

```text
entry qty: 0.025 BTC
notional: 1952.7075 USDT
equity share: 19.53%
native stop loss: 87481.3
native take profit: 58581.2
entry execution history visible after: 181.628 ms
reduce-only exit history visible after: 1925.515 ms
final positions: 0
final open orders: 0
```

DOGEUSDT strategy-sized short proof:

```text
entry qty: 18161 DOGE
notional: 1998.61805 USDT
equity share: 20.00%
native stop loss: 0.12326
native take profit: 0.08253
entry execution history visible after: 609.757 ms
reduce-only exit history visible after: 3201.353 ms
final positions: 0
final open orders: 0
```

Bybit demo WebSocket Trade order entry was attempted and was unavailable or rejected at `wss://stream-demo.bybit.com/v5/trade`. The deployed risk path remains `ORDER_SUBMIT_MODE=ws_then_rest`: WebSocket-first for state and exit decisions, REST fallback for demo order submission.

## Deployed State

Last verified VPS state before this cleanup:

```text
path: /opt/MODEL050426
branch: main
services: model050426-bybit-demo.service, model050426-bybit-risk.service
service restarts: 0
live positions after proof: 0
open orders after proof: 0
```

The VPS entry service intentionally runs at `INTERVAL_SECONDS=60`. Fast exits
are still handled by the separate websocket risk service; the one-minute entry
cadence is for quicker stale-order, report, and candidate-state refresh.

The canary timer can run every 30 minutes to provide order-path evidence between
sparse strategy signals. It is isolated under `reports/demo-canary` and does not
write strategy ledgers.

## Known Limits

- This proves demo execution plumbing, not future profitability.
- This does not prove real-money venue behavior.
- Funding is not part of the active demo gate yet. The research notes show funding can materially affect the short strategy.
- Adverse hourly stop-fill stress is still the hardest negative result for the older baseline; do not use exact-stop fills as sole promotion evidence for future variants.
