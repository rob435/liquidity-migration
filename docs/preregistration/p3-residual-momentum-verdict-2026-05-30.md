# P3 verdict — residual-momentum selection: a real refinement + a promising (uncertified) alpha lead

**Date:** 2026-05-30
**Stage:** run-complete (EXPLORATORY prechecks; not a validated backtest)
**Plan:** [research_plan_part2.md](../research_plan_part2.md) §P3 (extending Part 2)
**Scripts:** `p3_1_residual_selection_ic.py`, `p3_1b_residual_selection_v2.py`, `p3_2_residual_gate_validation.py`
**Data:** `~/SHARED_DATA/p3_1b_residual_selection_2026-05-30.json`, `p3_2_residual_gate_validation_2026-05-30.json`

## Question

Part 2 found the age-gated book roughly factor-neutral with a borderline residual. P3 asks: is that
residual **extractable** via selection? Test: does **trailing factor-residual momentum** (rmom — the
name's cumulative factor-model residual over the trailing 7d, PIT) predict which age300 candidates are
the best shorts, and does selecting on it lift the **residual Sharpe** to a Tier-3 pass?

## Results

**The signal is robust, PIT-clean, cross-venue (P3-1b, common4 residuals, full coverage):**
`rmom7_lag1` (strict signal-close PIT — excludes the signal-day residual) IC vs net_return =
**−0.19 bybit (99% matched) / −0.35 binance (100%)** — i.e. **short the idiosyncratically-weak
candidates.** It survives the PIT lag (incl −0.22/−0.39 → lag1 −0.19/−0.35), is same-sign cross-venue,
and the tercile net spread is +0.58% / +0.91% per trade.

**Selecting on it clears the Tier-3 residual gate on both venues — but with a caveat (P3-2):**

| | IC(rmom,net) | IC(rmom,**resid**) | recent resid IC | ann residual Sharpe: full → **LOW-rmom** → high |
|---|---|---|---|---|
| bybit | −0.19 | −0.08 | −0.14 | −0.13 → **+0.47** → −0.58 |
| binance | −0.35 | −0.03 | −0.06 | +0.52 → **+1.25** → −0.34 |

The low-rmom (selected) half clears +0.3 on both venues and holds in the recent third; the discarded
high-rmom half is negative on both.

## Verdict — a genuine selection refinement; promising but UNCERTIFIED as Tier-3 alpha

1. **Solid:** rmom is a robust, PIT-clean, cross-venue, recent-holding **selection signal that
   strongly improves NET return.** Shorting the idiosyncratically-weak liquidity-migration candidates
   is a real refinement on top of the age gate.
2. **The honest caveat:** `IC(rmom, residual)` is **weak** (−0.08 / −0.03) while `IC(rmom, net)` is
   strong (−0.19 / −0.35) — so rmom predicts net return **mostly through factor exposure**, only weakly
   through the residual. That is *inconsistent* with the large LOW-rmom residual-Sharpe lift
   (+0.47/+1.25), which is therefore likely **inflated by the optimistic per-half √(trades/yr)
   annualization** (overlapping trades; different per-half spans). So the "clears Tier-3 residual"
   reading is **suggestive, not certified.**
3. **Net:** a valuable selection refinement that improves the (still largely factor-harvesting)
   strategy's returns, and the **most promising residual-alpha lead in the program** — but it is not
   established unique alpha. (I am explicitly not repeating this session's earlier "alpha purifier"
   over-claim.)

## Recommended next step (operator-gated — a real engine build)

To **certify** this, the residual-momentum signal must be built into the engine's candidate selection
(compute rmom at each candidate's decision_ts as a PIT selection filter — integrating `risk_model`
into the `volume_events` selection path), then backtested full-PIT both venues with the full
robustness battery + an **overlap-aware (block/Newey-West) residual-Sharpe** annualization (not
√(trades/yr)). That is a meaningful code change with look-ahead risk — **scope + operator OK before
building.** The precheck evidence here is strong enough to *justify* that build (unlike the C0 engine,
which the c2b precheck did not justify). If built and the overlap-aware residual Sharpe holds ≥ +0.3
cross-venue, this would be the program's first **certified factor-neutral alpha** and a genuine
Tier-3 candidate.

## Honest program status after Part 3

The strategy is a **robust factor-harvesting short** (Part 2); the **age gate factor-neutralizes** it;
and **residual-momentum selection** further improves it and *points at* extractable residual alpha
(strong net IC, cross-venue, PIT-clean) without yet certifying it. The two remaining gates are both
real and concrete: (a) build+validate the residual-momentum gate (operator-gated code change), and
(b) forward demo (operator-gated). No more cheap in-sample prechecks remain — the leads are now builds.
