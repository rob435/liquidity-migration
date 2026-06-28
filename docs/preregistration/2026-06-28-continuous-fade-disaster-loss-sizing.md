# Continuous Fade Disaster-Loss Sizing Diagnostic

Date: 2026-06-28

Run label: exploratory.

Real money: no. Offline research only.

## Hypothesis

The continuous fade book may survive current synthetic shocks because current
sampled notional is tiny, but the position sizing should still be checked
against explicit loss-at-disaster budgets. If current per-trade notional is
larger than a disaster-budgeted safe notional under plausible catastrophic moves,
the system is implicitly relying on disasters not happening.

## Method

Inputs:

- `tables/trades_enriched.parquet`
- current portfolio notional per component trade:
  `notional_weight * component_weight`
- observed path adverse excursion from the validation replay

For each venue and component trade, compute:

```text
catastrophic_move_pct = max(fixed_floor, empirical_tail_proxy)
safe_notional_pct_equity = trade_loss_budget_pct_equity / catastrophic_move_pct
current_to_safe_notional = current_portfolio_notional / safe_notional_pct_equity
```

Scenarios:

- fixed +50% adverse move
- fixed +100% adverse move
- max(+50%, venue winner-MAE p95)
- max(+100%, venue all-trade MAE p99)

Loss budgets:

- 0.05% equity per trade
- 0.10% equity per trade
- 0.25% equity per trade

## Decision Rule

This cannot approve size. It can only:

- flag current sizing as disaster-budget-inconsistent when a material share of
  trades has `current_to_safe_notional > 1`;
- support current tiny-size observation when current notional is already below
  conservative disaster budgets.

The result is not a stop-placement test, exchange liquidation model, or live
kill-switch proof.

## Outcome

Command:

```powershell
.venv\Scripts\python.exe scripts\continuous_disaster_sizing.py
```

Artifacts:

- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/disaster_sizing_by_trade.csv`
- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/disaster_sizing_summary.csv`

At a fixed +100% adverse move and 0.10% equity per-trade disaster-loss budget,
most current component trades are above the budgeted safe notional.

| Venue | Safe notional at +100% / 0.10% budget | Trades over budget | Median current/safe | P95 current/safe | Max current/safe |
| --- | ---: | ---: | ---: | ---: | ---: |
| Bybit | 0.10% equity | 97.34% | 3.88x | 7.90x | 13.86x |
| Binance | 0.10% equity | 97.44% | 4.17x | 8.34x | 14.00x |

At a looser 0.25% equity budget:

| Venue | Scenario | Trades over budget | Median current/safe | P95 current/safe |
| --- | --- | ---: | ---: | ---: |
| Bybit | fixed +100% | 77.14% | 1.55x | 3.16x |
| Binance | fixed +100% | 78.53% | 1.67x | 3.34x |
| Bybit | fixed +50% | 27.67% | 0.78x | 1.58x |
| Binance | fixed +50% | 31.60% | 0.83x | 1.67x |

The empirical proxies did not exceed the fixed floors in this validation tape:
winner-MAE p95 was 18.24% Bybit / 16.92% Binance, and all-trade MAE p99 was
70.72% / 76.36%. The +50% and +100% fixed floors therefore drove the reported
catastrophic moves.

Verdict: current sizing is not disaster-budget-clean under a strict
per-position loss budget. This does not contradict the active-book survival and
dynamic outage diagnostics; it says any future size increase needs an explicit
loss-at-disaster cap, and even current sizing is larger than a 0.10% per-trade
budget under +100% adverse moves. No live-size increase is supported.

## Known Limits

The empirical tail proxies come from realized validation paths, not order-book
depth, external news jumps, bankruptcy/liquidation mechanics, or unavailable
OI/depth features. The diagnostic is per-trade; same-signal portfolio heat is
covered separately by `portfolio_heat_by_entry_cluster.csv` and the synthetic
active-book squeeze tables.
