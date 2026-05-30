# Pre-registration: P3b — validated backtest of the residual-momentum SELECTION gate

**Date:** 2026-05-30
**Author:** quant-researcher (autonomous loop; operator-greenlit the engine build)
**Stage:** run-pending
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

- bybit M = `<filled post-precompute>`
- binance M = `<filled post-precompute>`

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

## Post-run results / Verdict

(pending)
