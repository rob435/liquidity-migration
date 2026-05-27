# Phase 1 — universe-isolation diagnostic (pre-registration)

**Date:** 2026-05-27
**Stage:** pre-registered, not yet run.
**Parent plan:** [2026-05-27 multi-phase research plan](2026-05-27-rank-direction-edge-and-universe-isolation-research-plan.md)
**Phase label per plan Appendix B:** `biased_benchmark` (cells are NEVER
promoted to production, regardless of the numbers).

## Purpose

Empirically test H1 from the parent plan: *universe widening (the 474 →
764-symbol jump from the v5-listing supplement) explains the majority of
the post-fix DD shift on Bybit.*

The mechanism: the strategy filters to liquidity rank 31–400. The +290
v5-listing supplement symbols are mostly recent low-liquidity listings;
including them redefines the denominator of every pre-existing symbol's
daily liquidity rank, so `prior7_liquidity_rank` and `rank_improvement`
for older symbols are silently relabelled. The wider universe also fires
more total trades (post-fix Bybit shows 1.69× trade rate; 1.61× universe
scaling) and the marginal trades from newly-eligible names are
systematically lower-quality.

## Scope: Bybit only

The parent plan listed "~12 cells × 2 venues = 24 runs". Closer reading
of the Binance manifest shows it has **no `source` column** — i.e. no
v5-listing analog. Binance's manifest is single-source, so there is no
"archive-only" universe to compare against the "full" universe. Phase 1
is therefore **Bybit-only, 12 cells, 12 runs**. The "× 2 venues" line in
the parent plan was a generic phrasing that doesn't survive contact with
the actual Binance data layout.

Binance is **not** a useful null reference here either: the universe-
widening question is by construction venue-specific, so running the
same 6 configs on Binance over the same window would not control for the
v5-listing supplement effect. Cross-venue regime comparison is the
remit of Phase 2 / Phase 7, not Phase 1.

## Window

**2025-01-01 → 2026-05-28** (~17 months).

Matches the in-flight 2026-05-28 sweep window for direct comparison
with the EXPLORATORY baseline (which already exists in
`~/SHARED_DATA/bybit_full_pit/reports/sweep_2026-05-28/`). The Binance-
constrained `2026-04-30` end-date used for Phase 0 does NOT apply here
because Phase 1 is Bybit-only and Bybit klines extend to `2026-05-26`
(plus 2 days of `--allow-partial-pit` tolerance to reach `2026-05-28`).

## Cells (12 cells × 1 venue = 12 runs)

The pairing structure: 6 configs × 2 universes = 12 cells. The 474
universe uses the side-copy at `~/SHARED_DATA/bybit_full_pit_archive_only/`
(built by `scripts/build_legacy_archive_manifest.py`, completed
2026-05-27 with 464,475 archive rows kept / 79,838 v5-listing rows
dropped). The 764 universe uses the full root at
`~/SHARED_DATA/bybit_full_pit/`.

| Cell | Universe | Config (delta vs production baseline) |
|---|---|---|
| `P1_baseline_474`   | 474 archive-only | production defaults |
| `P1_baseline_764`   | 764 full         | production defaults (control) |
| `P1_turn10M_474`    | 474              | + `--universe-min-daily-turnover 10000000` |
| `P1_turn10M_764`    | 764              | + `--universe-min-daily-turnover 10000000` |
| `P1_rankmax200_474` | 474              | + `--universe-rank-max 200` |
| `P1_rankmax200_764` | 764              | + `--universe-rank-max 200` |
| `P1_rankimp200_474` | 474              | + `--liquidity-migration-rank-improvement-min 200` |
| `P1_rankimp200_764` | 764              | + `--liquidity-migration-rank-improvement-min 200` |
| `P1_hold2_474`      | 474              | + `--hold-days 2` |
| `P1_hold2_764`      | 764              | + `--hold-days 2` |
| `P1_combo_474`      | 474              | turn10M + hold=2 + rankimp200 |
| `P1_combo_764`      | 764              | turn10M + hold=2 + rankimp200 |

All other knobs at production defaults (Appendix A of the parent plan).
Default rank-direction is `improvement`.

## Decision rule — DESCRIPTIVE ONLY

Per the parent plan, Phase 1 is DESCRIPTIVE ONLY. It produces deltas
quantifying the universe effect; it produces **no promotion decision**.
The 474 cells are labeled `biased_benchmark` and are NEVER traded in
production, regardless of how attractive their numbers look. The
archive-only universe applies the 2026 archive coverage retroactively
to 2025-2026 data, which is by construction survivorship-biased.

### A-priori interpretation rule

If the **avg sharpe-like Δ** across the 6 paired (474 vs 764) cells is
**≥ +0.5** AND **avg DD Δ ≤ -8pp**, conclude that **universe widening
explains most of the post-fix DD shift on Bybit**. Phase 2's
interpretation of any H2 / H3 candidate is then explicitly downweighted
(we know we're seeing universe-contamination noise in the in-sample
number).

If the avg Δ is below those thresholds, conclude the universe-widening
contribution is small relative to other shifts (bug-fix, regime, code
drift); Phase 2 interpretation continues unchanged.

If the avg Δ flips sign (Sharpe Δ < 0 OR DD Δ > 0), the 474 restriction
is HURTING — implying the v5-listing supplement actually carries marginal
edge, not noise. That would be a surprising and worth-documenting result.

## Estimated cost

12 cells × ~7 min/run × parallel-8 ≈ **~12 min wall** on the 5950X.
Faster than Phase 0 because it's single-venue.

## Dispatch

```bash
SWEEP_MAX_WORKERS=8 POLARS_MAX_THREADS=4 \
  .venv/Scripts/python.exe scripts/phase1_universe_diag_sweep.py
```

Per-cell reports land under
`~/SHARED_DATA/bybit_full_pit{,_archive_only}/reports/phase1_universe_diag_2026-05-27/<cell>/`
(per-cell root depends on the 474/764 marker). Aggregate summary CSV at
`~/SHARED_DATA/phase1_universe_diag_2026-05-27_summary.csv` and is
flushed after every cell completion.

## Post-dispatch analysis

The decision-rule analyzer is NOT used for Phase 1 (the rule is
descriptive, not candidate-style). Instead the verdict write-up
manually computes:

- per-config Δ (Sharpe-like 764-cell - 474-cell, DD 764-cell - 474-cell)
- avg Δ across the 6 paired configs
- comparison vs the a-priori interpretation thresholds (±0.5 Sharpe,
  ±8pp DD)
- explicit reminder that no 474 cell is promotable

## Pre-commitments

1. **No 474 cell trades.** The archive-only universe is a measurement
   device, not a configuration that ships. Even if a 474 cell shows
   extraordinary numbers, it is `biased_benchmark` and cannot enter
   Phase 7 OOS.
2. **Cross-venue cell expansion (i.e. adding Binance cells) requires
   a plan amendment.** Phase 1's Bybit-only scope is committed; if a
   cross-venue universe diagnostic is later wanted, it gets its own
   dated pre-reg.
3. **The interpretation thresholds are committed.** The ±0.5 Sharpe /
   ±8pp DD bar is the bar; no post-hoc loosening.

## Forward pointer

- Phase 1's outcome **does not gate any other phase**. Phase 2 runs
  regardless. Phase 1 informs Phase 2's INTERPRETATION (downweighting
  in-sample numbers if universe-widening is confirmed dominant).
- If a Phase 1 cell happens to show a candidate-quality signature on
  the 764 (non-biased) side (e.g. `P1_combo_764` showing Sharpe Δ ≥
  +0.5 AND DD Δ ≤ -5pp vs `P1_baseline_764`), that finding is filed
  as a Phase 0-style "candidate" — but it must still go through Phase 7
  OOS just like any other candidate, and the FDR ceiling (max 3
  Phases 0-4 candidates to Phase 7) applies.
