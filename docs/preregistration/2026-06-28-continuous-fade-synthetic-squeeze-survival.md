# Continuous Fade Synthetic Squeeze Survival - 2026-06-28

## Experiment

Execute the synthetic squeeze survival diagnostic from
`docs/preregistration/continuous_fade_research_plan.md` sections 12.2 and
14.3 against the frozen
`continuous_ensemble_v2_baseline_current` validation run.

This is offline research only. No live orders, production credentials, mainnet
mode, or live guard changes were used.

## Data And Method

- Run root:
  `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/`
- Inputs:
  - `tables/trades_enriched.parquet`
  - per-venue `continuous_equity.csv`
- Venues: Bybit and Binance full-PIT roots.
- Run label: `exploratory`.

The diagnostic reconstructs active positions from baseline entry/exit
timestamps, sums active notional by symbol, injects deterministic squeeze
events at median, p95, and worst active-book placements, and measures
post-event equity, drawdown, recovery time, margin usage proxy, and remaining
account-level liquidation distance.

Assumptions are intentionally explicit: this is an instant-shock active-book
calculation with simple hedge-credit and maintenance-margin assumptions. It is
not an exchange liquidation engine, order-book gap simulator, or stochastic
risk-of-ruin bootstrap.

## Scenarios

- One active coin +50%.
- One active coin +100%.
- Three active coins +50%.
- Five active coins +30%.
- All active shorts +30% while BTC/ETH hedge legs are credited with +10%.
- One active coin +100% with 1h exchange outage and 10% extra exit damage.
- One active coin +100% with risk-daemon failure and 20% extra exit damage.

## Worst Active Results

| Venue | Scenario | Event date | Net loss | Post-event DD | Days to recovery | Final return after shock | Survives 50% ruin bar |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| Bybit | one_coin_100pct | 2023-10-03 | 3.12% | -3.04% | 168.6 | 23.53% | yes |
| Bybit | three_coins_50pct | 2024-02-21 | 3.28% | -3.97% | 258.6 | 23.36% | yes |
| Bybit | btc10_alts30 | 2025-08-09 | 2.65% | -2.63% | 79.5 | 24.00% | yes |
| Bybit | exchange_down_1h_one_coin_100pct | 2023-10-03 | 3.43% | -3.35% | 178.6 | 23.21% | yes |
| Binance | one_coin_100pct | 2024-02-13 | 3.15% | -3.48% | 115.6 | 15.69% | yes |
| Binance | three_coins_50pct | 2024-02-12 | 3.49% | -3.63% | 215.3 | 15.35% | yes |
| Binance | btc10_alts30 | 2025-07-11 | 2.69% | -2.71% | 191.4 | 16.15% | yes |
| Binance | exchange_down_1h_one_coin_100pct | 2024-02-13 | 3.46% | -3.78% | 162.6 | 15.37% | yes |

## Verdict

Current sampled sizing survives these deterministic active-book squeeze
diagnostics. That is useful evidence that the book is not obviously oversized
for a single +100% active-name shock or a small correlated active-book shock.

Do not overstate it. The largest drawdown in this table is still several times
the baseline max drawdown, recovery can take most of a year, and the diagnostic
does not model order-book gaps, real exchange liquidation, missing conditional
stops, path drift during an outage, or stochastic clustering. Follow-on
stochastic cluster/bootstrap risk-of-ruin and 5m dynamic outage overlays were
added in `2026-06-28-continuous-fade-cluster-risk-of-ruin.md` and
`2026-06-28-continuous-fade-dynamic-liquidation-outage.md`; they remain
exploratory diagnostics, not live-size approval.

## Artifacts

- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/synthetic_squeeze_survival.csv`
- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/reports/final_research_report.md`
