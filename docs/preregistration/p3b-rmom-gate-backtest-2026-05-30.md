# Pre-registration: P3b — validated backtest of the residual-momentum SELECTION gate

**Date:** 2026-05-30
**Author:** quant-researcher (autonomous loop; operator-greenlit the engine build)
**Stage:** run-complete
**Plan:** [research_plan_part2.md](../research_plan_part2.md) §P3 (certification)
**Follows:** [p3-residual-momentum-verdict-2026-05-30.md](p3-residual-momentum-verdict-2026-05-30.md)
**Engine:** the residual-momentum gate is now integrated (commit 17df8ba): config
`liquidity_migration_residual_momentum_max`, signal `<root>/residual_momentum.parquet`.

## What's changing

Certify the P3 residual-momentum lead with a *real engine backtest* (not a ledger sim). On the
age300 candidate pool, add the gate `--liquidity-migration-residual-momentum-max = M` (keep LOW
residual-momentum = short the idiosyncratically-weak names). Compare gated vs ungated, both venues,
at the realistic baseline (15 bps, max_active=12, bar_extreme_capped, full-PIT, 2023-04→2026-05).

## Threshold (pre-specified rule, not mined)

`M = per-venue median of residual_momentum` over the signal panel — reproduces the 50/50 low/high
split that P3-1b/P3-3/P3-4 validated, as a deployable fixed threshold. The median values are read
from the precompute and recorded here BEFORE the backtest. (A single pre-specified rule — the
median split — per venue; no threshold search.)

- bybit M = **+0.137677** (median over 576 age300 trades; frac≤med=0.50)
- binance M = **+0.114803** (median over 306 age300 trades; frac≤med=0.50)

(Note: the median is positive — age300 candidates are idiosyncratically strong, so a naive 0.0
cut would over-filter; the median is the principled 50/50 split the precheck validated. Signal:
bybit 445,985 / binance 406,002 rows in `<root>/residual_momentum.parquet`.)

## Hypothesis + predicted direction

The prechecks (PIT-clean, cross-venue) imply the gate should: raise MAR / Sharpe and cut DD vs the
ungated age300, keep trade count above Tier-2 mins (≈ half of age300: ~290 by / ~150 bn), hold the
recent third, and lift the **overlap-aware residual Sharpe** toward/over the Tier-3 +0.3 gate. The
ledger sim (P3-4) predicted a large directional MAR/Sharpe improvement (magnitudes inflated there;
this engine run gives the real numbers, incl. the max_active refill the sim ignored).

## Decision rule (a priori)

1. **Tier-2 demo-arbiter** (`scripts/r1_robustness.py --sweep-tag p3b_rmom_gate_2026-05-30
   --control 00_baseline`): gated cell must beat ungated age300 on pooled MAR Δ cross-venue,
   stay return-positive both venues, hold the recent third, ≥30 by / ≥20 bn trades.
2. **Tier-3 residual (the real prize):** decompose the gated cell's ledger and require an
   **overlap-aware (weekly-block) residual Sharpe ≥ +0.3 cross-venue.** If met → first certified
   factor-neutral alpha; if not (e.g. bybit stays ~marginal) → the gate is a strong return/risk
   refinement but not certified Tier-3 alpha — documented honestly.
3. **Falsifier:** if the engine gate does NOT improve MAR cross-venue (e.g. the max_active refill
   replaces dropped trades with equally-bad ones), the ledger-sim direction didn't survive the real
   engine — honest null for the gate as a portfolio improvement.

## Roots touched

- [x] bybit_full_pit (+ residual_momentum.parquet)
- [x] binance_full_pit (+ residual_momentum.parquet)
- [ ] forward demo/paper

## Run command

```bash
.venv/bin/python -u scripts/precompute_residual_momentum.py   # signal (done first)
PHASE=p3b_rmom_gate_2026-05-30 bash scripts/p3b_rmom_gate_dispatch.sh   # 00_baseline + 01_rmom_gated per venue
.venv/bin/python scripts/r1_robustness.py --sweep-tag p3b_rmom_gate_2026-05-30 --control 00_baseline
.venv/bin/python scripts/p2_1b_residual_alpha_v2.py  # (adapt) decompose the gated cell, overlap-aware
```

## Post-run results

Run 2026-05-30, sweep tag `p3b_rmom_gate_2026-05-30`, full-PIT both venues, 15 bps, max_active=12.
The `00_baseline` (age300) cells reproduce e2's `02_age_min` exactly (deterministic ✓); the gate
demonstrably fired (PIT-clean signal join; trade counts dropped to the low-rmom half + max_active refill).

| | ret | DD | Sharpe | trades | MAR (monthly-DD, r1) | weekly residual Sharpe (recent) |
|---|---|---|---|---|---|---|
| bybit age300 | +0.98 | −16% | 1.59 | 579 | 1.47 | −0.68 (+0.48) |
| **bybit + gate** | **+2.10** | **−3.3%** | **3.81** | 310 | 13.0 | **+0.00 (+2.18)** |
| binance age300 | +0.32 | −11% | 0.96 | 307 | 0.83 | +0.13 (+0.45) |
| **binance + gate** | **+0.90** | **−4.0%** | **2.93** | 169 | 5.80 | **+1.10 (+1.98)** |

**Tier-2 (r1_robustness): DEMO-ELIGIBLE.** Pooled MAR Δ +8.25; both venues all-thirds-positive,
LOO no-flip, bootstrap MAR-Δ p5 = +11.8 (bybit, P=97%) / +10.7 (binance, P=100%); recent third hugely
improved (bybit +25%→+104%, binance +4%→+53%); trades 310/169 above the minimums. The strongest,
most robust Tier-2 result in the program. (MARs are DD-inflated — the *robust diagnostics*, not the
MAR magnitude, are the headline.)

**Tier-3 (overlap-aware weekly residual Sharpe): NOT a clean cross-venue certification.** The gate
factor-neutralizes both venues (bybit −0.68→+0.00, binance +0.13→+1.10). **binance clears the +0.3
gate (+1.10)** = genuine factor-neutral alpha; **bybit is residual-NEUTRAL full-window (+0.00)** with
its residual alpha concentrated recently (+2.18). So it is binance-yes / bybit-marginal — borderline,
recency-tilted (the c2b caveat applies).

## Verdict

**The residual-momentum SELECTION gate is a VALIDATED, robust, cross-venue Tier-2 DEMO-CANDIDATE —
the program's payoff — but NOT a clean cross-venue Tier-3 alpha certification.** Honestly:
- It dramatically improves risk-adjusted return on both venues (return 2–3×, Sharpe doubled, DD
  halved), all-thirds-positive, LOO-stable, bootstrap-robust → **DEMO-ELIGIBLE**.
- Its cross-venue mechanism is **risk-reduction + factor-neutralization + venue-asymmetric / recent-
  concentrated residual alpha** (binance certified +1.10; bybit factor-neutral full-window, +2.18
  recent). It is *not* a fully-certified all-weather idiosyncratic-alpha engine.
- The DD-inflated MARs (13 / 5.8 monthly) are not the headline; the robust diagnostics + the residual
  Sharpe are.

**Recommendation (operator-gated):** **forward-demo the residual-momentum gate** (a robust, validated,
demo-eligible risk-adjusted improvement). Frame it as a risk-adjusted-return + factor-neutralization
improvement with binance-side / recent residual alpha — NOT a certified cross-venue alpha engine. The
forward demo (the real Tier-3 arbiter) tests whether the binance residual alpha and the recent bybit
residual persist OOS. Engine integration is committed (17df8ba); the gate defaults inactive, so the
promoted profile is unchanged until you choose to move it.
