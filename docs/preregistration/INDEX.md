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
| Continuous live target | TP12, daily rebalance disabled, no daemon/server stop, `CTRL_BTC_RISK_70_90_35` sizing overlay | First material forward loss observed; sniper retired and safety release pending deploy |
| Continuous validation baseline | `continuous_ensemble_v2_baseline_current` under `research/continuous_fade/runs/` | Timestamped baseline plus expanded diagnostics complete; exploratory only |
| Continuous forward readiness | `reports/continuous_forward_readiness/` | Older zero-trade report is stale; 2026-07-10 pre-reset reconcile has 7 paired base rows, 5 paper-only, 2 demo-only, 4 sniper-only, and one exit-reason divergence |
| Lookback robustness | `docs/lookback_audit.md` | Repo-wide classification of lifecycle, feature, risk-memory, reporting, and operational lookbacks; use before adding time windows |
| BTC month-regime lookbacks | `btc-month-regime-2026-07-04.md` | Proposed preregistration: continuous hourly confirmed 30d/month/smart BTC gate plus comparable long month-regime gate; defaults unchanged |
| Continuous tail survival | `continuous-tail-survival-2026-07-10.md` | Registered, not run: causally valid budget-only matrix (control plus 0.10%/0.15%/0.25% +100%-loss caps), signals through 2026-07-09 and strict exit-path data through 2026-07-11 on both venues; heat/exit arcs deferred |
| Granular adverse-risk | `continuous-granular-adverse-risk-2026-07-10.md` | Registered, not run: causal sub-hour entry/exit mechanism study; current canonical roots fail granular readiness and must be built/audited first |
| Long v11a | FC-only long sleeve with v11a sniper entry, vol parity, ATR exits | Best current internal positive object; tiny ADA forward sample has timing/exit mismatch |
| Reconciliation | `scripts/reconcile.sh` | 2026-07-10 run flagged both sleeves; venue independently confirmed flat; incident reconstruction is `docs/incidents/2026-07-10-1000tag.md` |

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
| Tail-budget control (2026-07-03) | Unexecuted and superseded. The 2026-07-05 "fixed stop in disguise" rejection was conceptually wrong and was withdrawn on 2026-07-10; the narrower implemented tail-survival prereg is now active. |
| Blacklist / entry-time controls | Rejected (2026-07-05): time-stop arm underperformed control; H1 symbol / H2 learned entry-time / H3 permanent blacklist branches did not produce a deployable improvement. Dated dispatcher and engine hooks removed; prereg retained as the falsifier record. |

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
