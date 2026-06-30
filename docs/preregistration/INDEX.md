# Preregistration Index

This folder is not a receipt archive. Exact historical receipt text lives in
git history and local run artifacts. Keep only:

- `_template.md` for the next serious run.
- This index for active anchors and closed-arc verdicts.

The live decision log is `docs/research_summary.md`; operational state is
`STATE.md`.

## Rule

Serious parameter work needs a dated hypothesis, data roots, decision rule, and
artifact path before the expensive run. Mark throwaway work `exploratory`; do
not use it as acceptance evidence.

## Active Anchors

| Area | Anchor | Status |
| --- | --- | --- |
| Continuous v2 | Baseline starts `2026-06-18T19:54:00Z`; three components, inverse-vol sizing, BTC/ETH hedge, BTC-vol regime | Control for future A/B work |
| Continuous live target | TP12, daily rebalance disabled, no daemon/server stop, `CTRL_BTC_RISK_70_90_35` sizing overlay | Deployed on VPS; forward paired trades still needed |
| Continuous validation baseline | `continuous_ensemble_v2_baseline_current` under `research/continuous_fade/runs/` | Timestamped baseline plus expanded diagnostics complete; exploratory only |
| Continuous forward readiness | `reports/continuous_forward_readiness/` | Paper/demo rebalance and operational-cycle telemetry are clean; 0 paired trades means no execution-performance evidence yet |
| Long v11a | FC-only long sleeve with v11a sniper entry, vol parity, ATR exits | Best current internal positive object; forward sample still needed |
| Reconciliation | `scripts/reconcile.sh` | Latest full three-way had no unexplained drift and no forward trades |

## Closed Continuous Arcs

| Arc | Verdict |
| --- | --- |
| Daily rebalance | Rejected rebalance ON for TP12 components; it mostly max-levered the book, worsened drawdown, and failed the MAR/worst-90d rule. |
| 5m timing and adverse-limit paths | Useful diagnostics, but added delay and +1% adverse-limit failed full replay; partial 5m source days remain explicit caveats. |
| Fixed stops | 20%/40%/80% price stops trailed the no-stop baseline on both venues. |
| BTC gate retunes | Gate-off and non-30d lookbacks failed full replay; do not retune the gate from this grid. |
| BTC-risk tail hard skip | Rejected by two-venue rule because Bybit MAR/DD worsened despite Binance improvement. |
| BTC-tail skip with BTC gate off | Closed in the hot path; both component ideas already failed separate full-replay falsifiers, so re-register before spending more compute. |
| Synthetic squeeze, cluster bootstrap, dynamic outage, disaster sizing | Current tiny sizing survives sampled shocks, but disaster-budget diagnostics argue against any size increase without explicit loss caps. |
| Conditional scale-in | By-trade signal looked positive, but full component+hedge replay lifted returns while worsening MAR and drawdown; no deployment change. |
| Signal invalidation | Sparse candidate-tape exits were zero-hit or reduced component net; hourly state coverage is insufficient. |
| DSR/PBO | Frozen replay variant surface is fragile; do not trust internal rankings as deployment proof. |

## Other Closed Research

| Arc | Verdict |
| --- | --- |
| Continuous v2 A/B foundation | No accepted candidate. Flow, conviction sizing, entry timing, exit timing, and TP variants failed two-venue or hash controls. |
| One-minute execution books A/B/E/F/G | No durable two-venue lead; signals exist but did not survive executable controls. |
| Upper-wick sizing | Retracted after duplicate-counting/parity audit. Code remains flag-off. |
| BTC-risk gate replacement | Full gate replacements failed; the narrow `CTRL_BTC_RISK_70_90_35` sizing overlay improved MAR/drawdown but cut Binance total return. |
| Long cadence loosening | More trades, worse guard outcomes; no retained arm. |

## What Not To Recreate

Do not rebuild the deleted markdown pile. A new receipt should summarize the
decision and point at durable artifacts, not paste command logs or repeat global
policy boilerplate.
