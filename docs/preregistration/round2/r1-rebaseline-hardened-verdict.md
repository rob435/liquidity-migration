# R1 re-baseline under hardened defaults — VERDICT (full-PIT)

**Date:** 2026-05-29
**Pre-reg:** [integrated-strategy-program.md](integrated-strategy-program.md) R1 +
[r-audit-methodology-hardening.md](r-audit-methodology-hardening.md) (re-baseline rule).
**Run label:** full-PIT re-baseline (in-sample 2023-04→2026-05). Executes the hardening
pre-reg's rule: re-run control + lead under the new defaults before reading deltas.
**Tools:** `scripts/r1_rebaseline_hardened_sweep.py` (`43c8cc1`), `scripts/r1_robustness.py`,
`cost_model.recost_trades` (R6). Tag `r1_rebaseline_hardened_2026-05-29`.

## Headline

**`R1_drop_all_4` FALSIFIES the Tier-2 demo-candidate bar under the honest hardened
engine.** Its original demo-eligibility (pooled MAR Δ **+0.45**) was substantially an
**artifact of pre-hardening optimistic stop fills** (stops filled at the trigger price,
not the bar's adverse extreme). Under the pre-registered honest engine — `bar_extreme`
stop fills (H3) + 100%-taker base (M2) + calendar-exact returns (M4/B3) — the edge
collapses: **binance return goes NEGATIVE on both the control and the lead, at BOTH the
conservative 45 bps and the honest 15 bps cost.**

## Results — hardened engine, both venues

Sweep at `cost_multiplier=3` → 45 bps round-trip (15 bps taker base × 3); honest 15 bps
column is the R6 `recost_trades` (sum of per-trade net return — a simple-return proxy,
signs/ordering exact):

| venue | cell | trades | ret (45bps) | daily DD | MAR (r1_robustness) | sum-net @15bps |
|---|---|---|---|---|---|---|
| bybit | 00_baseline | 761 | +0.167 | −33.1% | 0.15 | +0.405 |
| bybit | **R1_drop_all_4** | 816 | +0.321 | −30.5% | **0.30** | **+0.548** |
| binance | 00_baseline | 477 | −0.173 | −41.6% | −0.14 | −0.034 |
| binance | **R1_drop_all_4** | 509 | **−0.252** | −47.3% | **−0.19** | **−0.122** |

**Formal Tier-2 verdict (`r1_robustness.py`):** `R1_drop_all_4` → **FALSIFY (return ≤0 a
venue)**. Pooled MAR Δ **+0.05** (< +0.1 bar); bybit MAR Δ +0.15, binance MAR Δ −0.05;
binance return −0.25x (negative). Bootstrap MAR Δ: bybit p5 −0.09 / **P(Δ>0)=87%**
(real but weak bybit edge); binance p5 −0.30 / **P(Δ>0)=25%** (no binance edge — likely
degradation). Sub-period thirds: neither venue all-positive; the most load-bearing month
is 2025-11 on both.

## Original (pre-hardening) vs hardened — what the honest methodology revealed

| metric | original R1 (pre-hardening) | hardened re-baseline |
|---|---|---|
| pooled MAR Δ (drop_all_4 vs control) | +0.45 | **+0.05** |
| bybit MAR Δ | +1.30 | +0.15 |
| binance MAR Δ | −0.40 | −0.05 |
| bybit drop_all_4 ret / DD | +2.95× / −10.6% | +0.32× / −30.5% |
| binance drop_all_4 ret | +0.56× | **−0.25×** |
| Tier-2 verdict | DEMO-ELIGIBLE | **FALSIFY** |

The dominant driver is **`bar_extreme` stop fills**: max-DD roughly tripled on bybit
(−10.6%→−30.5%) and worsened on binance (−13.9%→−47.3%), and the return advantage
shrank ~9×. The cost level (15 vs 45 bps) is secondary — binance is negative at **both**.
This is exactly error #14 (warm/optimistic intrabar fill): the original R1 covered stops
at the trigger, which a live executor gapping through cannot guarantee.

## Implications

1. **The R1 re-baseline cascade premise is FALSIFIED.** R2/R13/R5 were carried forward
   "vs the drop_all_4 stack"; that stack is **not demo-eligible** under honest execution.
   Those phases' deltas were also measured pre-hardening (R13 especially interacts with
   `bar_extreme`) — they do not stand as demo evidence either.
2. **R9 `R9_event_only` baseline = the production profile**, NOT drop_all_4 (the plan:
   "production OR drop_all_4 if R1 promoted it" — R1 did not promote it under hardening).
   Note the production baseline is itself weak-to-negative under honest costs (bybit
   MAR 0.15 / −33% DD; binance MAR −0.14, negative return).
3. **The program continues to its pre-registered test of the INTEGRATED stack (R9).**
   drop_all_4 alone falsifying does NOT pre-judge R9: the R4 **factor-exposure caps**
   directly target the −33%/−47% DD blowout (bounding basket beta), the IC signal adds
   selectivity, and the R5 risk sizing + R6 model cost apply. R9 gets its honest test;
   if the best R9 cell ALSO falsifies Tier-2 → **default: do nothing** (strategy stays at
   the frozen promoted profile).

## Integrity

- **Full-PIT** both venues (`run_label=full_pit_universe`); engine aborts on coverage gaps.
- `bar_extreme` is the **conservative-honest** stop model (worst-case intrabar fill);
  reality sits between trigger and extreme, so the true edge is *between* the optimistic
  original and this pessimistic bound — but the pre-registered methodology is `bar_extreme`,
  and the FALSIFY holds at the honest 15 bps cost regardless.
- No decision threshold loosened; this is the literal execution of the hardening pre-reg's
  re-baseline rule. cost_multiplier left at 3 (unchanged) to isolate the hardening effect.

## DECISION

- **`R1_drop_all_4`: NOT demo-eligible** under the hardened engine (Tier-2 FALSIFY).
- **Re-baseline cascade premise falsified.** R9 uses the production baseline for
  `R9_event_only`; R2/R13/R5 carry-forwards are no longer demo evidence on their own.
- **Proceed to R9** (integrated stack: event entries + IC signal + R4 factor caps + R5
  sizing + R6 cost) and test it under the honest engine. Default to **do nothing** if R9
  falsifies.

## Next

Build R9 assembly (IC-augmented signal score + R4 basket factor-exposure caps + the
7-cell harness) → run R9 cells under hardened defaults → R10 demo-candidate gate
(residual Sharpe + recost + stress) → R11 OOS. R7/R8 analyzers feed R10.
