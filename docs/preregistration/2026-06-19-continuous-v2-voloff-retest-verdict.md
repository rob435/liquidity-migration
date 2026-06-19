# Continuous V2 — Vol-Off Retest Verdict (one-venue arms, updated system)

Date: 2026-06-19
Construction: `docs/preregistration/2026-06-19-continuous-v2-voloff-retest-construction.md`
System: operator-override {daily vol adjuster OFF, TP12}.
Run: `backtest-runs/continuous_v2_ab_voloff_retest_2026-06-19/` (+ robustness.json). Both venues.
Demo/paper research; `REAL_MONEY` false.

## Question

Did the one-venue-success / two-venue-fail arms (A4B regime hedge-intensity; B1 score-margin sizing)
flip to two-venue candidates once the daily volatility adjuster — shown to amplify venue splits — is
removed?

## Results vs the vol-off control (control re-simulated at {TP12, vol-off})

Control: Bybit MAR 6.387 / DD −1.30%; Binance MAR 4.160 / DD −1.41%. (Vol-off cuts gross/return and
tightens DD vs vol-on; the arm increments below are measured against this same vol-off control.)

| arm | venue | MAR Δ | boot P(Δ>0) | min LOO Δ | top-3 pos share | vs hash |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| A4B regime hedge-intensity | bybit | +0.042 | 0.610 | +0.0004 | 0.56 | hash −0.019 |
| A4B regime hedge-intensity | binance | +0.039 | **0.244** | −0.0002 | 0.43 | hash −0.104 |
| B1 score-margin sizing | bybit | **−0.077** | 0.788 | −0.0027 | 0.40 | hash −0.126 |
| B1 score-margin sizing | binance | −0.145 | 0.317 | −0.0022 | 0.63 | hash B6 +0.352 |

Vol-ON baseline (for comparison): A4B Bybit +0.439 / Binance −0.099; B1 Bybit +0.232 / Binance −0.356.

## Verdict — the adjuster confounded BOTH arms; neither is a robust two-venue candidate

- **A4B: the Binance penalty was a vol-adjuster artifact.** Binance flipped −0.099 → **+0.039** and
  A4B now beats its hash on both venues (hash −0.019 / −0.104). So removing the adjuster removed the
  Binance penalty — confirming the rebalance was responsible for A4B's two-venue failure. **But A4B
  is NOT a robust both-venue alpha:** the Binance bootstrap P(Δ>0) is only 0.244 (resampling is
  majority-negative), leave-one-month-out ≈ 0, top-3 positive-month share 0.43, and the +0.04 MAR is
  ~0.9% relative. Pooled +0.041 (runner verdict "descriptive", below the loose 0.1 bar). Net: vol-off
  turns A4B from Binance-negative into **venue-neutral / within-noise**, not a winner.
- **B1: the Bybit win was a vol-adjuster artifact — the harmful kind.** Bybit collapsed +0.232 →
  **−0.077**: the one-venue "success" was the vol-target amplifying the sizing tilt, not a real edge.
  Under the clean system B1 is **negative on both venues** (−0.077 / −0.145), pooled −0.111 (FALSIFY),
  and loses to its hash (B6 +0.352 Binance, +0.113 pooled — the random sizing tilt again trips the
  loose rule, re-confirming the sizing mechanism emits spurious MAR). Falsified.

## Clear read (the point of this retest)

Removing the daily volatility adjuster gives the un-confounded picture, and it cuts both ways: it was
**penalizing A4B on Binance** (artifactual two-venue failure) and **inflating B1 on Bybit**
(artifactual one-venue success). With it off, **neither one-venue arm survives as a robust two-venue
candidate** — A4B is venue-neutral/within-noise, B1 is negative both venues. No new candidate emerges
from this branch. (Methodology note now standing: judge sizing/overlay arms by the constant-gross
bootstrap vs their matched hash, not the rebalanced point estimate or the loose pooled rule.)

## Status / next

- A4B: closed as a two-venue candidate (point-estimate-positive but within noise). If the operator
  still wants it, it is only a no-order forward shadow hypothesis, not a wiring change.
- B1: closed (falsified on both venues, beaten by hash).
- The vol adjuster remains OFF per the operator override; this retest used that updated system.
