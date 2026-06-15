# Pre-registration: W5 Continuous Stage 2 - Entry-style (funding selection + decel note)

**Date:** 2026-06-15
**Author:** Claude (W5 continuous signal-alpha loop)
**Label:** `exploratory` (screen)
**Contract:** `00_methodology_contract.md`; applies the Stage 4d lesson (screen before engine;
robustness/controls before any candidate claim).

## Question

After the entry-priority (Stage 1), path-shape (Stage 5/7b), and liquidity (Stage 4) levers,
is there a causal ENTRY-STYLE signal that improves both-venue fade EV? Two candidates examined
BEFORE any engine spend:

1. **Deceleration entry filter** (existing `entry_decel_*` knobs: skip entries whose recent
   `close[entry]/close[entry−lookback]−1` is still high — "still ripping up").
2. **Entry funding** (the fade shorts pumped alts that carry high positive funding; high
   funding may proxy crowding → stronger reversion; funding is causal and time-varying so it
   escapes the symbol-identity trap that sank Stage 5).

## Pre-run evidence checks (the disciplined kill)

**Decel filter — NEGATIVE PRIOR, not run.** Selected entries have large positive `pre_6h_return`
(median +11% bybit / +9% binance), and the engine's decel test is `close[entry]/close[entry−6h]`
≈ the 6h pump size. Stage 7b found bigger pumps fade BETTER (pre_6h/pre_24h_return positive IC,
both venues). So the decel filter at any reasonable threshold drops the biggest-recent-pump
entries = the BEST fades → expected to HURT. The engine `entry_decel_*` knob is a 6h-magnitude
filter, not a true acceleration filter; with the IC pointing the other way it is closed without
an engine run (a 2.3h sweep with a negative prior is not worth it).

**Funding selection — screen.** Statistic = Stage 7b within-symbol partial rank-IC over
composite (1000-perm null, degenerate symbol-hash control). Funding at entry = last
funding_rate at or before `signal_ts` (causal asof-join). Admissible (⇒ engine
funding-weighted-sizing test) iff both venues, same sign, p<0.025, ≥2/3 thirds, control
degenerate.

## Post-run results (funding screen)

Run UTC 2026-06-15, both venues. Within-symbol partial funding IC over composite: **bybit
+0.032 (p=0.111), binance +0.051 (p=0.028)** — signs consistent (both positive, the
crowding/carry direction) but WEAK and NOT significant (bybit clearly NS; binance just misses
the 0.025 bar). Symbol-hash control degenerate both venues. Coverage > 500 both. Funding IC is
roughly half the magnitude of the path-shape / liquidity ICs (~0.08–0.13).

## Verdict

**NULL — entry-style lever closed.** Funding carries no significant within-symbol selection
signal beyond the production composite (the composite + pump size already capture the fade
edge; funding adds little). Combined with the decel filter's negative prior, the **entry-style
lever yields nothing harvestable** — consistent with the program-wide pattern that entry-set
modifications (priority, magnitude/path-shape, liquidity, decel, funding) do NOT robustly
harvest beyond the frozen control. The **BTC-vol regime-hedge (Stage 8c) remains the sole
validated both-venue edge.** No engine spend incurred (both ideas killed at the screen/evidence
stage — the Stage 4d discipline working as intended). Next distinct mechanism: a hedge
IMPROVEMENT (instrument/structure), the highest-prior untested direction since hedging is the
one proven lever.
