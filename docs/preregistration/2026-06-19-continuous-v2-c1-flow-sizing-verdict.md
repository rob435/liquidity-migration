# Continuous V2 C1 Idiosyncratic-Flow Sizing Verdict (Binance-only)

Date: 2026-06-19

Parent: `docs/preregistration/2026-06-19-continuous-v2-ab-amendment-binance-only-flow.md`
Construction: `docs/preregistration/2026-06-19-continuous-v2-c1-flow-sizing-construction.md`
Prior step: `docs/preregistration/2026-06-19-continuous-v2-c-flow-overlay-verdict.md`

Scope: `claimed_venue_scope=binance_only_flow_exploratory`. Run label `exploratory`;
no Tier-2 candidate pass possible. No real-money claim.

## What ran

`backtest-runs/continuous_v2_ab_c1flow_2026-06-19/` (V2_CONTROL, C1, C1H on binance).
C1: entries unchanged; causal mean-1 per-trade size tilt from `idiosyncratic_flow` with
NEGATIVE sign (size DOWN high idiosyncratic-buy / continuation-risk names, size UP low-flow
names). This was the de-risk direction — the opposite of B1P's tail-concentrating size-up —
and `idiosyncratic_flow` had the strongest C0 within-symbol trade IC (−0.103).

## Result — FALSIFIED

Control (binance): ret +84.02%, maxDD −3.27%, MAR 8.185.

| Arm | ret | maxDD | MAR | MAR Δ | maxDD Δ |
| --- | ---: | ---: | ---: | ---: | ---: |
| `C1_FLOW_RESID_FEATURE_SIZING_BINANCE_ONLY` | +79.93% | −5.87% | 4.336 | **−3.85** | −0.026 |
| `C1H_FLOW_RESID_SIZING_HASH_CONTROL_BINANCE_ONLY` | +83.50% | −3.42% | 7.762 | −0.42 | −0.0015 |

C1 is **catastrophically worse than control**: MAR roughly halves (8.185 → 4.336) and max
drawdown nearly doubles (−3.27% → −5.87%). The clip-bound up-weighting (max 2.0×) of low-flow
names concentrated a much larger drawdown. This is the SAME failure mode as the B-book
path-shape sizing arm (B1P): a real per-trade IC does not survive as a sizing rule because the
tilt concentrates the squeeze tail. **The C1H hash control lost only −0.42 MAR (the
sizing-mechanism noise level), so C1's −3.85 is ~9× the mechanism noise and is a
signal-aligned effect: the idiosyncratic_flow→trade alignment is actively destructive, not just
mechanism churn.** Hash-confirmed falsification.

## Verdict — the entire C-flow branch is now CLOSED (Binance-only exploratory)

Flow does not harvest in any tested intervention:

- C0 screen: flow is a real **trade-level** within-symbol signal (idiosyncratic_flow −0.103,
  taker_imbalance, flow_squeeze), but `flow_resid_return` (residualize-vs-run-up) fails the null.
- C2/C3 hedge-intensity overlays: closed — flow is a poor hedge-timing signal (C2 matched by
  its hash; C3 inside the overlay noise floor).
- C1 trade-level sizing (both directions implied): closed — sizing by flow destroys MAR and
  doubles drawdown (tail concentration).

A Bybit full-market taker-flow archive build is **not justified**: the flow signal is real but
untradeable through hedge timing or sizing on the venue where it is fully covered (Binance),
so spending storage/compute to extend it to Bybit would not change the conclusion. Revisit only
under a new mechanism hypothesis (e.g. flow as an *admission/exit* gate, or a risk-aware
size-by-return/risk rule) in a fresh dated amendment.
