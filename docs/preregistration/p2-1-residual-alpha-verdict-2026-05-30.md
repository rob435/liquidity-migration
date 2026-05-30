# P2-1 verdict — residual-alpha decomposition of the age gate

**Date:** 2026-05-30
**Stage:** run-complete (EXPLORATORY-grade infra reuse; the residual-Sharpe number is the literal Tier-3 gate metric)
**Plan:** [research_plan_part2.md](../research_plan_part2.md) §P2-1
**Scripts:** `scripts/p2_1_residual_alpha.py` (v1), `scripts/p2_1b_residual_alpha_v2.py` (resolution-fixed)
**Data:** `~/SHARED_DATA/p2_1b_residual_alpha_2026-05-30.json`

## Question

Is the discrete age-gate edge **real idiosyncratic alpha**, or just exposure to known priced
factors (the gate removes high-vol freshly-listed names → a low-vol / short-beta tilt)? Decompose
the baseline (age90) and age300 ledgers (realistic 15 bps E2 run) through the validated 6-factor
risk model; the Tier-3 gate is **annualized residual Sharpe ≥ +0.3**.

## Result (annualized residual Sharpe)

| | bybit full6 | bybit common4 | binance full6 | binance common4 |
|---|---|---|---|---|
| resolved fraction | **1.00** | **1.00** | 0.63 (✗ untrust.) | **1.00** |
| baseline (age90) | −0.08 | −0.99 | +0.22 | −0.42 |
| **age300** | **+0.27** | **−0.12** | +0.43 | **+0.52** |

`common4` = the 4 klines/price factors (`btc_beta, xs_rank_ret_30d, realized_vol_rank,
liquidity_rank`); `full6` adds `funding_rate_z, premium_index_z`. **binance `funding_rate_z` is
38.8% null** → binance `full6` resolves only 0.63 (untrustworthy, per the pre-registered ≥0.70
falsifier); binance `common4` resolves 1.00. So the apples-to-apples cross-venue model is `common4`
(both venues 1.00); `full6` is the validated model but clean only on bybit.

## Verdict — mostly factor exposure; residual alpha borderline, NOT a clean Tier-3 pass

1. **The baseline strategy is pure factor exposure.** Its residual Sharpe is strongly *negative*
   under the robust `common4` model on **both** venues (−0.99 bybit, −0.42 binance) — it actually
   *underperforms* its own factor exposure. The baseline's impressive raw Sharpe (≈ +1.1) is
   entirely priced factor premia (short high realized-vol / low-liquidity alts), not alpha.
2. **The age gate sharply reduces factor dependence.** age300 improves the residual massively vs
   baseline on every venue/model (e.g. bybit `full6` explained collapses +0.00107 → +0.00012). The
   gate strips the most factor-loaded (young/high-vol) names.
3. **But the age-gated residual alpha is borderline and not cross-venue-robust at +0.3.** binance
   clears it (`common4` +0.52); bybit is marginal and **model-sensitive** (`full6` +0.27, just
   under the gate; `common4` −0.12, negative). It does **not** cleanly pass Tier-3 on both venues.
4. **Annualization caveat (makes the above optimistic):** residual Sharpe is annualized by
   √(trades/yr), which assumes independent trades. With `max_active=12` + event clustering the
   effective independent count is much lower, so the true annualized residual Sharpe is **below**
   these numbers — i.e. even binance's +0.52 is an upper bound likely nearer the gate or under it.

## What this means (honest)

The discrete strategy — including the age-gated version — is **primarily a factor-harvesting
vehicle** (it earns the realized-vol / liquidity / beta premia by systematically shorting
high-vol, low-liquidity alts). The age gate is a real, robust **risk-reduction + factor-exposure-
reduction** improvement (it removes the worst factor-loaded names and roughly halves DD), and it
leaves a *possible* small idiosyncratic residual — but that residual is **marginal, model-
sensitive, and not a clean cross-venue Tier-3 pass.** This corrects the premature "the age gate
purifies factor exposure into ~90% alpha" read (that was the bybit `full6` cell alone, before the
`common4` robustness + the annualization caveat).

**Implication for real money:** the returns are substantially **crowdable priced-factor premia**,
not unique idiosyncratic alpha — available in part more directly/cheaply via factor trading, and
more exposed to factor-premium decay. This does **not** kill the strategy (a robust factor-harvesting
short at MAR 3–6 with halved DD is still a legitimate demo-candidate), but it correctly **tempers
the "we found alpha" framing.** Forward demo stays informative (does the factor exposure + small
residual persist OOS?); the Tier-3 residual-Sharpe gate is **not** robustly met as-is.

## Follow-ons

- P2-2 (better maturity gate) and P2-3 (best combined profile) proceed, but with this framing: the
  bar is now also "does it improve the **residual**, not just the raw return / MAR."
- A cleaner residual-Sharpe estimate would use an overlap-aware (e.g. Newey-West / block) annualization
  instead of √(trades/yr) — noted for any Tier-3 promotion attempt.
