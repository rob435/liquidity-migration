# R1 — Per-filter audit (VERDICT)

**Date:** 2026-05-28 (sweep ran 2026-05-28, verdict generated 2026-05-28)
**Stage:** run-complete · **5 investigation-positive · 0 falsifiers · 0 descriptive · 1 skip-control**
**Pre-reg:** [docs/preregistration/round2/r1-per-filter-audit.md](r1-per-filter-audit.md)
**Parent plan:** [Round 2 integrated-strategy program](integrated-strategy-program.md)
**Run-label per integrity standard:** `exploratory` (Investigation tier; no production change at R1 — by pre-commitment, investigation-positive cells join the R10 candidate queue but do NOT triggered filter-stack edits).

## Headline

All 5 R1 cells cleared the Investigation tier. None falsified. The 5 cells forward to the R10 promotion-bar queue exactly at the FDR ceiling.

**The production filter stack stays as-is.** Investigation-positive at R1 means "this cell merits R10 testing under the strict Promotion bar"; it does NOT authorize any filter-stack change. Per the plan's pre-commitment, only an R11-passing finalist would justify production change.

Notable observations:
- The single largest improvement is `R1_retest_rank_max` (drop `--universe-rank-max 400`): combined ΔMAR = +1.69, Bybit ΔMAR = +1.44 (Bybit return rises from +38.56× to +49.28× with DD shrinking 4.9 pp). This replicates Phase 0's mild positive LOO Δ at the looser Investigation threshold.
- `R1_retest_realized_loss` (drop realized-loss-pressure veto) shows Bybit ΔMAR = +0.61, Binance ΔMAR = ≈0. The pattern is "Bybit benefit, Binance no-op" — consistent with Phase 0.
- `R1_drop_both_noops` (joint drop of `day_return` + `stop_pressure`) is investigation-positive **only** through the 1/2-venue-tolerance branch: Bybit ΔMAR = -0.12 (within -0.5 tolerance), Binance ΔMAR = +0.35. No falsifying interaction surfaced; the two filters appear independently no-op in their joint removal too.
- `R1_drop_day_return` alone: combined ΔMAR = +0.13. Truly no-op.
- `R1_drop_stop_pressure` alone: combined ΔMAR = +0.09, again through the 1/2-tolerance branch (Bybit -0.20, Binance +0.29). Truly no-op.

The R1 verdict is consistent with Phase 0's Manifesto verdict (which rejected all of these). The Investigation tier surfaces them as worth R10 retest under the strict bar; under the strict Promotion bar (ΔMAR ≥ +0.5 BOTH venues) **none of the 5 cells would pass** — only `R1_retest_rank_max` clears on Bybit (+1.44) and not Binance (+0.25). So if R10 runs with the Promotion-bar threshold unchanged, the R1 candidate queue likely empties at R10 — exactly the disciplined outcome the two-tier structure is designed for.

## Decision-rule analyzer output (verbatim)

```
# rule: investigation  control: R1_baseline_v2  mar_delta_min: +0.0  mar_delta_tolerance: -0.5  mar_falsify: -1.0  dd_falsify: -70%  min_trades: bybit=30 binance=20  window_days_fallback: 1125.0
cell_id                  by_mar_d            bn_mar_d            by_dd_d             bn_dd_d             by_tr               bn_tr               by_ret              bn_ret              verdict
R1_baseline_v2           +0.00               +0.00               +0.0pp              +0.0pp              602                 421                 +38.56x             +4.21x              skip_control
R1_drop_both_noops       -0.12               +0.35               +0.9pp              -4.1pp              609                 428                 +38.47x             +4.83x              investigation_positive
R1_drop_day_return       +0.08               +0.05               +0.0pp              +0.0pp              603                 422                 +39.88x             +4.39x              investigation_positive
R1_drop_stop_pressure    -0.20               +0.29               +0.9pp              -4.1pp              608                 427                 +37.20x             +4.63x              investigation_positive
R1_retest_rank_max       +1.44               +0.25               -4.9pp              -1.6pp              586                 407                 +49.28x             +4.95x              investigation_positive
R1_retest_realized_loss  +0.61               +0.00               -1.2pp              +0.0pp              607                 421                 +45.60x             +4.21x              investigation_positive

# summary: investigation_positive=5 rejects=0 descriptive=0 skip_control=1
# INVESTIGATION-POSITIVE cells (ranked by combined-venue MAR Δ; forward to R10 candidate queue):
#   R1_retest_rank_max  combined ΔMAR=+1.69
#   R1_retest_realized_loss  combined ΔMAR=+0.61
#   R1_drop_both_noops  combined ΔMAR=+0.23
#   R1_drop_day_return  combined ΔMAR=+0.13
#   R1_drop_stop_pressure  combined ΔMAR=+0.09
```

Baseline (`R1_baseline_v2`) metrics reproduce Phase 0's baseline bit-identically (trades=602 / 421, return=+38.56× / +4.21×, DD=-42.11% / -42.20%, sharpe=2.4536 / 1.4615) — confirms the R1 orchestrator + BASELINE_PARAMS table match Phase 0's. The R1 baseline at 1125 days gives MAR = +5.46 (Bybit), +1.68 (Binance) per the geometric annualization formula.

## Per-cell discussion

### `R1_retest_rank_max` — drop `--universe-rank-max 400`

**Investigation-positive** with the largest combined ΔMAR in the menu.

| Metric | R1_baseline_v2 | R1_retest_rank_max | Δ |
|---|--:|--:|--:|
| Bybit total return | +38.56× | +49.28× | +10.72× |
| Bybit annualized | +230.0%/yr | +290.4%/yr | +60.4 pp |
| Bybit |max DD| | 42.11% | 37.22% | -4.89 pp |
| Bybit MAR | +5.46 | +6.90 | **+1.44** |
| Bybit trades | 602 | 586 | -16 |
| Binance total return | +4.21× | +4.95× | +0.74× |
| Binance annualized | +70.9%/yr | +78.3%/yr | +7.4 pp |
| Binance |max DD| | 42.20% | 40.60% | -1.60 pp |
| Binance MAR | +1.68 | +1.93 | **+0.25** |
| Binance trades | 421 | 407 | -14 |

Mechanism: the `--universe-rank-max 400` filter excludes names that rank ≥401 by 30-day ADV from the eligible universe. Removing the cap admits the tail of liquidity that Round 1's Phase 0 LOO already flagged as mild-positive. At the Investigation tier this passes; at the Promotion tier, the Binance ΔMAR of +0.25 falls below the +0.5 bar — so this cell forwards to R10 but is unlikely to clear it without additional infrastructure (R6 cost model recosting may further help or hurt the small-cap tail).

### `R1_retest_realized_loss` — drop realized-loss-pressure veto

**Investigation-positive** through the 2/2-positive branch (both venues ≥0 MAR Δ, with Binance numerically flat).

| Metric | R1_baseline_v2 | R1_retest_realized_loss | Δ |
|---|--:|--:|--:|
| Bybit total return | +38.56× | +45.60× | +7.04× |
| Bybit MAR | +5.46 | +6.07 | **+0.61** |
| Bybit |max DD| | 42.11% | 40.87% | -1.24 pp |
| Binance total return | +4.21× | +4.21× | +0.00× |
| Binance MAR | +1.68 | +1.68 | **+0.00** |
| Binance |max DD| | 42.20% | 42.20% | +0.00 pp |

Mechanism: the realized-loss-pressure veto blocks new entries when ≥6 recent losses fired in the trailing 5 days. Removing it on Bybit lets a few additional entries through, modestly raising return without widening DD materially. On Binance the filter never fires at the current production threshold — its removal is a true no-op (numerically identical metrics). Phase 0's LOO showed this exact pattern.

### `R1_drop_both_noops` — joint drop of `day_return` + `stop_pressure`

**Investigation-positive** through the 1/2-tolerance branch.

| Metric | R1_baseline_v2 | R1_drop_both_noops | Δ |
|---|--:|--:|--:|
| Bybit total return | +38.56× | +38.47× | -0.08× |
| Bybit MAR | +5.46 | +5.34 | **-0.12** |
| Bybit |max DD| | 42.11% | 43.02% | +0.91 pp |
| Binance total return | +4.21× | +4.83× | +0.62× |
| Binance MAR | +1.68 | +2.03 | **+0.35** |
| Binance |max DD| | 42.20% | 38.14% | -4.06 pp |

Interaction check: the singleton-drop ΔMARs are Bybit +0.08 / Binance +0.05 (day_return) and Bybit -0.20 / Binance +0.29 (stop_pressure). The joint Bybit ΔMAR of -0.12 is approximately the sum of the singletons (+0.08 + -0.20 = -0.12) — no surprise interaction. The joint Binance ΔMAR of +0.35 is also approximately the sum (+0.05 + +0.29 = +0.34). The two filters operate independently with no positive joint effect; their joint removal is approximately the linear combination of the singleton drops.

This means R10 likely sees `R1_drop_both_noops` as ≈ "the better of R1_drop_day_return alone or R1_drop_stop_pressure alone"; testing it separately at R10 may be redundant with the singleton cells. (NOT a basis to skip — R10 still gets it per the plan.)

### `R1_drop_day_return` — drop day-return floor

**Investigation-positive** through the 2/2-positive branch with tiny effects (combined ΔMAR = +0.13).

| Metric | R1_baseline_v2 | R1_drop_day_return | Δ |
|---|--:|--:|--:|
| Bybit MAR | +5.46 | +5.54 | **+0.08** |
| Binance MAR | +1.68 | +1.73 | **+0.05** |

Both venues' DD stay exactly at baseline. Trades increase by 1 on each venue. The day-return floor is essentially a no-op gate that doesn't bind in the baseline-passing population — confirmed.

### `R1_drop_stop_pressure` — drop stop-pressure veto

**Investigation-positive** through the 1/2-tolerance branch.

| Metric | R1_baseline_v2 | R1_drop_stop_pressure | Δ |
|---|--:|--:|--:|
| Bybit MAR | +5.46 | +5.26 | **-0.20** |
| Binance MAR | +1.68 | +1.97 | **+0.29** |
| Bybit |max DD| | 42.11% | 43.02% | +0.91 pp |
| Binance |max DD| | 42.20% | 38.14% | -4.06 pp |

Bybit slightly worse (DD +0.9 pp), Binance better (DD -4.1 pp). This is the venue-asymmetry pattern Phase 0 also surfaced. The stop_pressure veto appears to be Bybit-protective and Binance-overcautious; the net effect is venue-dependent.

## Implications for downstream phases

- **R10 (Promotion gate):** all 5 R1 cells join the candidate queue. Per the FDR ceiling (5 max), no cells are pruned at R1. R10 applies the strict Promotion bar (MAR Δ ≥ +0.5 BOTH venues, sub-period sign-consistent, residual Sharpe ≥ +0.3, etc.). Looking at the R1 numbers, **at most** `R1_retest_rank_max` survives Promotion (Bybit +1.44 strong, Binance +0.25 below bar), and even that needs residual Sharpe analysis from R4 to clear. Practical expectation: R10 will likely reject all 5 with the Promotion bar intact. That's the discipline of the two-tier structure.
- **R2 (per-feature standalone):** not gated by R1; runs next. R1's findings about which filters are load-bearing don't change the per-feature IC test's design.
- **R3 (bearish stack honest test):** not gated by R1.
- **R4–R9:** not gated by R1.

## Open follow-ups (NOT acted on at R1)

1. **Per-venue threshold split.** `R1_retest_rank_max` reveals strong Bybit benefit (+10.72×) and modest Binance benefit (+0.74×) from removing the rank cap. The asymmetry mirrors Phase 2's finding that Bybit and Binance have different optimal rank-improvement thresholds. A per-venue universe-rank-max threshold (e.g. unbounded on Bybit, 400 on Binance) is **not in scope** for R1; per the plan, per-venue threshold variants are a possible R10 amendment, not an R1 follow-up.

2. **R6 cost-model re-evaluation.** All R1 cells use the legacy `cost_multiplier=3` flat cost. R6 will re-cost with the per-name per-bar model when it lands; this could meaningfully change the relative ordering of the 5 cells, especially `R1_retest_rank_max` (small-cap names have higher modeled costs). R10 must re-evaluate any forwarded cell under the model cost.

3. **Joint-cell redundancy at R10.** As discussed in the per-cell section, `R1_drop_both_noops` is approximately the linear combination of the two singleton-drops. R10 should NOT double-count toward the FDR ceiling — if it survives R10 it occupies one candidate slot, not three.

## Pre-commitment compliance check

- ✅ Investigation tier threshold not loosened (used pre-committed +0.0 MAR Δ majority, -0.5 tolerance, -1.0 falsifier, 70% DD ceiling, 30/20 trade floor).
- ✅ Pareto + sign-consistency at sub-period level deferred to R10 (Investigation tier is full-window only by pre-commitment — R10 runs the sub-period split).
- ✅ FDR ceiling (5) trivially satisfied — 5 investigation_positives = the ceiling, no pruning needed.
- ✅ No production filter change made. Production stack stays exactly as-is.
- ✅ The plan's worked-example MAR (+5.50 Bybit) matches the actual R1 baseline MAR (+5.46) within 1%; the plan's MAR threshold semantics are validated against real numbers.
- ✅ Window 2023-04-01 → 2026-04-30 = 1125 days = ~37 months; window_days emitted into summary CSV as documented.

## Artifacts

- Pre-reg: `docs/preregistration/round2/r1-per-filter-audit.md`
- Summary CSV: `~/SHARED_DATA/r1_filter_audit_2026-05-28_summary.csv`
- Per-cell reports:
  - `~/SHARED_DATA/bybit_full_pit/reports/r1_filter_audit_2026-05-28/<cell>/`
  - `~/SHARED_DATA/binance_full_pit/reports/r1_filter_audit_2026-05-28/<cell>/`
- Decision-rule analyzer command:
  ```
  .venv/Scripts/python.exe scripts/apply_decision_rule.py \
    ~/SHARED_DATA/r1_filter_audit_2026-05-28_summary.csv \
    --control R1_baseline_v2 --rule investigation
  ```
- Compute used: 54.6 min wall on 5950X, 8-way parallel sweep, 4 polars threads/cell. Longer than the pre-reg's 30-min estimate; the bigger Bybit cells (~50 min each sequential) dominate, and the 8-way pool quickly saturates with the 4 longest Bybit cells running concurrently.

## Forward pointer

**Next: R2 (per-feature standalone decile-sort + correlation matrix).** Not gated by R1; pre-reg + orchestrator drafted next. R2 runs on the signal_harness path (polars-native), so compute is fast (~30 min wall).
