# Continuous V2 Problem Book F — Exit-Timing Shadow Construction (Pre-Registration)

Date: 2026-06-19

Parent plan: `docs/preregistration/2026-06-18-continuous-v2-ab-research-plan.md` (Problem Book F).
Control: frozen v2 (10% component TP + 24h hold, 48h max; no daemon/server stops).
Scope: CONTINUOUS demo/paper research, **both venues**. NO-ORDER SHADOW ONLY — this can become
`candidate` only after the bar the old live-exit stack failed, and `paper_ready` only with forward
demo. No real-money claim.

## Why (and the landmine)

Exit attribution on the v2 control ledger (both venues): the 24h `max_hold` bucket is ~65% of
trades and **net-negative** (raw −2.0%/−2.5%, contribution −0.41/−0.49); the whole book's PnL is the
~35% that hit the 10% TP. ~28% of max_hold trades reached MFE >3% and gave it all back by 24h.

This looks like a large exit opportunity, but it is a **known falsified trap**: the old daemon exits
(`stop_approach`, `left_decile`, `failed_fade`, `breakeven`, 25% server stop) were registered as
edge-destroying (STATE.md, 2026-06-18 replay) because capturing the giveback also cuts the trades
that later reach the 10% TP. So this is a SHADOW with explicit TP-winner-cut accounting — the exit
analogue of E1's missed-fill accounting.

## Construction (fixed before running)

For each control trade, reconstruct the causal hourly price path from entry to the original exit
(capped at the 48h max). Apply each candidate exit rule causally (decision uses only the path up to
the decision bar). Recompute the realized short return at the shadow exit; keep cost/funding
first-order. Per-trade shadow diagnostic (not a daemon re-sim), both venues.

Candidate exit rules (one mechanism each — not stacked):

- **F2a SHORTER_HOLD_12H / 18H**: cap the hold at 12h / 18h (vs 24h). TP still fires first.
- **F2b MFE_GIVEBACK**: once intratrade MFE ≥ A (A ∈ {3%, 5%}), exit if price gives back ≥ G of the
  peak gain (G ∈ {0.5}). A profit-trail; TP still fires first.
- **F2c TIME_DECAY**: exit at the historical median TP-hit hold (~11h) if not already TP'd
  ("after the MFE window, not before").

Negative control:

- **F5 RANDOM_EXIT**: exit at a uniformly random hour within [1h, original hold], matched exit
  frequency. A rule must beat this null to claim exit-timing skill.

## Honest accounting (the falsifier that killed the old exits)

For every trade the rule exits EARLY vs control, classify:

- **TP winner cut**: control exited via take_profit; shadow exits before → forgoes (TP% − shadow%).
- **loser saved**: control max_hold loss; shadow exits earlier at a smaller loss → saves.
- Net per rule = Σ (losers saved) − Σ (TP winners cut), notional-weighted, both venues.

Report per venue: total return / MAR proxy delta, TP-winners-cut count + forgone PnL, losers-saved
PnL, and the comparison vs the F5 random-exit null.

## Pass / falsifier

- PASS (shadow): improves the per-trade net contribution on BOTH venues, beats the random-exit null,
  and the gain is NOT dominated by cutting TP winners (i.e. losers-saved > TP-winners-cut on both).
- FALSIFY: net ≤ 0 on either venue; or does not beat the random null; or the "gain" is just survivor
  giveback that also cuts a comparable mass of eventual TP winners (the old-exit failure mode).
- Even a PASS is shadow-only: it requires a forward demo/paper plan matching the exit lifecycle
  before any order-capable change. The frozen v2 forward ledger is NOT modified by this study.

## Limitation

Per-trade path shadow on `klines_1h`; does not re-run the daily vol-target rebalance or re-solve
concurrency/cooldown under changed holds. It is the gate that decides whether a full lifecycle Book F
A/B + forward shadow is worth building.
