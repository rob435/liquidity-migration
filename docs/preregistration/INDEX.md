# Preregistration Index

This folder no longer stores append-only research diaries. Exact historical
receipts live in git history and local run artifacts. Keep only:

- `_template.md` for the next serious run.
- This index for active anchors and closed-arc verdicts.

## Rule

Serious parameter work needs a dated hypothesis, data roots, decision rule, and
artifact path before the expensive run. Mark throwaway work `exploratory`; do
not use it as acceptance evidence.

## Active Anchors

| Area | Anchor | Status |
| --- | --- | --- |
| Continuous v2 | Baseline starts `2026-06-18T19:54:00Z`; three components, inverse-vol sizing, BTC/ETH hedge, BTC-vol regime | Control for future A/B work |
| Continuous live target | TP12, daily rebalance disabled, no daemon/server stop, `CTRL_BTC_RISK_70_90_35` sizing overlay | Local demo/paper target; not yet proven deployed on VPS |
| Continuous daily rebalance A/B | `2026-06-25-continuous-daily-vol-rebalance-ab.md` | Run complete; rejected legacy ON, keep disabled |
| Continuous validation baseline | `2026-06-27-continuous-fade-validation-baseline.md` | Timestamped baseline + expanded diagnostics complete; +1h/+2h delay, +1% adverse-limit, fixed-stop, gate-off, and non-30d BTC-lookback replays rejected; exploratory only |
| Continuous 5m data | `2026-06-27-continuous-fade-5m-data-backfill.md` | 5m partitions present for all PIT manifest symbol-days in the validation sample; some source days remain partial |
| Continuous synthetic squeeze survival | `2026-06-28-continuous-fade-synthetic-squeeze-survival.md` | Active-book squeeze diagnostics survived current sampled sizing; dynamic 5m overlay completed separately |
| Continuous cluster risk-of-ruin | `2026-06-28-continuous-fade-cluster-risk-of-ruin.md` | Completed 10,000-path cluster/tail-injected bootstrap; no account impairment, but worst-cluster overweight stress shows material 10% DD fragility |
| Continuous dynamic liquidation/outage | `2026-06-28-continuous-fade-dynamic-liquidation-outage.md` | Completed 42-row 5m path overlay; no maintenance-proxy liquidations, exploratory only and not a liquidation engine |
| Continuous disaster-loss sizing | `2026-06-28-continuous-fade-disaster-loss-sizing.md` | Completed current notional versus loss-budgeted safe notional diagnostic; strict +100% / 0.10% budget flags ~97% trades over budget |
| Continuous BTC-risk tail skip | `2026-06-28-continuous-fade-btc-risk-tail-skip.md` | Completed full portfolio replay; rejected by two-venue rule because Bybit MAR/DD worsened despite Binance improvement |
| Continuous BTC-tail skip with BTC gate off | `2026-06-28-continuous-fade-btc-tail-skip-gate-off.md` | Registered combined falsifier; run pending |
| Continuous conditional scale-in | `2026-06-28-continuous-fade-conditional-scale-in.md` | Diagnostic add-on-after-MAE model is directionally positive on both venues but exploratory only |
| Continuous scale-in portfolio replay | `2026-06-28-continuous-fade-scale-in-portfolio-replay.md` | Full component+hedge overlay lifted return but worsened MAR and drawdown on both venues; reject deployment change |
| Continuous signal invalidation | `2026-06-28-continuous-fade-signal-invalidation.md` | Sparse candidate-tape invalidation arms were zero-hit or reduced component net; hourly coverage audit still lacks full state panel, so do not add live exit |
| Continuous DSR/PBO | `2026-06-28-continuous-fade-dsr-pbo.md` | Frozen full-replay variant surface is fragile: PBO 41.43% Bybit / 35.71% Binance and baseline DSR probabilities 23.17% / 20.08%; do not trust internal rankings as deployment proof |
| Continuous forward readiness | `2026-06-28-continuous-forward-readiness.md` | Paper/demo rebalance and operational-cycle telemetry are clean, but 0 paired trades means no execution-performance evidence yet |
| Long v11a | FC-only long sleeve with v11a sniper entry, vol parity, ATR exits | Best current internal positive object |
| Reconciliation | `scripts/reconcile.sh` pulls demo/paper, rebuilds PIT context, and runs three-way checks | Latest run had no unexplained drift and no forward trades |

## Closed Research

| Arc | Verdict |
| --- | --- |
| Continuous v2 A/B foundation | No accepted candidate. Flow, conviction sizing, entry timing, exit timing, and TP variants failed two-venue or hash controls. |
| One-minute execution books A/B/E/F/G | No durable two-venue lead; signals exist but did not survive executable controls. |
| Upper-wick sizing | Retracted after duplicate-counting/parity audit. Code remains flag-off. |
| BTC-risk gate replacement | Full gate replacements failed; the narrow `CTRL_BTC_RISK_70_90_35` sizing overlay improved MAR/drawdown but cut Binance total return. |
| Continuous daily rebalance | TP12 component A/B rejected legacy ON: it mostly max-levered the book, worsened drawdown, and failed the MAR/worst-90d rule. |
| Long cadence loosening | More trades, worse guard outcomes; no retained arm. |
| Old daily short | Erased from the active system on 2026-06-11. Git history is the archive. |

## What Not To Recreate

Do not rebuild the deleted markdown pile. A new receipt should summarize the
decision and point at durable artifacts, not paste command logs or repeat global
policy boilerplate.
