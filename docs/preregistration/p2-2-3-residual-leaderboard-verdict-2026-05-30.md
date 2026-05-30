# P2-2/3 verdict — residual-alpha leaderboard across selection configs

**Date:** 2026-05-30
**Stage:** run-complete (EXPLORATORY infra reuse; residual Sharpe = the Tier-3 metric)
**Plan:** [research_plan_part2.md](../research_plan_part2.md) §P2-2/P2-3
**Script:** `scripts/p2_2_residual_leaderboard.py` · **Data:** `~/SHARED_DATA/p2_2_residual_leaderboard_2026-05-30.json`

## Question

Residual alpha is a SELECTION property (per-trade, size-agnostic — so `risk_equal` and other sizing
cannot move it). Which selection config, if any, has robust cross-venue **residual** alpha? Decompose
the existing ledgers through the 6-factor risk model under `full6` (validated) and `common4` (drop the
sparse funding/premium factors — the only model trustworthy on BOTH venues, since binance
`funding_rate_z` is 38.8% null → binance `full6` resolves only 0.63–0.69).

## Annualized residual Sharpe

| config | bybit full6 | bybit common4 | binance full6 (untrust) | binance common4 |
|---|---|---|---|---|
| baseline (age90) | −0.08 | **−0.99** | +0.22 | **−0.42** |
| age300 | +0.27 | **−0.12** | +0.43 | **+0.52** |
| **age400** | +0.55 | **−0.04** | +0.34 | **+0.26** |
| drop_all_4 (big MAR lever) | +0.30 | **−0.53** | −0.08 | **−0.63** |
| drop_all_4_rebase | −0.09 | **−1.03** | −0.37 | **−1.00** |

## Verdict

1. **No config clears the Tier-3 residual-Sharpe gate (+0.3) cross-venue robustly.** Under the only
   both-venues-trustworthy model (`common4`) the age residuals **sign-flip** (bybit ≈0/negative,
   binance positive), so there is **no robustly-established unique idiosyncratic alpha.** (Under the
   validated `full6` model bybit age400 is +0.55 — a pass — but binance `full6` can't be verified, and
   the result is model-fragile, so this is not a robust cross-venue claim.)
2. **`drop_all_4` — the biggest *return* lever (+295% bybit in R5) — is NOT alpha.** Its residual is
   *negative* on both venues (`common4` −0.53 / −0.63); its huge MAR is **factor harvesting**, not
   idiosyncratic edge. Stacking it would add return *and* factor exposure, not alpha. (P2-3's combined
   profile is therefore not pursued — it would not improve the residual.)
3. **The robust, undeniable finding: the age gate FACTOR-NEUTRALIZES the strategy.** Under every
   model/venue the age gate moves the residual sharply *up* from the baseline — from a strongly-negative
   *inefficient factor bet* (baseline −0.99 / −0.42 common4) to **roughly factor-neutral** (age400
   −0.04 / +0.26). **Stricter age = more factor-neutral** (age400 > age300 > baseline). The age gate's
   real value is **removing the factor exposure**, not adding alpha.
4. **√(trades/yr) annualization is optimistic** (overlapping trades) — even the positive cells are
   upper bounds.

## So what (honest)

The discrete liquidity-migration short is fundamentally a **factor-harvesting vehicle** (short
high-vol / low-liquidity alts = realized-vol / liquidity / beta premia). The deployed baseline and the
big return levers (`drop_all_4`) are *inefficient* factor bets (they even underperform their own factor
exposure → negative residual). **The age gate's contribution is risk-and-factor-neutralization**: it
strips the most factor-loaded (young/high-vol) names, ~halving DD and moving the book toward factor
neutrality — which is **valuable for forward robustness** (a roughly factor-neutral book is far less
exposed to factor-premium decay / crowding than the baseline's leveraged factor bet), even though it
does **not** establish clean idiosyncratic alpha.

**Recommendation to the operator (unchanged gate, sharper framing):** forward-demo the **age gate**
(age300 conservative / age400 most-factor-neutral) — but frame it correctly: you are demoing a
**robust, factor-neutralized short**, not a unique-alpha engine. The forward demo's real question is
whether the (near-neutral) residual + the reduced factor exposure persist OOS. Do **not** expect Tier-3
residual-Sharpe ≥ +0.3 to be met as-is. The continuous engine, execution sniper, and `drop_all_4`
return-chasing are all dead ends for *alpha* (factor exposure or nulls).
