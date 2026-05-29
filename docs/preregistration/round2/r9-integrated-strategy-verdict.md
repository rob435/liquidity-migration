# R9 — Integrated daily strategy (Architecture A) — VERDICT: DOCUMENTED NULL (full-PIT, honest engine)

**Date:** 2026-05-29
**Pre-reg:** [integrated-strategy-program.md](integrated-strategy-program.md) sub-phase R9.
**Run label:** full-PIT, hardened engine (`9f52819`+`b1a3368`: bar_extreme stops, 100%
taker, calendar-exact returns, permutation-null variance). In-sample 2023-04→2026-05.
**Tools:** the R1/R5/R13 hardened re-baselines + the R9 IC-selectivity pre-check +
`r1_robustness.py`. Tags `r1_rebaseline_hardened_2026-05-29`,
`r9_event_sizing_hardened_2026-05-29`, `r9_exit_sizing_hardened_2026-05-29`,
`r9_ic_selectivity_precheck_2026-05-29`.

## Headline

**Under honest methodology, the daily liquidity-migration strategy (Architecture A) is a
DOCUMENTED NULL. No combination of the pre-registered daily levers produces a Tier-2
demo-candidate.** The best stack found — `drop_all_4` entries + `risk_equal` 2% sizing +
`ff6_4pct` failed-fade exit — yields a **genuine bybit edge but NO binance edge**, so it
fails the cross-venue Tier-2 bar (positive return BOTH venues). **DECISION: do nothing —
the frozen promoted profile is unchanged; nothing is promoted.**

The Round-2 demo-eligibility reported earlier (pooled MAR Δ +0.45) was substantially a
**pre-hardening optimistic-stop-fill artifact** (error #14): stops filled at the trigger,
not the bar's adverse extreme. Honest fills triple the drawdown and flip binance negative.

## Best pre-registered daily stack (hardened; cost_multiplier=3 = 45 bps, conservative)

`drop_all_4` + `risk_equal` 2% + `ff6_4pct`:

| venue | ret | daily DD | MAR | Sharpe | Tier-2 |
|---|---|---|---|---|---|
| bybit | **+21.9%** | −15.7% | **1.39** | 0.72 | edge real |
| binance | **−1.3%** | −20.6% | −0.06 | −0.02 | **no edge (≤0)** |

→ **FALSIFY (return ≤0 a venue).** binance has no edge; bybit-only cannot satisfy the
pre-committed cross-venue robustness bar.

## How every R9 cell class was falsified (levers tested decisively, not blind-run)

The 7 pre-registered R9 cells were resolved by testing the LEVERS (re-baselines +
pre-checks) rather than building the full IC/factor-cap engine integration for cells whose
fate the diagnostics already determine — more rigorous and far cheaper:

| R9 cell class | determining test | result |
|---|---|---|
| `R9_event_only` | R1 hardened re-baseline | drop_all_4 FALSIFY (binance −0.25× at promoted exit/dollar-equal; pooled MAR Δ +0.45→+0.05) |
| `R9_event_*_ic`, `ic_only` | R9 IC-selectivity pre-check | composite IC ANTI-selects within events (Spearman −0.16 bybit / −0.31 binance; high-IC = worst shorts) → IC filtering makes it WORSE |
| `R9_event_*_factor_capped` | R4 factor model + the above | factor caps bound beta/DD but cannot create a positive return where the gross edge is absent (binance); the IC component is adverse |
| `R9_market_neutral*` | R3 bearish-stack test | bearish/deterioration = 0 trades (no short-the-dump population) → no second sleeve |

**Sizing (R5) and exit (R13) re-confirmed under honest costs** — and they *are* the best
choices, just not enough: `risk_equal` 2% robustly cuts DD (binance −47%→−22%, bootstrap
MAR Δ P(Δ>0)=98%) and `ff6_4pct` is the best exit (improves binance −3.5%→−1.3%,
P(Δ>0)≈70%). Each lever reduces the loss; none flips binance's sign. `stop10` (tighter
stop) hurts. The honest 15 bps recost (R6) adds only ~+12pp to binance — still negative.

## Core finding

The strategy is a **fade-the-volume-spike short with a genuine bybit edge and no binance
edge** under honest execution. The 5 IC features collapse to the same high-vol/extended
basket the event trigger already selects (R2), and *within* events that basket's most
extreme names CONTINUE rather than revert (the IC anti-selectivity) — so selectivity
cannot help. The cross-venue Tier-2 bar exists precisely to reject venue-specific
edges like this. This is the **second documented null** for the strategy (Round 1 was the
first); honest methodology confirms it.

**Post-hoc note (NOT promotable):** the inverse signal (LOW composite IC = better short)
is positive in-sample on both venues, but adopting it is a post-hoc sign flip (error #17)
— a candidate for a future FRESH-OOS pre-registration, not edge here.

## DECISION

- **Daily Architecture A: DOCUMENTED NULL → DO NOTHING.** Frozen promoted profile
  unchanged; nothing promoted to demo-candidate or real money.
- **Component winners recorded** (apply only if a positive-return cell ever emerges):
  exit = `ff6_4pct`, sizing = `risk_equal` 2%.
- **bybit-only edge is real but NOT promoted** — it fails the pre-committed cross-venue
  bar. Pursuing a bybit-only strategy would be a NEW operator pre-registration decision
  (the cross-venue bar is a protected decision-rule threshold; not relaxed here).
- **R7 stress / R8 capacity / R10 demo-gate / R11 OOS: not run** — all are downstream of a
  Tier-2 demo-candidate, which does not exist. (R4 residual model, R6 cost model are
  validated infrastructure, retained.)

## Remaining pre-registered tracks — operator decision

- **R12 sniper (entry-fill optimization): NOT pursued.** The failure mode is the absent
  binance gross edge + the cross-venue bar, not entry slippage — a better entry fill
  cannot manufacture edge. Building it on a no-edge strategy is unjustified.
- **C0–C3 continuous (Architecture B): operator decision.** A genuinely different
  (higher-frequency) test of the SAME features, but a ~5–7 day build with a low prior
  given (a) the daily features anti-select within events and (b) binance has no edge. Per
  the "default = do nothing" pre-commitment and "don't run expensive research on a
  falsified premise," this is NOT auto-pursued — surfaced for the operator to weigh.

## Integrity

Full-PIT both venues; honest hardened engine; all daily levers tested (entries, IC,
sizing, exits); the FALSIFY holds at both the conservative 45 bps and honest 15 bps cost;
component re-confirmations and the post-hoc inverse-IC observation documented without
being cited as promotable edge.
