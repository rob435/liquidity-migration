# Decision receipt: WP2 — demote the downtrend extension, re-anchor the continuous canonical

**Date:** 2026-06-09
**Type:** decision receipt over existing evidence (no new run). Program: continuous
regime robustness WP2 (plan folded into `docs/research_summary.md` on 2026-06-10).
**Evidence base:** the 2026-06-09 refinement-campaign + robustness-battery session
(commit 5e1c960; receipt `docs/preregistration/continuous-winner-robustness-2026-06-09.md`;
artifacts `~/SHARED_DATA/continuous_{robustness,multihorizon,breaker,rmom_probe,rmomgate}_2026-06-09`).

## Decision

1. **DEMOTED:** the downtrend-extended ensemble
   `winner_up_..._plus_dt40_turn4p5_premium_..._w70_tv45_max10_dd4` (headline 7.50/6.84)
   is no longer the continuous research lead. The `premium_24h_ge0` downtrend sleeve is
   fragile/overfit: 85 trades on ~10 active days (bybit) / 91 on ~9 days (binance) over
   3 years, ~0% standalone return, and `dt_scale=0.4` sits at a cliff (a=0.7 → binance
   MAR 3.26/DD −17%; a=1.0 → 2.51/−22%; a=1.5 → 1.67/−32%). Its headline is partly an
   artifact of a thin, unstable sliver.
2. **RE-ANCHORED canonical (research-only):** the parsimonious uptrend ensemble
   `winner_base = {turn3p3:0.30, turn4p3:0.20, turn4p5:0.40, age210tp14:0.10}` @
   `w90/max_scale` rebalance, quoted at **max4–6 leverage** (max4: bybit +84%/MAR 5.0,
   binance +60%/MAR 4.6), not the max10 headline (6.18/6.01) — the max10 number is
   recent-regime-flattered and impact-uncalibrated. `tv` is a dead knob (scale pinned
   at `max_scale`); state it as such.
3. Forward expectations set from the weaker 2023–24 sub-period, per the robustness
   receipt.

## Why now

The winner passed the 5/5 falsification battery (plateau, de-lever-stable, 3x-cost
survivor) — the EDGE is robust; the downtrend sliver and the max10 headline are the
non-robust parts. Keeping a fake 7.5-MAR target distorts every subsequent comparison
bar. STATE.md and `docs/research_summary.md` updated in this same change.

## Status

Research-only. Not promoted, not paper-ready. Tier-3 arbiter remains forward demo/paper.
