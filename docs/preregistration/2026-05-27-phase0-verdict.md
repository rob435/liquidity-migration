# Phase 0 — filter LOO audit (VERDICT)

**Date:** 2026-05-28 (sweep ran 2026-05-27, verdict generated 2026-05-28)
**Stage:** run-complete, **REJECTED** (zero candidates per the Strictness Manifesto)
**Pre-reg:** [docs/preregistration/2026-05-27-phase0-filter-loo-audit.md](2026-05-27-phase0-filter-loo-audit.md)
**Phase label per parent plan Appendix B:** `exploratory` (negative finding —
"every filter currently in production earned its weight per the Manifesto threshold")

## Headline

**0 candidates · 3 falsifiers · 11 inconclusive · 1 skip-control.**

No production filter removal is recommended. Three filters (`crowding`,
`event_rank_frac`, `turnover_ratio`) emerged as **clean falsifiers** —
their removal causes ≥-0.5 sharpe Δ AND material DD worsening on both
venues, confirming they earn their keep. The other eleven cells finished
inconclusive: none cleared the Manifesto's per-cell candidate bar
(sharpe Δ ≥ +0.5 on BOTH venues AND DD Δ ≤ -5pp on BOTH venues).

The current production filter stack stays as-is.

## Decision-rule analyzer output (verbatim)

```
# rule: manifesto  control: 00_baseline  sharpe_delta_min: +0.5  dd_delta_max: -5.0pp  min_trades: bybit=50 binance=30
cell_id                   by_sh_d   bn_sh_d   by_dd_d    bn_dd_d    by_tr  bn_tr  by_ret    bn_ret    verdict
00_baseline               +0.00     +0.00     +0.0pp     +0.0pp     602    421    +38.56x   +4.21x    skip_control
P0_noflt_close_location   +0.04     -0.32     -5.0pp     +12.6pp    612    426    +41.58x   +2.34x    inconclusive
P0_noflt_cooldown         -0.32     +0.01     +1.0pp     -2.8pp     608    423    +21.33x   +4.20x    inconclusive
P0_noflt_crowding         -0.61     -0.25     -1.4pp     +6.6pp     678    475    +16.46x   +2.64x    reject
P0_noflt_day_return       +0.02     +0.03     +0.0pp     +0.0pp     603    422    +39.88x   +4.39x    inconclusive
P0_noflt_entry_delay      -0.11     -0.31     +6.7pp     +16.1pp    644    449    +34.62x   +2.44x    inconclusive
P0_noflt_event_rank_frac  -1.37     -0.79     +16.2pp    +26.5pp    644    471    +2.90x    +0.85x    reject
P0_noflt_max_active       +0.29     -0.13     -42.0pp    -42.0pp    733    477    +0.02x    +0.01x    inconclusive
P0_noflt_pit_age          +0.04     -0.16     -3.7pp     +5.1pp     637    446    +48.68x   +3.26x    inconclusive
P0_noflt_rank_max         +0.11     +0.08     -4.9pp     -1.6pp     586    407    +49.28x   +4.95x    inconclusive
P0_noflt_rank_min         -0.42     -0.13     +0.5pp     -0.7pp     612    426    +21.10x   +3.62x    inconclusive
P0_noflt_realized_loss    +0.10     +0.00     -1.2pp     +0.0pp     607    421    +45.60x   +4.21x    inconclusive
P0_noflt_residual_return  -0.26     +0.07     -1.0pp     -6.1pp     652    484    +26.30x   +5.07x    inconclusive
P0_noflt_stop_pressure    -0.03     +0.05     +0.9pp     -4.1pp     608    427    +37.20x   +4.63x    inconclusive
P0_noflt_turnover_ratio   -1.33     -0.64     +27.1pp    +11.3pp    726    539    +3.22x    +1.22x    reject

# summary: candidates=0 rejects=3 inconclusive=11 skip_control=1

# REJECT reasons:
#   P0_noflt_crowding: falsifier: bybit sharpe Δ -0.61 ≤ -0.5
#   P0_noflt_event_rank_frac: falsifier: binance DD -68.7% worse than -60%;
#                             falsifier: bybit sharpe Δ -1.37 ≤ -0.5;
#                             falsifier: binance sharpe Δ -0.79 ≤ -0.5
#   P0_noflt_turnover_ratio: falsifier: bybit DD -69.2% worse than -60%;
#                            falsifier: bybit sharpe Δ -1.33 ≤ -0.5;
#                            falsifier: binance sharpe Δ -0.64 ≤ -0.5
```

## The three falsifiers (filters that clearly earn their keep)

### `crowding` (`--liquidity-migration-crowding-filter union_pathology`)

Removal: Bybit sharpe 2.45 → 1.85 (Δ -0.61), Binance sharpe 1.46 → 1.21
(Δ -0.25). Trade count rises (602 → 678 on Bybit, 421 → 475 on Binance)
which dilutes per-trade quality. The crowding gate is doing real work
detecting late/stalled/weak-market entries that would otherwise dilute
the basket.

### `event_rank_frac` (`--liquidity-migration-event-rank-fraction-max 0.90`)

Removal: Bybit sharpe 2.45 → 1.09 (Δ -1.37), Binance sharpe 1.46 → 0.67
(Δ -0.79). DD widens to -58% Bybit / -69% Binance. The cap on event
rank fraction is the single most load-bearing filter in the stack.
Removing it lets in candidates that score top-decile on volume rank but
are also top-decile on event-of-the-day rank — apparently a kiss-of-death
combination at the cohort level.

### `turnover_ratio` (`--liquidity-migration-turnover-ratio-min 6.0`)

Removal: Bybit sharpe 2.45 → 1.13 (Δ -1.33), Binance sharpe 1.46 → 0.82
(Δ -0.64). DD widens to -69% Bybit / -53% Binance. The turnover-ratio
floor (today's turnover ≥ 6× the prior 7d mean) is the load-bearing
"signal-day amplitude" gate. Without it, low-amplitude churn-style
events enter the basket and dilute edge.

## The eleven inconclusive cells (none cleared the +0.5 Manifesto bar)

Filed and not pursued further. The Manifesto explicitly forbids "well it's
very close" reasoning; per the pre-commitment, near-misses become evidence
that the filter is operating at its margin, not evidence that the threshold
should bend.

A FEW gates LOOK near-inactive on both venues (small |Δ|, suggesting they
rarely or never fire on the baseline-passing population):

- `day_return`: Bybit Δ +0.02, Binance Δ +0.03 (essentially no-op both venues)
- `stop_pressure`: Bybit Δ -0.03, Binance Δ +0.05 (essentially no-op)
- `realized_loss`: Bybit Δ +0.10, Binance Δ +0.00 (Binance numerically identical to baseline)
- `rank_max`: Bybit Δ +0.11, Binance Δ +0.08 (small positive, but not +0.5)

These observations are **descriptive only**. The Manifesto does NOT
permit acting on them; removing a no-op filter requires either:

1. Its own dated pre-reg with a freshly committed candidate-quality
   decision rule, OR
2. A demonstration that removing all four simultaneously clears the
   +0.5 bar on both venues (the LOO can't test combinations).

Both are out of Phase 0's pre-committed scope. Neither happens in this
verdict.

The `max_active` "inconclusive" verdict is **degenerate** rather than
informative: with `--max-active-symbols 999`, gross-exposure 1.0 spreads
across 477-733 active positions → ~0.001% notional per position →
friction eats everything. Numerically high sharpe (Bybit 2.74, Binance
1.33) but ret ≈ 0%. The position cap is doing real work; the LOO
parameter just degenerates the strategy.

## Implications for downstream phases

- **Phase 1 (universe-isolation diagnostic):** not gated by Phase 0;
  runs next. Phase 1's outcome will inform Phase 2's interpretation
  (does universe widening explain the DD shift?).
- **Phase 2 (rank-direction grid):** not gated by Phase 0; runs next.
  Phase 2 tests a different axis (rank direction + threshold) so no
  Phase 0 finding short-circuits it.
- **Phase 5 (signal-research harness):** not gated by Phase 0.

## Open follow-up (NOT acted on in this phase)

The pattern "three gates appear to be no-ops on both venues" is the
strongest secondary observation from Phase 0. If the operator wants to
test "remove all four no-op-looking gates simultaneously", that would
require:
- A new dated pre-reg referencing this verdict as motivation
- A candidate-quality decision rule (same +0.5 Sharpe Δ + -5pp DD bar)
- Cross-venue conjunction

This is NOT scheduled. The default action is "do nothing" per the
parent plan's pre-commitment ("the strategy stays in its current state").

## Pre-commitment compliance check

- ✅ Threshold not loosened (used Manifesto's +0.5 / -5pp / 50-by-trade
  bars exactly as pre-registered)
- ✅ FDR ceiling honoured (0 candidates ≤ 3-candidate cap; trivially
  satisfied since 0)
- ✅ 474 cells absent (Phase 0 is full-universe by design)
- ✅ Inconclusive cells filed, not pursued ("3 no-op gates" observation
  is descriptive only)
- ✅ Verdict committed before any downstream phase dispatches

## Artifacts

- Pre-reg: `docs/preregistration/2026-05-27-phase0-filter-loo-audit.md`
- Summary CSV: `~/SHARED_DATA/phase0_loo_2026-05-27_summary.csv`
- Per-cell reports:
  - `~/SHARED_DATA/bybit_full_pit/reports/phase0_loo_2026-05-27/<cell>/`
  - `~/SHARED_DATA/binance_full_pit/reports/phase0_loo_2026-05-27/<cell>/`
- Decision-rule analyzer: `scripts/apply_decision_rule.py
  ~/SHARED_DATA/phase0_loo_2026-05-27_summary.csv --control 00_baseline`

## Run-label per `docs/backtesting_errors_we_never_repeat.md`

`exploratory` — Phase 0 is the LOO audit; no cell is `paper_ready` /
`promoted`. This includes the falsifier hits, which are useful evidence
but not actionable on their own.

## Forward pointer

**Next: Phase 1 (universe-isolation diagnostic).** Pre-reg ready at
`docs/preregistration/2026-05-27-phase1-universe-isolation-diagnostic.md`,
orchestrator at `scripts/phase1_universe_diag_sweep.py`. Dispatch
immediately after this verdict commits.
