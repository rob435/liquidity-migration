# Preregistration Index

This folder is a compact contract and decision index, not a raw command-log
archive. Keep active preregistrations and any compact completed receipt needed
to reconstruct a decision. Store large artifacts with the run and record their
content identity here. Git history is a backup, not the only evidence registry.

The live decision log is `docs/research_summary.md`; operational state is
`STATE.md`.

## Rule

Decision-influencing work follows `docs/parameter_pre_registration.md`: register
the claim, exposure boundary, comparison, decision/stopping rule, tested set,
and artifacts before inspecting the affected result. Exploration is allowed but
does not become confirmatory retroactively.

## Active Anchors

| Area | Anchor | Status |
| --- | --- | --- |
| Continuous v2 | Baseline starts `2026-06-18T19:54:00Z`; three components, inverse-vol sizing, BTC/ETH hedge, BTC-vol regime | Control for future A/B work |
| Continuous live target | TP12, daily rebalance disabled, no daemon/server stop, `CTRL_BTC_RISK_70_90_35` sizing overlay | Deployed control remains base-only; the future local runtime deletes the adverse-limit add-on. The TAC/SKL receipt is historical sleeve-projection evidence, not account-owner acceptance. |
| Continuous validation baseline | `continuous_ensemble_v2_baseline_current` under `research/continuous_fade/runs/` | Timestamped baseline plus expanded diagnostics complete; exploratory only |
| Continuous forward readiness | `reports/continuous_forward_readiness/` | Historical 2026-07-10 sleeve-projection receipt: 5/5 paper↔demo pairs. It predates the target-only account-owner boundary and is not current runtime acceptance. |
| BTC month-regime lookbacks | `btc-month-regime-2026-07-04.md` | Proposed preregistration: continuous hourly confirmed 30d/month/smart BTC gate plus comparable long month-regime gate; defaults unchanged |
| Continuous tail survival | `continuous-tail-survival-2026-07-10.md` | Registered, not run: causally valid budget-only matrix (control plus 0.10%/0.15%/0.25% +100%-loss caps), signals through 2026-07-09 and strict exit-path data through 2026-07-11 on both venues; heat/exit arcs deferred |
| Granular adverse-risk | `continuous-granular-adverse-risk-2026-07-10.md` | Registered, not run: causal sub-hour entry/exit mechanism study; current canonical roots fail granular readiness and must be built/audited first |
| Strategy overhaul scout | `strategy-overhaul-scout-2026-07-10.md` | Local full-window outcome-blind Phase 0 `strategy-overhaul-phase0-bccefdfc38ae9fda3c17` is internally re-executable but `NOT_READY`; it found a seven-day Binance root/provenance gap that was repaired prospectively afterward, both roots still lack canonical lineage, no big-PC bundle or canonical child exists, and no population/outcome stage was inspected |
| Long v11a | FC-only long sleeve with v11a sniper entry, vol parity, ATR exits | Best current internal positive object; tiny ADA forward sample has timing/exit mismatch |
| Account runtime acceptance | `docs/account_execution_cutover.md` | Open. The sleeve-local reconciler was retired 2026-07-13 because its compatibility projections are not authoritative for the account owner. Structural account-journal parity exists, but captured-tape, common-scheduler, venue-rule, credentialed-demo, and fill/P&L gates remain unproven. |
| Demo execution calibration v1 | `docs/preregistration/account_execution_calibration_2026_07_13.md` | Closed at feasibility before a calibration order: current BTC structural minimum made its $30 open impossible. Preserved as spent design history. |
| Demo execution calibration v2 | `docs/preregistration/account_execution_calibration_v2_2026_07_13.md` | Prospective bounded target-only sample registered from rule feasibility only: three symbols, five $80 round trips each, optional registered funding hold, independent clock-offset gate. Execution calibration only; LONG/CONT parity remains separate. |

## Closed Continuous Arcs

“Closed” records the current read and discourages repeating an identical search.
It is not an administrative ban. Distinguish empirically contradicted,
inconclusive, abandoned, and superseded claims when reopening work.

| Arc | Verdict |
| --- | --- |
| Daily rebalance | Rejected rebalance ON for TP12 components; it mostly max-levered the book, worsened drawdown, and failed the MAR/worst-90d rule. |
| 5m timing and adverse-limit paths | Useful diagnostics, but added delay and +1% adverse-limit failed full replay; partial 5m source days remain explicit caveats. |
| Fixed stops | 20%/40%/80% price stops trailed the no-stop baseline on both venues. |
| BTC gate retunes | Gate-off and non-30d lookbacks failed full replay; do not retune the gate from this grid. |
| BTC-risk tail hard skip | Rejected by two-venue rule because Bybit MAR/DD worsened despite Binance improvement. |
| BTC-tail skip with BTC gate off | Closed in the hot path; both component ideas already failed separate full-replay falsifiers, so re-register before spending more compute. |
| Synthetic squeeze, cluster bootstrap, dynamic outage, disaster sizing | Current tiny sizing survives sampled shocks, but disaster-budget diagnostics argue against any size increase without explicit loss caps. |
| Conditional scale-in | By-trade signal looked positive, but full component+hedge replay lifted returns while worsening MAR and drawdown; rejected runtime/shadow implementation removed 2026-07-13. |
| Signal invalidation | Sparse candidate-tape exits were zero-hit or reduced component net; hourly state coverage is insufficient. |
| DSR/PBO | Frozen replay variant surface is fragile; do not trust internal rankings as deployment proof. |
| Tail-budget control (2026-07-03) | Unexecuted and superseded. The 2026-07-05 "fixed stop in disguise" rejection was conceptually wrong and was withdrawn on 2026-07-10; the narrower implemented tail-survival prereg is now active. |
| Blacklist / entry-time controls | Rejected (2026-07-05): time-stop arm underperformed control; H1 symbol / H2 learned entry-time / H3 permanent blacklist branches did not produce a deployable improvement. Dated dispatcher and engine hooks removed; prereg retained as the falsifier record. |

## Other Closed Research

| Arc | Verdict |
| --- | --- |
| Continuous v2 A/B foundation | No accepted candidate. Flow, conviction sizing, entry timing, exit timing, and TP variants failed two-venue or hash controls. |
| One-minute execution books A/B/E/F/G | No durable two-venue lead; signals exist but did not survive executable controls. |
| Upper-wick sizing | Retracted after duplicate-counting/parity audit. Flag-off runtime code removed 2026-07-13; the falsifier record remains here. |
| BTC-risk gate replacement | Full gate replacements failed; the narrow `CTRL_BTC_RISK_70_90_35` sizing overlay improved MAR/drawdown but cut Binance total return. |
| Long cadence loosening | More trades, worse guard outcomes; no retained arm. |

## What Not To Recreate

Do not rebuild the deleted markdown pile. A new receipt should summarize the
decision and point at durable artifacts, not paste command logs or repeat global
policy boilerplate. Do not delete a future decision-influencing contract after
its result is seen; compact or supersede it while retaining its identity and
outcome.
