# R1 — Per-filter hypothesis audit (pre-registration)

**Date:** 2026-05-28
**Stage:** proposed (pending operator confirmation; not yet run).
**Parent plan:** [2026-05-29 Round 2 integrated-strategy program](integrated-strategy-program.md), section "Sub-phase R1".
**Phase label per integrity standard:** `exploratory` — no R1 cell is eligible for production filter change without a separate dated removal pre-reg.

## Purpose

Apply the Round 2 Investigation tier (softer than the Round 1 Manifesto, but pre-committed) to the two filters Round 1's Phase 0 flagged ambiguous (`rank_max`, `realized_loss`) and to the two filters Phase 0 flagged near-no-op (`day_return`, `stop_pressure`).

The 10 KEPT filters from the plan's R1 evidence table are **not re-tested** at R1 — Phase 0 already produced decisive falsifier or beneficial-removal evidence on them under the strict Manifesto, which dominates the looser Investigation tier. Re-testing them here would burn compute without changing the decision.

The plan's R1 table also commits us to a single "joint drop" cell to catch any interaction the LOO grid missed for `day_return` + `stop_pressure`.

## Cells (6 cells × 2 venues = 12 runs)

| Cell | Description | Override delta vs `R1_baseline_v2` |
|---|---|---|
| `R1_baseline_v2` | Current promoted profile = production filter stack as shipped. Control. | (none) |
| `R1_drop_day_return` | Production minus `day_return` floor | `--liquidity-migration-day-return-min -1.0` |
| `R1_drop_stop_pressure` | Production minus `stop_pressure` veto | `--stop-pressure-stop-count 999` |
| `R1_drop_both_noops` | Joint drop of the two near-no-ops | `--liquidity-migration-day-return-min -1.0` + `--stop-pressure-stop-count 999` |
| `R1_retest_rank_max` | Production minus universe-rank upper bound | `--universe-rank-max 99999` |
| `R1_retest_realized_loss` | Production minus realized-loss-pressure veto | `--realized-loss-pressure-loss-count 999` |

Baseline = the 28-flag production profile encoded in `scripts/volume_events_cell.sh` and `scripts/_sweep_runtime.py` (matches `configs/volume_alpha.default.yaml`). Each cell modifies exactly the flag(s) named in the third column; everything else stays at baseline.

## Window

**2023-04-01 → 2026-04-30** (~3 years, 1 month).

Reasoning:
- Bybit klines: 2021-01-01 → 2026-05-26 available.
- Binance klines: 2020-01-01 → 2026-04-30 available.
- Cross-venue minimum dictates 2026-04-30 end.
- Start date matches Round 1 Phase 0 for direct comparability — the Investigation-tier verdict for the two RE-TEST cells must be consistent with Phase 0's stricter LOO finding under the same window, modulo the looser threshold.
- The pre-2023 data (2020-2022) is reserved for R11 OOS and is **not** touched by R1.

Sub-period thirds (for sign-consistency check): 2023-04-01 → 2024-04-30, 2024-05-01 → 2025-04-30, 2025-05-01 → 2026-04-30. Each ~1 year.

## Decision rule — Round 2 Investigation tier

A cell is **investigation-positive** if **ALL**:
- **MAR Δ > 0** on majority of venues (2/2 OR 1/2 with the other not worse than -0.5 MAR)
- No return sign-flip vs control (both venues remain same-signed)
- ≥30 trades on Bybit, ≥20 trades on Binance

A cell **falsifies** if **ANY**:
- MAR Δ ≤ -1.0 on either venue
- Return goes negative on a venue that was positive in control
- DD > 70% on either venue
- Trade count < 10 / sub-period on either venue

Cells failing investigation-positive but not falsifying are **descriptive** — recorded for context, not acted on.

### MAR computation

MAR = annualized_return / |max_drawdown|.

Annualization formula (geometric):
```
annualized_return = (1 + total_return) ** (365.25 / window_days) - 1
```
where `window_days = (end_date − start_date)` in calendar days. For this R1 sweep `window_days = 1126` (2023-04-01 → 2026-04-30 inclusive of start, exclusive of end).

The `apply_decision_rule.py` analyzer currently scores on Sharpe Δ (the Round 1 Manifesto). Round 2 requires an MAR-based Investigation rule that the analyzer does not yet implement. Add as part of R1:

- A new `--rule investigation` mode to `apply_decision_rule.py` that:
  - Computes per-cell MAR from `total_return`, `max_drawdown`, and a `--window-days` CLI flag (or a new optional `window_days` column in the summary CSV when emitted by the sweep runtime).
  - Applies the Investigation-tier verdict above instead of the Manifesto-strict one.
  - Outputs the same table shape with `mar_d` columns added next to `sharpe_d`.
- Optional: emit `window_days` into the summary CSV so the analyzer doesn't need a CLI flag. Decided downstream — see "Code changes" below.

The Manifesto-strict rule (`--rule manifesto`) is **kept as-is** and used at R10. R1 does NOT touch the existing rule.

## Hypotheses

Per cell, the hypothesis under test:

| Cell | Hypothesis | What would falsify it |
|---|---|---|
| `R1_drop_day_return` | The day-return floor is a no-op because the `residual_return ≥ 0.08` floor already excludes negative-day signals. Removing it should not change MAR. | MAR Δ ≤ -0.5 on either venue (the floor was actually doing work). |
| `R1_drop_stop_pressure` | The stop-pressure veto rarely binds (Phase 0 LOO Δ ≈ ±0.05). Removing it should not change MAR. | MAR Δ ≤ -0.5 on either venue. |
| `R1_drop_both_noops` | Joint drop has no interaction effect: ΔMAR ≈ sum of individual Δs (≈ 0). | Either: (a) joint Δ much worse than singleton Δs (negative interaction), or (b) joint Δ noticeably positive on both venues (the two filters were jointly redundant with overlapping logic — Investigation-positive). |
| `R1_retest_rank_max` | Phase 0 LOO showed mild Sharpe gain + DD shrinkage on removal. Under the Investigation tier the filter is a candidate for drop. | MAR Δ ≤ 0 on either venue (Phase 0 finding doesn't replicate under the looser bar). |
| `R1_retest_realized_loss` | Same pattern: Phase 0 LOO showed Bybit benefit on removal, Binance no-op. | MAR Δ ≤ 0 on Bybit OR Binance loses materially. |

If a cell investigation-positives, **no production change happens at R1.** The cell joins the candidate set forwarded to R10 promotion-bar testing. R10 applies the strict Promotion bar; only cells passing both Investigation and Promotion forward to R11 OOS. R1's job is to keep options open, not to make filter-stack changes.

## Roots that will be touched

- [x] `~/SHARED_DATA/bybit_full_pit` (per-venue working dataset, full PIT)
- [x] `~/SHARED_DATA/binance_full_pit` (per-venue working dataset, full PIT)
- [ ] forward demo/paper (no — R1 is pure backtest)
- [ ] pre-2023 OOS window (no — reserved for R11)

## Code changes required for R1

1. **New orchestrator script:** `scripts/r1_filter_audit_sweep.py` — adapts the `phase0_loo_sweep.py` pattern with the 6 R1 cells and the new sweep tag `r1_filter_audit_2026-05-28`. Window constants pinned per this pre-reg. Window-days emitted into the summary CSV (see #3).
2. **Analyzer extension:** `scripts/apply_decision_rule.py` gains `--rule investigation` mode + `mar_d` column in the output table. Existing `--rule manifesto` left untouched. Tests added: synthetic CSV with known MAR ratios → expected verdicts (both `manifesto` and `investigation` modes covered).
3. **Summary-CSV schema:** `_sweep_runtime.py:run_cell` adds `window_days`, `start_date`, `end_date` to the per-row metrics dict. The analyzer reads `window_days` if present and falls back to `--window-days` CLI for compatibility.

Estimated effort: **~2-3 hours of code + tests + ruff/pytest run.** Cleanly additive — no existing behaviour changes.

Compute budget for the sweep itself: **12 runs × ~10 min/run ÷ 4-way parallel ≈ 30 min wall.** (Plan's estimate was 30 min at 4-way; with 8-way parallel and 4 polars threads = 32 threads = full SMT, closer to 15-20 min. Either way ≪ 1 hour.)

## Dispatch

```powershell
# Pre-flight: ruff clean, pytest green
.venv\Scripts\python.exe -m ruff check liquidity_migration tests
.venv\Scripts\python.exe -m pytest -q

# Sweep (8-way parallel, 4 polars threads/cell)
$env:SWEEP_MAX_WORKERS = "8"
$env:POLARS_MAX_THREADS = "4"
.venv\Scripts\python.exe scripts\r1_filter_audit_sweep.py

# Decision-rule analysis
.venv\Scripts\python.exe scripts\apply_decision_rule.py `
  $env:USERPROFILE\SHARED_DATA\r1_filter_audit_2026-05-28_summary.csv `
  --control R1_baseline_v2 `
  --rule investigation
```

Reports land in `~/SHARED_DATA/{bybit,binance}_full_pit/reports/r1_filter_audit_2026-05-28/<cell>/`. Aggregate CSV at `~/SHARED_DATA/r1_filter_audit_2026-05-28_summary.csv`, flushed after every completion under a lock.

## Pre-commitments

1. **Investigation tier thresholds are pre-committed.** MAR Δ > 0 majority-venues + sign-consistent + trade-count floors. No loosening after seeing results.
2. **No production filter change from R1.** Any DROP / RE-TEST that investigation-positives joins the R10 promotion-bar test queue. R10 + R11 are the only gates that produce a production-promotion-eligible decision.
3. **Falsifier hits are first-class evidence.** If a DROP candidate falsifies (the filter was actually doing work), that closes the question on that filter for Round 2.
4. **No off-menu cells.** If an interaction observed at R1 suggests a 7th cell would clarify it, that's an amendment to this pre-reg, committed before the additional cell runs.
5. **Sub-period sign-consistency is binding.** A cell whose MAR is positive on the full window but mixed across thirds is descriptive, not investigation-positive.

## Threats to inference (vs `docs/backtesting_errors_we_never_repeat.md`)

| # | Threat | R1 mitigation |
|---|---|---|
| #1 | Future universe selection | Full PIT roots; no archive-only sub-setting. The biased_benchmark 474 root is NOT touched in R1. |
| #2 | Future info in signals | All features computed at end-of-day close; +1h entry delay preserved (it's a KEPT filter, not under test). |
| #4 | Revised / non-PIT data | Datasets are end-exclusive on 2026-04-30; rebuild scripts are idempotent. |
| #15 | Warm-started state | Cell start is 2023-04-01 with cold start (no warm-up); matches Round 1 Phase 0 convention. R7 stress tests handle warm-start semantics separately. |
| #17 | Parameter mining | Investigation tier is the published threshold from the Round 2 plan, pre-committed before any cell runs. The threshold cannot be loosened to admit a near-miss. |
| #18 | OOS reuse | Pre-2023 window untouched by R1. Reserved for R11. |
| #19 | Multiple testing | 6 cells × 2 venues = 12 paired tests. Investigation bar is intentionally looser (we're gathering evidence, not promoting) but FDR ceiling 5 still binds at the R10 gate downstream. |
| #20 | Bad accounting | Cost model = legacy `cost_multipliers=3`. R6 will refit; R10 will re-cost any forwarded cells. R1 verdicts are cost-as-Round-1; if R6 changes them materially, R10 re-evaluates. |
| #23 | Pretty-report bias | Every cell produces the standard volume_events ledger + JSON report + equity curve + monthly P&L + config hash. |
| #25 | All-or-nothing compute | Sweep orchestrator inherits `_sweep_runtime.py` checkpointed-per-cell summary-flush pattern from Phase 0. |

## Forward pointer

- **If 0 cells investigation-positive:** R1 verdict = "the 4 ambiguous/no-op filters from Round 1 Phase 0 do not become candidates under the Investigation tier either." Filter stack stays as-is. R2 proceeds.
- **If 1-2 cells investigation-positive:** Each joins the R10 candidate queue. R2 still proceeds in parallel.
- **If ≥3 cells investigation-positive on a single filter family** (e.g. `R1_drop_day_return` AND `R1_drop_both_noops` both pass on both venues): note the joint signal but treat as ONE candidate at R10 (the joint cell). Avoid double-counting the same filter for the FDR ceiling.
- **R2 (per-feature standalone)** runs in parallel with R1 — no dependency.

## Open questions before dispatch

1. **`window_days` schema decision.** Adding to the summary CSV (option A, cleaner) vs `--window-days` analyzer CLI flag (option B, no schema change). Pinning A here unless operator prefers B.
2. **Investigation-tier majority-venue ambiguity.** The plan reads "2/2 OR 1/2 with the other not worse than -0.5 MAR". I interpret "majority" as ≥1 venue positive; the second venue can be ≤0 as long as ≥ -0.5 MAR. Pinned in this pre-reg's rule statement. If operator reads this differently, amendment before dispatch.
3. **MAR sign convention.** MAR is defined as `annualized_return / |max_drawdown|`. Both numerator and denominator are positive when the strategy makes money; MAR ≥ 0 always for a profitable cell. A negative-return cell has MAR ≤ 0 (negative numerator, positive denominator). Pinned for clarity in case future cells throw negative-return outliers.

## Issue spotted during code: plan's worked-example window typo

The Round 2 master plan's "Worked example for the Round 1 baseline" table says:

| Venue | Total return | Period | Annualized | Max DD | MAR |
|---|--:|---|--:|--:|--:|
| Bybit | +518.76% | **17m** | +231.5%/yr | -42.1% | **+5.50** |
| Binance | +66.12% | **17m** | +45.4%/yr | -40.7% | **+1.11** |

The Round 1 baseline window was actually **2023-04-01 → 2026-04-30 = 1125 days ≈ 37 months / 3.08 years** (not 17 months). With the standard geometric annualization formula `(1 + total_return)^(365.25/window_days) - 1`, the real numbers are:

| Venue | Total return | Period | Annualized (geometric) | Max DD | MAR |
|---|--:|---|--:|--:|--:|
| Bybit | +518.76% | 1125 d (37m) | **+80.7%/yr** | -42.1% | **+1.92** |
| Binance | +66.12% | 1125 d (37m) | **+17.9%/yr** | -40.7% | **+0.44** |

The plan's worked example is internally consistent only if the window is ~1.5 years (~555 days), not 37 months. The "17m" cell appears to be a draft-era typo carried over from an earlier baseline. Verified by running the formula explicitly.

**Why this matters:**
- The Investigation tier ("MAR Δ > 0 majority venues") and Promotion bar ("MAR Δ ≥ +0.5 both venues") are **deltas** against the control. The thresholds themselves are pre-committed and stay as written.
- But the baseline MAR values in operator intuition matter for calibrating "what level of improvement should we expect to see." If the operator reads "Bybit baseline is +5.50 MAR" from the plan, a Promotion bar of "Δ ≥ +0.5" feels like a ~10% improvement (achievable). With the real baseline of +1.92, the same Δ is a ~25% improvement (harder, but still achievable).
- The thresholds are NOT being moved. Just flagging that the plan's worked example is misleading and the real baseline numbers are ~3× lower.

**Proposed remediation (no impact on R1 dispatch):**
- Update the Round 2 plan's worked example to reflect the real window (1125 days / 37 months / 3.08 years) and real annualized + MAR numbers. This is a doc-only edit, no decision-rule change. Done as a separate operator-confirmed commit.
- Leave R1 dispatch behavior unchanged — the Investigation tier uses MAR Δ thresholds calibrated against whatever control the sweep produces, so the typo doesn't affect the decision math.

The `compute_mar` and `compute_annualized_return` helpers in `apply_decision_rule.py` use the standard geometric formula. Tests `test_compute_annualized_return_round1_baseline_actual_window` and `test_compute_mar_round1_baseline_actual_window` pin the corrected numbers explicitly so this finding is preserved in code.
