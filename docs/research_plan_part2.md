# Research plan — Part 2: from a validated in-sample edge to *real, promotable alpha*

**Created 2026-05-30.** Part 1 ([research_plan_selection_execution.md](research_plan_selection_execution.md))
established that the alpha is a **SELECTION** signal and found one robust, exhaustively-hardened
in-sample refinement — the **discrete age gate** (drop symbols younger than ~300 days, which
~doubles MAR cross-venue and survives threshold/regime/cost/fill stress; mechanism: young-name
shorts get squeezed). Part 1's job is done: we have a strong **in-sample Tier-2 demo-candidate.**

Part 2 asks the three questions that decide whether this is *real money* alpha and whether it can
be made better — sequenced cheapest/most-decisive first.

## P2-1 — Is the age-gate edge REAL ALPHA, or just factor exposure? (decisive, FIRST)

**Question.** The age gate works by removing high-volatility, freshly-listed names. Does its
improvement survive **stripping known systematic factors**, or is it just a low-vol / short-beta
tilt dressed up as alpha? This is exactly the **Tier-3 residual-Sharpe gate** — and the 6-factor
risk model is already built & validated (`risk_model.py`, `decompose_strategy_pnl`;
[r4-risk-model-verdict.md](preregistration/r4-risk-model-verdict.md)).

**Method.** Build the per-venue factor panel + fit daily factor returns; decompose the **baseline**
and **age300** trade ledgers (both venues, the realistic 15 bps E2 run) into factor-explained +
residual. Report per-trade and **annualized residual Sharpe** (Tier-3 gate ≥ +0.3), the
resolved-fraction (decomposition trustworthiness), and mean-explained vs mean-residual.

**Read-out / decision.**
- If **age300 annualized residual Sharpe ≥ +0.3 on both venues** → the edge is real alpha that
  survives factor stripping → Tier-3-prep PASS (on this dimension); the strongest possible case
  short of forward demo.
- If age300 residual Sharpe **< baseline** or **< 0.3** → the "improvement" is largely factor
  exposure (the gate removes vol/beta), i.e. "selling vol / buying beta," not alpha. Honest, and
  it reframes the whole finding.

**Falsifier.** Low resolved-fraction (< ~0.7) ⇒ the ledger/panel grids don't line up and the
number is untrustworthy — fix the join before believing either verdict.

## P2-2 — Is "age" the best maturity gate, or a proxy for something better? (gated on P2-1 ≠ pure-factor)

**Question.** "Age" likely proxies **liquidity/seasoning maturity** (a name has been around long
enough that its flow isn't dominated by listing-pump dynamics). Is there a *better* maturity
signal — cumulative quote turnover since listing, number of prior liquidity-migration events the
name has had, or time-since-first-large-volume-day? A cleaner proxy could be a better, more
capacity-aware gate.

**Method.** Within-selection cross-venue IC of each candidate maturity proxy vs short net_return
(exploratory, hypothesis-gen), then a **single pre-registered** gate per promising proxy backtested
full-PIT both venues vs the age gate. Decision = does any proxy beat the age gate cross-venue +
recent-third, at equal-or-better trade count? No threshold mining; one rule per proxy.

**Falsifier.** If no proxy beats age cross-venue, age is the gate — document and stop.

## P2-3 — Build + validate the best combined demo-candidate profile (the deliverable)

**Question.** Part 1's age gate + the independently-validated **component winners** from earlier
work (the `drop_all_4` filter set; `risk_equal` 2% sizing; `ff6_4pct` failed-fade exit). Does
stacking the pieces that *each* survived validation produce a **stronger, robust** profile than age
alone — the concrete thing to forward-demo?

**Method.** Pre-register one combined profile (age300 + the validated components), backtest full-PIT
both venues, run the **full robustness battery** (cross-venue, recent-third, cost, fills, and — from
P2-1 — residual Sharpe). Decision = Tier-2 demo-arbiter + does it dominate age-alone without adding
fragility (LOO, bootstrap). This is what the operator would deploy to the demo.

**Falsifier.** If the combination is fragile (LOO-flips, recent-third breaks) or doesn't beat
age-alone, recommend **age-alone** as the demo profile — simpler and already robust.

## Standards (unchanged from Part 1)

PIT-only, full-PIT, BOTH venues, pre-register every non-exploratory run, three-tier demo-arbiter
(MAR-primary), report the full distribution, **always split recent/early before believing a
full-window number** (the c2b lesson), serial cells on the 32 GB box. **Hard line:** commit (never
push), demo only, no profile change / deploy / real-money without explicit operator OK. The
remaining real gate above everything here is **forward demo** — Part 2 sharpens the case for it; it
does not replace it.

## Out of scope

- Re-mining the age threshold (Part 1 exhausted it: age300 is the recommended, stress-robust value).
- The continuous C0 engine (c2b nulled it — recent-regime-only).
- Execution-timing / sniper (E1 nulled it).
- The long sleeve (exhaustively searched in Part 1's predecessor).
