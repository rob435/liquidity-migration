# Research Findings — verdict pointer

**Updated 2026-05-29.** This file is a short pointer. The single consolidated
research record is **[docs/research_summary.md](research_summary.md)**; live /
operational state is **[STATE.md](../STATE.md)**.

## Verdict (corrected 2026-05-29)

**The strategy is not a null — and "Round 2 = documented null" has been
retracted.** That conclusion was substantially a *methodology artifact*
(worst-case `bar_extreme` stop fills + `max_active=3` over-concentration + a ×3 /
45 bps cost stacked together) compounded by a *selection/execution conflation*.

Under the realistic capped stop fill (`bar_extreme_capped`, 10%) at
`max_active=12`, the **daily** strategy — which uses the fade-confirmation
execution — is **positive on both venues in-sample** (bybit +37.8% / −27.5% DD /
Sharpe 0.70; binance −4.7% net but **gross +16.1%**, ~breakeven at honest 15 bps).
Both venues are gross-positive. It remains in-sample; the forward demo (since
2026-05-22) is the arbiter; nothing is promoted; real money stays off.

## The frame that matters

The strategy is two separable layers:

1. **Selection (initial signal)** — the liquidity-migration event picks a
   *candidate pool* (mid-liquidity perp takes price-insensitive flow). This is
   **not** an entry.
2. **Execution (entry signal)** — the in-migrated flow exhausts and **fades**.
   You do not short the top (the pump can continue — the extremes even continue
   *up* first); you wait for the fade to **confirm**, then short — "**fade the
   fade**." This is a fade strategy, not a catch-the-top strategy.

The **continuous** candidate signal carries real, robust cross-venue selection
IC, but was only ever tested with *immediate* entry — so its "not tradeable"
label is about timing the top, not the signal. Applying + refining the
fade-confirmation execution on that candidate pool is the open lead. The forward
plan is **[docs/research_plan_selection_execution.md](research_plan_selection_execution.md)**.

## Methodology

See **[docs/backtesting_errors_we_never_repeat.md](backtesting_errors_we_never_repeat.md)**
— the standard every run clears before its numbers can be cited. No real-money
deployment claim is made beyond what the evidence supports; the VPS forward demo
is the forward evidence, and the strategy is not real-money-validated.
