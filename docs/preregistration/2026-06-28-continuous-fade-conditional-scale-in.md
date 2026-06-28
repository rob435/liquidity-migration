# Continuous Fade Conditional Scale-In Diagnostic

Date: 2026-06-28.

## Question

Does adding short exposure after a trade has already moved adversely look
directionally useful on the frozen continuous validation tape?

## Method

Command:

```powershell
.venv\Scripts\python.exe scripts\continuous_scale_in_diagnostic.py
```

Inputs:

- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/trades_enriched.parquet`
- frozen run metadata in `run_metadata.json`

The diagnostic tests add-on shorts at MAE thresholds of 5%, 10%, 20%, and 40%,
with add-on size equal to 25% or 50% of the original primary notional. It assumes
the add-on fills exactly at the adverse threshold, applies 15 bps extra round
trip cost, and exits the add-on at the original trade exit.

This is not a full component+hedge portfolio replay and does not model order
book depth, margin coupling, or changed hedge sizing.

## Artifacts

- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/conditional_scale_in_by_trade.csv`
- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/conditional_scale_in_summary.csv`
- refreshed `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/reports/final_research_report.md`

## Result

Best diagnostic arm:

| Venue | Trigger | Add-on | Fill rate | Primary component net | Combined component net |
| --- | ---: | ---: | ---: | ---: | ---: |
| Bybit | 5% MAE | 50% of primary | 54.63% | 20.89% | 29.85% |
| Binance | 5% MAE | 50% of primary | 53.21% | 14.69% | 20.84% |

The diagnostic is directionally positive on both venues, but it is leverage-like
and path-conditioned. It should not be promoted or wired live from this result.

## Verdict

Label: `exploratory`.

Recommendation: treat conditional scale-in as a mechanism lead only. The next
valid step would be a preregistered full component+hedge portfolio replay with
margin/heat caps, BTC-risk sizing interaction, hedge resizing, and disaster-loss
limits. Do not use this diagnostic to increase live size.
