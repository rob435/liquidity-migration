# Continuous Fade DSR/PBO Diagnostic

Date: 2026-06-28.

## Question

Do the existing frozen full-portfolio replay variants show obvious
multiple-testing or train/test selection fragility under DSR/PBO-style
diagnostics?

## Method

Use only artifacts already written under
`research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/`.
Do not run new strategy variants and do not touch the PIT roots.

Variant universe:

- `timing_portfolio_replay.csv`
- `stop_portfolio_replay.csv`
- `regime_portfolio_replay.csv`
- `skip_portfolio_replay.csv`
- `scale_in_portfolio_replay.csv`

For each venue, collapse duplicate `baseline_current` rows and load each
variant's `continuous_equity.csv`.

Metrics:

- Deflated Sharpe probability for each replay variant using daily returns,
  observed skew/kurtosis, and the observed full-replay trial Sharpe
  distribution for that venue.
- CSCV-style PBO over daily returns using contiguous date partitions. Each
  split selects the highest in-sample Sharpe variant and ranks it by
  out-of-sample Sharpe.

Interpretation:

- This is inference-risk evidence only, not alpha, OOS, promotion, or live-size
  evidence.
- The forward demo/paper ledgers remain the only pristine OOS surface.
- A poor result is bearish for trusting the internal replay surface. A good
  result does not override the existing forward-sample and deploy-alignment
  blockers.

## Result

Command:

```powershell
.venv\Scripts\python.exe scripts\continuous_overfit_diagnostic.py
```

Artifacts:

- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/overfit_variant_universe.csv`
- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/deflated_sharpe.csv`
- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/pbo_cscv_summary.csv`
- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/pbo_cscv_splits.csv`
- refreshed `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/reports/final_research_report.md`

Summary:

| Venue | Full-replay variants | Common daily observations | PBO | Median OOS rank | Baseline DSR probability | Best Sharpe variant | Best Sharpe DSR probability | Verdict |
| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | --- |
| Bybit | 21 | 284 | 41.43% | 50.00% | 23.17% | `mae05_add25` | 24.03% | `fragile_internal_surface` |
| Binance | 21 | 251 | 35.71% | 71.43% | 20.08% | `skip_btc_tail_035` | 28.77% | `fragile_internal_surface` |

The best-Sharpe variants are not deployable positives: `mae05_add25` was
already rejected by the scale-in portfolio replay because MAR/drawdown worsened
on both venues, and `skip_btc_tail_035` was already rejected by the two-venue
rule because Bybit MAR/drawdown worsened despite Binance improvement.

## Verdict

Label: `exploratory`.

The frozen internal replay surface is fragile under this DSR/PBO diagnostic.
This does not invalidate the baseline mechanics, but it is direct evidence
against trusting internal Sharpe/MAR rankings as deployment proof. Keep the
existing recommendation: no continuous skip/tail/scale-in/invalidation change
should influence deployment without forward paper/demo OOS evidence, and no
size increase should happen from these internal diagnostics.
