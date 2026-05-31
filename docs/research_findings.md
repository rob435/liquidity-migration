# Research Findings — verdict pointer

**Updated 2026-05-31.** This file is a short pointer. The single consolidated
research record is **[docs/research_summary.md](research_summary.md)**; live /
operational state is **[STATE.md](../STATE.md)**.

## Verdict

The signal is statistically real but regime-conditional; the earlier "Round 2 =
documented null" verdict has been **retracted** (substantially a methodology
artifact). Under realistic capped fills the daily strategy is gross-positive on
both venues in-sample. See **[docs/research_summary.md](research_summary.md)** for
the dated postmortem and the figures, and **[STATE.md](../STATE.md)** for live
state. Results remain in-sample; the forward demo is the arbiter and real money
stays off.

## The frame that matters

The strategy is two separable layers:

1. **Selection (initial signal)** — the liquidity-migration event picks a
   *candidate pool* (mid-liquidity perp takes price-insensitive flow). This is
   **not** an entry.
2. **Execution (entry signal)** — the in-migrated flow exhausts and **fades**.
   You do not short the top (the pump can continue — the extremes even continue
   *up* first); the deployed entry waits ~a day for the fade to settle, then
   shorts. This is a fade strategy, not a catch-the-top strategy.

**The alpha is SELECTION, not execution (E1, 2026-05-30).** A controlled test on
the same pool — immediate +1h entry vs fade-confirmation — found fade-confirmation
adds no robust cross-venue premium, so the entry/fill-timing axis is a non-lever
(E3/sniper dropped). The **intraday-detection kernel** that followed is now closed:
faster *detection* of the same event is un-capturable (K1a), and the purpose-built
intraday burst-short is a real but **marginal, unvalidated** standalone (funding eats
~85% of it — full write-up `intraday_burst_synthesis.md`). The robust, already-validated
open lead is therefore **SELECTION refinement** — the **age gate** (`pit-age-days-min≈300`)
and the **residual-momentum gate** — under forward demo (operator-gated). Forward plan:
**[docs/research_plan_intraday_kernel.md](research_plan_intraday_kernel.md)**.

## Methodology

See **[docs/backtesting_errors_we_never_repeat.md](backtesting_errors_we_never_repeat.md)**
— the standard every run clears before its numbers can be cited. No real-money
deployment claim is made beyond what the evidence supports; the VPS forward demo
is the forward evidence, and the strategy is not real-money-validated.
