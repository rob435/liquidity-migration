# Continuous Fade BTC-Risk Tail Skip Replay

Date: 2026-06-28

Run label: exploratory.

Real money: no. Demo/paper and offline research only.

## Hypothesis

The existing BTC-risk sizing overlay already identifies a small tail state by
assigning `external_size_multiplier=0.35`. Post-hoc candidate-ledger diagnostics
show those selected trades are negative net on both venues and have materially
higher adverse excursion. The full replay tests whether that state should be
skipped entirely rather than down-sized.

## Pre-Run Evidence

Frozen selected-trade candidate tape distribution:

| Venue | Selected trades | 0.35x trades | Share |
|---|---:|---:|---:|
| Bybit | 2,367 | 277 | 11.70% |
| Binance | 2,152 | 258 | 11.99% |

Candidate-level post-hoc contribution for the 0.35x rows:

| Venue | 0.35x net contribution | MAE >=20% | Never profitable |
|---|---:|---:|---:|
| Bybit | -0.002678 | 23.47% | 14.44% |
| Binance | -0.001565 | 23.26% | 12.79% |

The selected share fits the plan's simple 5-15% skip target. Wider thresholds up
to `<=0.70` are identical in this tape because selected multipliers are binary
(`0.35` or `1.0`); `<=1.0` would skip the whole book and is not a valid arm.

## Arms

| Variant | Rule |
|---|---|
| `baseline_current` | Current deployed research target: BTC-risk tail state remains 35% sized. |
| `skip_btc_tail_035` | Skip entries when the supplied external size multiplier is `<=0.35`. |

## Data And Engine

- Data roots: `~/SHARED_DATA/bybit_full_pit` and `~/SHARED_DATA/binance_full_pit`.
- Engine: full continuous component replay plus BTC-risk sizing plus two-factor hedge.
- Artifacts: `skip_portfolio_replay.csv`, per-venue component reports, equity curves,
  monthly curves, summary JSONs, and BTC-risk multiplier CSVs.
- Costs/funding/hedge: unchanged from the frozen continuous ensemble refresh.

## Decision Rule

Reject the skip arm if any venue loses MAR, materially worsens drawdown, removes
outside the 5-15% intended trade range, or only improves a metric by collapsing
trade count. If both venues improve return/MAR without a drawdown penalty and the
removed share remains near the preregistered 5-15% window, label it a research
candidate only. It still needs forward demo/paper arbitration before any live
sizing change.

## Known Limits

This is not an OOS promotion test. The window is spent. The result can reject a
bad skip rule or nominate a paper-only candidate, but cannot approve real money
or size increases.

## Outcome

Completed 2026-06-28. Verdict: rejected for promotion by the preregistered
two-venue rule.

| Venue | Baseline return / MAR / DD | Skip return / MAR / DD | Net component trade removal |
|---|---:|---:|---:|
| Bybit | +26.64% / 7.33 / -1.13% | +26.68% / 7.08 / -1.17% | 7.52% |
| Binance | +18.84% / 5.72 / -1.02% | +20.49% / 7.15 / -0.89% | 7.53% |

The skip arm improved Binance, but Bybit lost MAR and worsened drawdown. Keep
the current 35% BTC-risk sizing behavior unless forward OOS evidence contradicts
this replay.

Artifacts:

- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/skip_portfolio_replay.csv`
- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/portfolio_replays/skip_grid/skip_btc_tail_035/bybit/continuous_equity_summary.json`
- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/portfolio_replays/skip_grid/skip_btc_tail_035/binance/continuous_equity_summary.json`
- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/reports/final_research_report.md`
