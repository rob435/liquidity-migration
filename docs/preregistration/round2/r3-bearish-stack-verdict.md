# R3 — Bearish-stack honest test (H2 retried) — VERDICT (full-PIT)

**Date:** 2026-05-29
**Pre-reg:** [integrated-strategy-program.md](integrated-strategy-program.md) sub-phase R3.
**Run label:** `exploratory` (full-PIT, Tier-1 Investigation, in-sample).
**Tools:** `apply_decision_rule.py --rule investigation --control R3_baseline_v2`. Dispatcher `scripts/r3_bearish_stack_sweep.py` (cd2f3dd). Tag `r3_bearish_stack_2026-05-29`.

## Headline

**H2 is DECISIVELY CLOSED.** The honestly mirror-imaged bearish stack produces
**0 trades on BOTH venues** — not because the quality gates exclude bearish names
(Phase-2's "falsified-by-construction"), but because the **load-bearing
`turnover_ratio ≥ 6.0` volume-spike event trigger is structurally a pump
detector**: a 6× volume spike co-occurring with rank *deterioration* + a down day
+ residual ≤ −0.08 is an empty population in this universe/window. The
deterioration direction has no tradeable population under the event trigger. **No
bearish R9 line; `R3_market_neutral` not run** (it was conditional on
`R3_bearish_only` clearing the Investigation bar).

## Results (full-PIT; control = drop_all_4 bullish)

| cell | bybit trades / ret / DD | binance trades / ret / DD | Tier-1 |
|---|---|---|---|
| R3_baseline_v2 (control) | 816 / +2.95× / −10.6% | 509 / +0.56× / −20.7% | — (reproduces drop_all_4) |
| **R3_bearish_only** | **0** / 0 / 0 | **0** / 0 / 0 | **reject** (falsifier: trades 0 < 30/20) |

The bearish mirror applied: `rank_direction=deterioration` (rank Δ ≤ −150),
`residual_return ≤ −0.08`, `close_location ≤ 0.70`, `day_return ≤ 0` (bullish
`*_min` bounds turned off), keeping the R1-confirmed load-bearing filters
(`turnover_ratio ≥ 6.0`, `event_rank_fraction ≤ 0.90`, crowding, universe_rank,
pit_age) and the drop_all_4 entry-population drops. Validation passed
(`min ≤ max`).

## Integrity / not-a-bug

The 0-trades is a **real empty filter result, not a bug**: the bullish control ran
identically to R1/R13/R5 `drop_all_4` (816/509 trades), and the bearish mechanism
is fully engine-supported (CLI flags + filter + `min ≤ max` validation + existing
tests). The bearish cell simply matches no events. Both cells `full_pit_universe`.

## Findings

- The liquidity-migration strategy is fundamentally a **"fade the volume-spike
  pump"** short. There is no symmetric "short the volume-spike dump" population:
  capitulation events (6× volume + rank deterioration + down day) are vanishingly
  rare here. This is a *deeper* characterization than Phase 2 — the binding
  constraint is the **event trigger**, not the directional quality gates.
- Round-1 Phase-2's bug-driven bearish trades were "capture-by-accident," not
  edge. Confirmed: even with appropriate bearish filters there is nothing to
  capture.

## DECISION

- **H2 closed.** No bearish / deterioration-direction line proceeds to R9.
- `R3_market_neutral` (separate long+short slot pools) is **not run** — it was
  pre-registered as conditional on `R3_bearish_only` being investigation-positive,
  which it is not (0 trades). A long+short basket would have no short leg.
- R9 proceeds with the bullish event-driven stack only:
  `drop_all_4` entries + `ff6_4pct` exit + dollar-equal sizing + 1 composite IC factor.

## Next

R4 risk-factor model (foundational code) → R6 cost model → R12 sniper → C0–C3 →
R9 assembly → R10 → R11 OOS.
