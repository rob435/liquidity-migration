# Phase 0 — filter LOO audit (pre-registration)

**Date:** 2026-05-27
**Stage:** pre-registered, not yet run.
**Parent plan:** [2026-05-27 multi-phase research plan](rank-direction-edge-and-universe-isolation-research-plan.md)
**Phase label per plan Appendix B:** `exploratory` (a removed filter would
get its own dated pre-reg before shipping to production).

## Purpose

Empirically test H5 from the parent plan: *some filters in the current
event_demo stack hurt more than help*. Leave-one-out every filter knob in
the production config and compare per-cell against the baseline. A filter
whose removal **improves** Sharpe AND DD on **both** venues by the
Strictness-Manifesto thresholds is a candidate for permanent removal from
the production config; its removal would then get its own dated pre-reg
before shipping.

## Decision rule

The Strictness Manifesto, verbatim from the parent plan (no loosening):

- per-cell candidate (= "remove this filter from production"): sharpe-like
  Δ ≥ **+0.5** vs control on **both** venues AND max-DD Δ ≤ **-5pp** on
  **both** AND sign-consistent across 3 sub-period thirds AND ≥50
  trades/sub-period on Bybit (≥30 on Binance).
- per-cell falsifier: sharpe-like Δ < -0.5 on either venue, OR sign flip
  between venues / sub-periods, OR any sub-period DD > 60%. Falsified
  cells confirm "the filter pulls its weight".
- inconclusive cells (not candidate, not falsifier) are FILED and not
  pursued further. No "well it's close" reasoning.

Decision-rule analysis runs via `python scripts/apply_decision_rule.py
<SUMMARY_CSV> --control 00_baseline` and is reproduced in the verdict
write-up.

## Window

**2023-04-01 → 2026-04-30** (~3 years, 1 month).

The parent plan asked for 2023-04-01 → 2026-05-28. The end-date is
clamped to **2026-04-30** because Binance USD-M klines coverage in
`~/SHARED_DATA/binance_full_pit/klines_1h/` currently ends 2026-04-30
(Bybit goes to 2026-05-26). Using the cross-venue minimum keeps both
venues observing the same calendar window, preserving the comparison's
validity. The 28-day shortfall vs the plan is a fully-observable
operational constraint (not data-dependent cherry-picking), so it does
NOT trigger the "no off-menu cells" pre-commit; the cell menu and
decision rule are unchanged.

The plan's optional extension to 2023-01-01 is NOT taken: the 85
2023-24 archive partitions the plan mentioned have been repaired (Bybit
data covers 2021-01-01 → 2026-05-26 without gaps), so the available
historical window is actually longer than the plan assumed. We keep
the 2023-04-01 start for direct comparison with the parent plan's
expected output structure.

## Cells (14 LOO + 1 baseline = 15 cells × 2 venues = 30 runs)

The baseline `00_baseline` is the current promoted profile (matches
Appendix A of the parent plan).

| Cell | Filter disabled | Override |
|---|---|---|
| `00_baseline` | (none — control) | — |
| `P0_noflt_turnover_ratio` | turnover ratio gate | `--liquidity-migration-turnover-ratio-min 0` |
| `P0_noflt_event_rank_frac` | event rank-fraction ceiling | `--liquidity-migration-event-rank-fraction-max 1.0` |
| `P0_noflt_day_return` | day-return floor | `--liquidity-migration-day-return-min -1.0` |
| `P0_noflt_residual_return` | residual-return floor | `--liquidity-migration-residual-return-min 0` |
| `P0_noflt_close_location` | close-location floor | `--liquidity-migration-close-location-min 0` |
| `P0_noflt_pit_age` | PIT-age floor | `--liquidity-migration-pit-age-days-min 0` |
| `P0_noflt_crowding` | crowding-detection family | `--liquidity-migration-crowding-filter none` |
| `P0_noflt_stop_pressure` | stop-pressure veto | `--stop-pressure-stop-count 999` |
| `P0_noflt_realized_loss` | realized-loss-pressure veto | `--realized-loss-pressure-loss-count 999` |
| `P0_noflt_rank_min` | universe rank lower bound | `--universe-rank-min 1` |
| `P0_noflt_rank_max` | universe rank upper bound | `--universe-rank-max 99999` |
| `P0_noflt_cooldown` | per-symbol cooldown | `--cooldown-days 0` |
| `P0_noflt_max_active` | concurrent-position cap | `--max-active-symbols 999` |
| `P0_noflt_entry_delay` | 1h entry delay | `--entry-delay-hours 0` |

All other knobs at production defaults (Appendix A of the parent plan).
The default `--liquidity-migration-rank-direction improvement` is in
effect; Phase 0 does not vary direction. The default rank-direction
flag is asserted bit-for-bit identical to pre-Change-1 behaviour by the
test suite added with commit `ef9d3aa`.

## Estimated cost

15 cells × 2 venues = **30 runs** × ~7 min/run × parallel-8 ≈ **~30
min wall** on the 5950X (slight bump vs the parent plan's 25 min
estimate, since the 15th-cell baseline adds one to the dispatch list).

## Dispatch

```bash
SWEEP_MAX_WORKERS=8 POLARS_MAX_THREADS=4 \
  .venv/Scripts/python.exe scripts/phase0_loo_sweep.py
```

Reports land in
`~/SHARED_DATA/{bybit,binance}_full_pit/reports/phase0_loo_2026-05-27/<cell>/`.
The aggregate summary CSV writes to
`~/SHARED_DATA/phase0_loo_2026-05-27_summary.csv` and is flushed after
every cell so the file always reflects all completed cells.

## Post-dispatch decision-rule application

```bash
python scripts/apply_decision_rule.py \
  ~/SHARED_DATA/phase0_loo_2026-05-27_summary.csv \
  --control 00_baseline
```

This produces the per-cell verdict table the verdict commit reproduces.

## Pre-commitments

1. If ANY cell shows the candidate signature (Sharpe Δ ≥ +0.5 BOTH
   venues + DD Δ ≤ -5pp BOTH + sign-consistent + ≥50 trades), it
   becomes a candidate for permanent removal from production. The
   removal itself requires its OWN dated pre-reg before shipping; it
   does not auto-promote.
2. If MORE than 3 cells meet candidate criteria, the FDR ceiling
   (max 3 from Phases 0-4 combined to Phase 7) applies and the
   `apply_decision_rule.py` tie-break (combined-venue Sharpe) selects
   which 3.
3. If NO cell meets candidate criteria, the conclusion is "every
   filter currently in production pulls its weight per the Manifesto
   threshold". The verdict is filed, no production change.
4. Inconclusive cells (close-but-not-passing) are FILED and not
   pursued. The Manifesto threshold is non-negotiable; near-misses
   become evidence that the filter is operating at its margin, not
   evidence that the threshold should bend.

## Threats to inference (sanity check vs `docs/backtesting_errors_we_never_repeat.md`)

| # | Threat | Phase-0 mitigation |
|---|---|---|
| #1 | Future universe selection | Full PIT universe via `bybit_full_pit` / `binance_full_pit` — no archive-only restriction here. |
| #4 | Revised / non-PIT data | All datasets are end-exclusive on today's UTC date; rebuild scripts are idempotent. |
| #17 | Parameter mining | Threshold is the Manifesto's, pre-committed. FDR ceiling enforced via `apply_decision_rule.py`. |
| #19 | Multiple testing | 14 cells per venue × 2 venues = 28 paired tests. Manifesto's strict 0.5 Sharpe / 5pp DD on BOTH venues + sign-consistency is the FDR-aware shape that makes a hit meaningful. |
| #23 | Pretty-report bias | All cells produce the standard volume_events ledger + equity curve + monthly P&L + config-hash. |

## Forward pointer

- If 0 candidates: verdict = "current filter stack stays as-is", **next
  phase = Phase 1** (universe-isolation diagnostic).
- If 1-3 candidates: each gets its own removal pre-reg, then Phase 7
  OOS gate at session end.
- Phase 2 (direction grid) runs in parallel with Phase 1 as soon as
  this phase completes — independent of Phase 0 outcome.
