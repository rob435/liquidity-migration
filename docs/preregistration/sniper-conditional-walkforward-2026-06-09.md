# Pre-registration: S2 — conditional (vol-scaled, ridge-sized) snipe, walk-forward OOS

**Date:** 2026-06-09 (registered BEFORE the run). **Label:** `exploratory`.
**Mandate:** operator 2026-06-09 ("doesn't have to be so hard set... ridge regression
on multiple parameters"). Method note: the operator waived repo formality; we keep the
two parts that protect the result itself — causality and out-of-sample judgment. ALL
policy parameters here are chosen causally inside a walk-forward; the ONLY reported
numbers are stitched out-of-window. Nothing is hand-tuned on the evaluation window.

## Policies raced on identical stitched-OOS data (quarterly refits from 2024-04, 12m warmup)

- P0 incumbent: fixed level x=8%, size b=0.25 (from the S1 arc).
- P1 vol-scaled level: x_i = c * vol168_i per trade (c chosen each refit from trailing
  data on a declared grid c ∈ {1.0, 1.5, 2.0, 2.5, 3.0} by trailing pooled-trade-pnl),
  b=0.25. vol proxy: the engine's own inverse-vol sizing inverted (causal at entry),
  floored/capped to [2%, 20%] level.
- P2 = P1 level + ridge-sized b_i = clip(ridge(features), 0, 0.5), ridge fit each refit
  on trailing trades' realized B-leg pnl; features (all entry-causal): vol proxy,
  composite score, BTC 30d trailing return, entry hour-of-day sin/cos; standardized;
  lambda=1.0 fixed (not tuned).
- Fill/outcome model for fitting and comparison: the mae-based sim (consistent across
  policies); the WINNING policy then gets ONE bar-accurate validation (pro-rata
  funding, strict trade-through fills, own-TP) before any headline.

## Evaluation (stitched OOS 2024-04..end, both venues)

Per policy: per-trade deltas -> components -> frozen combine -> hedged-max4 rebalance,
metrics on the stitched window. Decision rule (a-priori): a conditional policy (P1/P2)
replaces P0 only if its pooled OOS MAR delta vs baseline EXCEEDS P0's by >= +0.15 with
both venues no worse than -0.1 vs P0; ties -> simpler policy. The winner's bar-accurate
validation must confirm within 0.15 MAR. Honest framing: this refines the S1 Tier-1
lead; banking still requires the calibrated-cost test (R4) or forward evidence.

## Artifacts

`~/SHARED_DATA/sniper_conditional_2026-06-09/`. Script:
`scripts/sniper_conditional_walkforward.py`.

## Verdict (filled in after the run)

_pending_

## Verdict (filled in after the run, same day)

**The FIXED incumbent wins the walk-forward race on both venues** — stitched-OOS
(2024-04+) hedged-max4 deltas: P0 fixed x8/b25 **+1.06 bybit / +0.60 binance**
(pooled +0.83); P1 vol-scaled +0.39/+0.47; P2 vol-scaled+ridge +0.54/+0.41. The causal
conditional policies over-reach for per-fill quality (mean level ~11-12%, B-pnl
+426/+260 bps) at the cost of fill breadth (30% vs 42%) — net worse. Per the
pre-registered rule (conditional must beat P0 by >= +0.15 pooled), P0 STANDS.

Third independent confirmation tonight of the same lesson (ensemble weights, snipe-c,
ridge-b): in this system, SIMPLE FIXED parameters beat adaptive/fitted ones
out-of-window. The fixed snipe's OOS validation is itself strengthened by winning
against causal competitors on never-seen quarters. Banking still awaits the
calibrated-cost test (R4: VPS key authorization pending — this box's id_ed25519.pub
needs adding to the VPS authorized_keys; host key verified+pinned 2026-06-09).
