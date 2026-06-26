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
