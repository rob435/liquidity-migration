# Construction + Verdict: Continuous V2 Next-Level — Problem Book G (Volatility-Control Rework)

Date: 2026-06-20
Author: Claude (operator-directed next-level research push)
Stage: construction + verdict (full-ledger)
Parent plan: `docs/preregistration/2026-06-19-continuous-v2-next-level-ab-research-plan.md`
Run label: `exploratory` (full backtest ledger; forward demo/paper is the OOS arbiter). **Verdict: the daily vol-adjuster is a pure LEVERAGE dial, not a timing edge. No risk-control rework beats constant gross. The prior "adjuster hurts Bybit" was a TP confound.**

## Objective

The 2026-06-19 operator override DISABLED the daily vol-target adjuster, "to be
reworked + retuned when research is finished." Find a daily risk control that
improves risk-adjusted behavior (MAR / drawdown) vs the current OFF override
without re-introducing a venue split — at the FULL daily-marked ledger level (the
adjuster acts on daily equity; the per-trade proxy used by Books A/B is the wrong
tool).

## Method

`scripts/continuous_v2_book_g_volcontrol.py`. Load the frozen V2_CONTROL (TP12)
component pieces cached by the Phase-0 freeze, compute the 2f BTC+ETH hedge inputs +
BTC-vol regime intensity once per venue (exactly as the A/B runner), then rebuild the
full hedged ledger via `build_full_ledger(..., rebalance_rule=<variant>)` per G arm.
TP and entries are held fixed (same components) so this ISOLATES the vol-control.
G0 reproduces the Phase-0 control exactly (bybit MAR 6.387, binance 4.160) → driver
validated. All arms are expressible with the existing `ContinuousRebalanceRule`.
Critically, added **constant-gross controls** `G_CONST{2,3,4}` (vol-timing OFF, flat
leverage at cap, no drawdown/momentum derisk): MAR is ~leverage-invariant, so if the
vol-timed arms don't beat these at matched gross, the daily vol TIMING adds nothing.

## Results (full ledger, 2026-06-20)

| arm | bybit MAR / dd / worst | binance MAR / dd / worst |
|-----|------------------------|--------------------------|
| G0_CONSTANT_OFF (current) | 6.387 / −0.013 / −0.009 | 4.160 / −0.014 / −0.006 |
| G1_MAX4 (prior adjuster) | 7.447 / −0.052 / −0.037 | 4.530 / −0.056 / −0.025 |
| G2_CAP2 | 6.871 / −0.026 / −0.019 | 4.350 / −0.028 / −0.013 |
| G2b_CAP15 | 6.592 / −0.020 | 4.232 / −0.021 |
| G3_DD_ONLY | 6.387 (== G0) | 4.160 (== G0) |
| G4_MOM_DERISK | 6.536 | **3.690 (worse)** |
| G5_TGT_LOWER (cap3) | 7.553 / −0.039 | 4.614 / −0.042 |
| **G_CONST2 (flat 2×)** | **6.871 (== G2_CAP2)** | **4.350 (== G2_CAP2)** |
| **G_CONST3 (flat 3×)** | **7.553 (== G5)** | **4.646 (≥ G5 4.614)** |
| **G_CONST4 (flat 4×)** | **8.364 (> G1 7.447)** | **4.995 (> G1 4.530)** |

## Verdict — the vol-control is leverage, not alpha

1. **The MAR "improvement" from re-enabling the adjuster is purely leverage.** The
   constant-gross controls nail it: `G2_CAP2 == G_CONST2` exactly; `G5 ≈ G_CONST3`;
   and `G_CONST4` (flat 4×) **beats** `G1_MAX4` on BOTH venues. The hedged book's
   realized daily vol sits so far below any sane target that the vol-target scale is
   pinned at `max_scale` almost every day → the "adjuster" acts as a CONSTANT
   leverage multiplier equal to its cap. Where it does deviate (the drawdown-half
   and momentum derisk), it slightly HURTS (G1 < G_CONST4; G4 < G0 on binance). So
   the daily vol-TIMING contributes zero or negative risk-adjusted value.
2. **MAR rises with leverage only because drawdowns are tiny and the book is
   positive** — `MAR = ann_return/|maxDD|` is ~scale-invariant in theory, but here
   the small positive asymmetry and the hedge make higher gross look monotonically
   better on MAR while LINEARLY inflating absolute drawdown and worst-day
   (worst-day −0.93% → −3.71% from 1× to 4× on bybit). That is a capital / risk-
   appetite decision, NOT an alpha mechanism.
3. **The prior "vol-adjuster hurts Bybit MAR" finding was a TP confound.** That
   comparison (override receipt) moved TP 10→12 AND the adjuster together. Isolated
   here (TP fixed at 12), re-enabling the adjuster improves MAR on BOTH venues — but,
   per (1), only as leverage.
4. **Drawdown-only control is inert at this scale** (G3 == G0): the book essentially
   never draws down past −4%, so the drawdown-half never fires. Momentum-derisk (G4)
   is actively harmful on Binance.

## Recommendation (honest)

- There is **no vol-control timing edge to recover**. Re-enabling the adjuster should
  be framed to the operator as a **leverage / capital dial**, not a research win.
- If the operator wants daily risk *control* (not more gross), the only defensible
  option in this set is **G2_CAP2** — a moderate constant ~2× gross with materially
  less drawdown inflation than G1_MAX4 (−2.6% vs −5.2%) and a small MAR uplift — but
  this is a risk-appetite choice, and it must be gated/forward-shadowed like any
  frozen-object change (it voids the forward ledger via config hash).
- Both-venue MAR "improvers" exist ONLY because they lever up; none beats its
  constant-gross control. **No candidate vol-control mechanism.**

## Falsifiers applied

- Constant-gross controls (the load-bearing falsifier): vol-timed arms do not beat
  matched flat leverage → timing adds nothing.
- Both-venue: leverage helps both, but it's leverage, not a control mechanism.
- TP-confound check: isolating TP overturns the prior venue-split claim.

## No real-money / promotion claim

`REAL_MONEY` stays false. No frozen-object change is made. The vol-control remains a
risk-appetite dial; G2_CAP2 is the only operator-gated lead if more daily control is
desired, and it is a capital decision, not alpha.
