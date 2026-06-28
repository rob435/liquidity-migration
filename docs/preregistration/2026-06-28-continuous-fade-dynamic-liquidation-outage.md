# Continuous Fade Dynamic Liquidation / Outage Diagnostic

Date: 2026-06-28

Run label: exploratory.

Real money: no. Offline research only.

## Hypothesis

The active-book squeeze table and cluster bootstrap show current tiny sizing
survives deterministic shocks, but they still treat shocks as instant scalar
losses. The open gap is whether an adverse move during an exchange/risk-daemon
outage could push the account through a simple maintenance-margin proxy before a
disaster stop can flatten.

## Method

Inputs:

- `tables/trades_enriched.parquet`
- `tables/synthetic_squeeze_survival.csv`
- manifest-gated `klines_5m` from `~/SHARED_DATA/{bybit,binance}_full_pit`

For each existing synthetic squeeze survival placement (`median_active`,
`p95_active`, `worst_active`) and scenario, reconstruct the active book at
`event_ts_ms`, identify the hit symbols using the same active-symbol exposure
ordering, and overlay the synthetic shock onto the actual post-event 5m path.

For each hit symbol:

1. Use the last 5m close at or before the event as the reference price.
2. Apply the scenario's synthetic shock immediately.
3. During outage/stop delay, use the actual 5m high to estimate peak adverse
   mark-to-market.
4. At the first 5m close after the delay, estimate flatten loss plus the
   scenario's extra stop-market/slippage damage.

Account-level maintenance-margin proxy:

```text
maintenance_margin = active_short_notional * 0.5%
account_liquidated = equity_before - dynamic_peak_loss <= maintenance_margin
```

The diagnostic writes both peak-loss and flatten-loss rows. Missing 5m path
coverage is explicitly flagged; missing-symbol exposures fall back to the static
shock and are not treated as proof of safety.

## Scenarios

Reuse the existing synthetic squeeze survival scenarios:

- one coin +50%
- one coin +100%
- three coins +50%
- five coins +30%
- all active shorts +30% with BTC/ETH +10% hedge credit
- one coin +100% with one-hour exchange outage and 10% extra exit damage
- one coin +100% with risk-daemon failure / two-hour delay and 20% extra exit
  damage

## Decision Rule

This cannot approve live size. It can only:

- flag the current sizing as unsafe if any selected placement crosses the
  account-level maintenance proxy or if peak dynamic drawdown is materially
  larger than the static survival table;
- support continued tiny-size demo/paper observation if all selected placements
  stay far from account-level liquidation and 5m coverage is adequate.

The remaining live design still needs real disaster-stop placement/repair checks
and forward demo/paper reconciliation.

## Outcome

Command:

```powershell
.venv\Scripts\python.exe scripts\continuous_dynamic_tail.py
```

Artifact:

- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/dynamic_liquidation_outage.csv`

The run wrote 42 rows: 21 Bybit and 21 Binance. All rows had complete 5m path
coverage and no row crossed the account-level maintenance-margin proxy.

| Venue | Worst peak scenario | Peak net loss | Peak DD | Max flatten loss scenario | Max flatten loss | Maintenance-proxy liquidations |
| --- | --- | ---: | ---: | --- | ---: | ---: |
| Bybit | `three_coins_50pct/worst_active` | 3.33% | -4.02% | `risk_daemon_down_one_coin_100pct/worst_active` | 3.59% | 0/21 |
| Binance | `three_coins_50pct/worst_active` | 3.56% | -3.70% | `risk_daemon_down_one_coin_100pct/worst_active` | 3.68% | 0/21 |

Verdict: current sampled sizing survives this 5m path-overlay diagnostic. That
supports continued tiny-size demo/paper observation, not a size increase. The
result is still exploratory and does not replace exchange liquidation modeling,
order-book gap modeling, live disaster-stop placement/repair checks, or forward
demo/paper evidence.

## Known Limits

This is not an exchange liquidation engine. It does not model order-book depth,
liquidation queues, maintenance-tier ladders, conditional-order rejection,
partial fills, insurance fund behavior, bankruptcy price mechanics, or real
venue outage telemetry. It is a path-aware diagnostic over the spent validation
tape.
