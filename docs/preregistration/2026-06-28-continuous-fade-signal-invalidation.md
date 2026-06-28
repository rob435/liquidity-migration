# Continuous Fade Signal-Invalidation Diagnostic

Date: 2026-06-28.

## Question

Can explicit future same-symbol candidate-tape evidence identify open shorts
whose fade thesis has failed, improving the frozen continuous validation tape?

## Method

Command:

```powershell
.venv\Scripts\python.exe scripts\continuous_signal_invalidation_diagnostic.py
```

Inputs:

- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/trades_enriched.parquet`
- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/signal_table.parquet`
- hourly PIT klines under `~/SHARED_DATA/{bybit,binance}_full_pit`
- causal OI/funding/BTC hourly state where present in the same PIT roots

The simulator is deliberately limited to explicit future candidate rows for the
same venue, component, and symbol while the original trade is still open. It
does not infer invalidation from the absence of a signal row because this is a
sparse candidate tape, not a full hourly feature-state panel.

Rules tested:

- same-symbol candidate pressure after 3h while the open short is losing, with
  component score >= 95%;
- same-symbol candidate pressure after 3h while losing, with score >= 99%;
- same-symbol candidate pressure after 3h while losing, with score >= 95% and
  volume z-score >= 1;
- same-symbol candidate pressure after 6h while losing, with score >= 95%;
- future same-symbol candidate rejected by the BTC-trend gate after 3h while
  losing.

Exit price is the first hourly bar close at or after the candidate row's
`order_submit_ts_ms`, capped inside the original trade window. The diagnostic
keeps the original round-trip cost and prorates the original funding return to
the shortened hold.

This is not a full component+hedge portfolio replay and does not model altered
hedge sizing, live order queueing, stop state, or margin coupling.

## Artifacts

- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/signal_invalidation_by_trade.csv`
- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/signal_invalidation_summary.csv`
- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/signal_invalidation_hourly_state_panel.parquet`
- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/signal_invalidation_state_panel_summary.csv`
- refreshed `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/reports/final_research_report.md`

## Result

Best active diagnostic arm:

| Venue | Rule | Invalidations | Invalidation rate | Baseline component net | Scenario component net | Delta |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Bybit | `candidate_pressure_3h_score99` | 211 | 8.91% | 20.89% | 17.85% | -3.03% |
| Binance | `candidate_pressure_3h_score99` | 127 | 5.90% | 14.69% | 12.87% | -1.83% |

All active candidate-pressure arms reduced component net on both venues. The
BTC-trend rejection arm had zero in-window hits on both venues.

Hourly state-coverage audit:

| Venue | Trades | State rows | Candidate-state coverage | OI coverage | Funding coverage | BTC coverage | Spread/depth coverage | Sector proxy coverage | Full panel ready |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Bybit | 2,367 | 48,447 | 2.45% | 67.55% | 100.00% | 100.00% | 0.00% | 0.00% | false |
| Binance | 2,152 | 44,416 | 2.25% | 7.12% | 99.74% | 100.00% | 0.00% | 0.00% | false |

The hourly panel is a coverage audit only. Candidate-state rows remain sparse,
absence of a row is not invalidation, Binance OI coverage is poor, and the
frozen tape still has no spread/depth or sector-proxy state.

## Verdict

Label: `exploratory`.

Recommendation: do not add a candidate-tape signal-invalidation exit to the live
continuous sleeve from this evidence. The measurable sparse-tape invalidation
rules were either no-ops or harmed returns. A stronger future test would require
a full hourly state panel with causal OI, funding, spread/depth, and sector
features plus a full component+hedge replay before it could affect deployment.
The 2026-06-28 hourly coverage audit confirms that full panel is not available
yet.
