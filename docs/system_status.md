# System Status

Current as of 2026-05-18.

## Active System

- Research default: full-PIT liquidity-migration short, `union_pathology` crowding veto.
- Entry policy: `promoted_quality_squeeze` conservative router.
- Promoted research strategy id: `liqmig_union_q40_h3_tp25_g100_qsqueeze`.
- Live demo entry profile: `observe`, strategy id `observe_liqmig_q40_h3_tp25_g100_relaxed_qsqueeze`.
- Gross exposure: `1.00`, split across `10` max active symbols in observe mode.
- Per-entry target: `10.00%` of current Bybit demo USDT equity in observe mode.
- Entry service: `model050426-bybit-demo.service`.
- Risk service: `model050426-bybit-risk.service`.
- Venue mode: Bybit demo only. `demo=False` is still refused by the private client.
- Paper shadow was intentionally skipped by user decision; keep the risk contained to demo-only trading.

## Promoted Evidence

Promoted report:

```text
/Users/jhbvdnsbkvnsd/agc-bybit-fullpit-funded-20230503-20260503/reports/entry_signal_cross_strategy_20260517/quality_tier_stress/quality_tier_stress_report.md
```

Full-PIT exact-stop, 3x-cost, no-funding result on `2023-05-03` to `2026-05-03`:

```text
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

Funding stress on the same conservative router produced 444 trades, +1853.99%
total return, -13.72% max drawdown, -6.29% worst 90d, +122.17% worst split,
and +175.32% OOS. Candidate selection, exits, cooldowns, gross exposure, and
crowding decisions are unchanged; only entry timing for promoted-grade squeeze
bars changed.

## Observe-Mode Evidence

The active VPS entry service is intentionally configured for a higher-frequency
demo-only observation profile. This profile is not the promoted research
default. It lowers the entry gates while keeping the same short
liquidity-migration premise, fixed exits, stop/loss throttles, and
`union_pathology` same-hour crowding veto. It uses the same conservative
`promoted_quality_squeeze` router for promoted-grade events and normal 1h entry
for lower-tier observe-only events.

Full-PIT funded stress report:

```text
/Users/jhbvdnsbkvnsd/agc-bybit-fullpit-funded-20230503-20260503/reports/entry_signal_cross_strategy_20260517/quality_tier_stress/quality_tier_stress_report.md
```

```text
trades: 1268
candidate events: 1603
total return: +221.29%
max drawdown: -21.32%
worst 90d return: -18.90%
train return: +12.36%
validation return: +17.02%
OOS return: +142.92%
average split Sharpe-like: 1.04
promotion gate: pass
```

Observe-mode relaxed gates:

```text
PIT liquidity rank: 11-260
current 24h turnover floor: disabled
rank improvement: >= 80
turnover expansion: >= 3.0
daily return floor: >= -3%
residual return floor: >= +3%
close-location floor: >= 0.25
max active symbols: 10
symbol cooldown: 2 days
crowding veto: union_pathology kept
```

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

The VPS entry service intentionally runs at `INTERVAL_SECONDS=60` and
`STRATEGY_PROFILE=observe`. Fast exits are still handled by the separate
websocket risk service; the one-minute entry cadence is for quicker stale-order,
report, and candidate-state refresh.

## Known Limits

- This proves demo execution plumbing, not future profitability.
- This does not prove real-money venue behavior.
- Funding is not part of the active demo gate yet. The research notes show funding can materially affect the short strategy.
- Adverse hourly stop-fill stress is still the hardest negative result for the older baseline; do not use exact-stop fills as sole promotion evidence for future variants.
