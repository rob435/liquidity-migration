# Research plan: rank-direction edge, universe isolation, filter audit, and signal-research harness

**Date:** 2026-05-27 (v2 — all-out compute version)
**Author:** owner (drafted with assistant)
**Stage:** proposed — multi-phase research program. Each phase below becomes
its own dated pre-registration file before its sweep is run.
**Compute target:** Ryzen 5950X (16 cores / 32 threads), no compute throttling.
**Integrity standard:** `docs/backtesting_errors_we_never_repeat.md` is binding.

---

## TL;DR

We are doing four things in parallel, with strict pre-registered decision
rules sized for the compute-scaled multiple-testing exposure.

1. **Filter LOO audit** (Phase 0) — leave-one-out every filter in the
   current event_demo stack on both venues and full window. Gut filters
   that don't pull their weight. Pure pruning, no new signal.

2. **Strategy-frame diagnostics + direction edge** (Phases 1–4) —
   universe-isolation diagnostic, full rank-direction grid, measurement-
   driven exit selection, hybrid event-types. The question being asked is
   *"can we improve the existing event-driven strategy by relaxing one
   axis at a time?"*

3. **Signal-research harness + combined-signal portfolio** (Phases 5–6) —
   build a proper per-(symbol, date) feature panel, run univariate IC
   tests on every candidate feature we can extract from PIT data, then
   build a continuous-rank combined-signal portfolio from survivors. The
   question being asked is *"is the current event-driven discrete-trade
   architecture even the right one, or does a continuous-rank multi-
   signal portfolio dominate?"*

4. **Mandatory OOS gate** (Phase 7) — every finalist from anywhere above
   must clear the dedicated pre-2023 Bybit + Binance roots on all three
   sub-period thirds, both venues. Sign-consistency and DD bound enforced.

Total program compute: ~1,000–2,000 cells across phases, depending on
Phase 2/5 fan-out. On 5950X with 8-way parallelism at ~5 min/cell wall
that's a single overnight + one daytime. The compute is cheap; the bar
to call something a candidate is high specifically because the compute
is cheap.

---

## Background: why we are doing this

While debugging demo↔backtest reconciliation, we found that the production
filter `liquidity_migration_rank_improvement_min >= 150` was silently
being satisfied by symbols whose **rank deteriorated** by 150+ places
(not improved). Cause: u32 underflow in `prior_rank - current_rank`,
fixed in commit `78df65a` by casting to Int64.

In the same commit, we also added `--include-v5-fallback` (now
unconditional after the K1-K5 refactor) to the manifest builder, which
expanded the Bybit universe from 474 to 764 symbols by pulling in
currently-Trading perps the historical archive scrape never indexed.

The post-fix sweep baseline on the 2025-01-01 → 2026-05-28 window shows
**-42% max DD** vs the **~22% DD** we were quoting from older runs. That
shift has *two* candidate explanations, and we need to disentangle them:

- **Universe widening** (474 → 764 symbols) silently rewrites the
  liquidity-rank coordinate system for every symbol the strategy looks
  at. The trade rate scaled 1.69× (167/yr → 282/yr) while the universe
  scaled 1.61×. Those are essentially identical multipliers. The
  arithmetic strongly suggests universe widening is the dominant driver
  of the trade-count change — and therefore likely also the DD change,
  since more trades on systematically lower-quality (newer, less liquid)
  names dilutes per-trade Sharpe.
- **Bug-fix removed Population B** (rank-deteriorating symbols). Hard to
  isolate but plausibly small in absolute terms given the trade-count
  math.

Beyond explaining the DD shift, we are also asking whether the current
filter stack is the right shape and whether there is unmined signal in
the data we already have but don't currently exploit (funding, OI,
premium, residuals, cross-sectional ranks).

---

## Hypotheses

### H1 — Universe widening explains the majority of the DD shift

**Mechanism:** The strategy filters to rank 31–400 by liquidity. The
+290 v5-listing symbols are mostly low-liquidity recent listings. They
change the **denominator** of every symbol's daily liquidity rank, so
`prior7_liquidity_rank` and `rank_improvement` for pre-existing symbols
are silently relabelled. The wider universe also fires more total
trades (1.69× ratio matches 1.61× universe ratio), and the marginal
trades from newly-eligible names are systematically lower-quality.

**Predicted direction:** Restricting backtest to the 474-archive-only
universe on the same window shows trade count drop ~37% and DD shrink
back toward -25% (within 5pp of the historical -22%).

**Falsifier:** 474-baseline DD does not materially improve vs the 764
baseline (DD Δ < 5pp), implying the shift is NOT universe-driven and
something else (bug-fix, regime, code drift) is responsible.

### H2 — Inverse-direction rank deterioration is a tradable short edge

**Mechanism:** Rapid liquidity-rank deterioration = capital leaving =
loss of speculator interest = continuation lower. Distinct from the
rank-improvement-fade thesis (mean-reversion against fresh crowding) —
H2 is a *continuation* story on the same coordinate system.

**Predicted direction:** A direction=deterioration cell with threshold
≥150 produces sharpe-like > 1.5 and DD < 40% on both venues on the
2025-01+ window.

**Falsifier:** Sharpe-like ≤ 0 on either venue, OR DD > 50% on either
venue → the inference that "bug-driven trades were profitable" was an
artifact of the position-cap dynamic (bug-version filled slots with
mixed populations and the wins came from improvement-half, masking
deterioration-half losses).

### H3 — Two-sided rank-dislocation as a single event

**Mechanism:** If both rank-improvement-fade and rank-deterioration-
continuation carry edge, a single event firing on `|rank_improvement|
>= X` might capture both with one set of plumbing. Downside: the two
populations may have different optimal hold/stop parameters.

**Predicted direction:** Two-sided cell at threshold 150 beats either
single-direction cell on trade count and total return; sharpe-like
depends on whether the directions share optimal exit logic.

**Falsifier:** Two-sided cell underperforms both single-direction cells,
indicating the directions must be treated as separate event types if
either passes.

### H4 — The inverse-direction edge survives pre-2023 OOS

**Mechanism:** Same as H2 but tested on pre-2023 dedicated OOS roots —
the only truly clean evidence surface left to this strategy.

**Predicted direction:** sharpe-like > 0 on **both** pre-2023 venues,
**all three** sub-periods, with DD < 50% per sub-period.

**Falsifier:** Any sub-period DD > 60% OR sign flip across venues or
sub-periods → H2 is regime-conditional or noise, not a generalisable
edge.

### H5 — Some filters in the current event_demo stack hurt more than help

**Mechanism:** The current filter stack has ~14 gates accumulated over
many sweeps, each of which was at one point individually justified. The
joint behaviour was never re-tested. Some filters may now be cutting
edge instead of risk; some may be redundant with others; some may simply
have been mining noise at the time they were added.

**Predicted direction:** Per-filter leave-one-out shows ≥1 filter where
removal **improves** both-venue Sharpe AND DD, indicating the filter is
net-negative. Likely candidates from prior reasoning: `crowding_filter`
(probably overcounts), `stop_pressure_*` (high-friction reactive gate),
`pit_age_days_min` (excludes new listings that may have edge after
the v5-fallback expansion).

**Falsifier:** No filter improves both-venue metrics on removal. Every
gate is pulling weight; nothing to prune.

### H6 — One or more orthogonal features in our PIT panel has stable forecasting power for next-3-day returns

**Mechanism:** The current strategy fires on ONE event type with many
filters (AND-of-vetoes). A Jane-Street-y reading of the same data
extracts many orthogonal features and combines them (cross-sectional
rank, weighted, sized by risk). If any of these orthogonal features
shows stable univariate IC > 0.03 across sub-periods, it is a candidate
input to a combined-signal portfolio. Candidates we have data for and
do not currently exploit: funding rate Z-score, funding rate momentum,
OI Δ, OI/MCap, mark-index premium, realized vol regime, cross-
sectional return ranks, close-location, range extension.

**Predicted direction:** ≥3 of the ~20 tested features show |mean IC|
≥ 0.03 with stable sign across all three sub-periods on both venues.

**Falsifier:** All ~20 features have |mean IC| < 0.02 or sign-flipping
across sub-periods → there is no orthogonal forecasting signal in this
data, and the current event-driven architecture is correct in spirit
even if its specific filters are off.

### H7 — A combined-signal portfolio from H6 survivors beats the current event-driven strategy

**Mechanism:** Even if individual features have weak IC, a combined
portfolio (equal-weighted Z-score, or IC-weighted) trading top-decile
shorts each day, sized by 1/realized-vol, may capture diversified edge
the single-event strategy cannot.

**Predicted direction:** Combined portfolio sharpe-like > current
strategy on full 5-year backtest on both venues, with DD ≤ -35%.

**Falsifier:** Combined portfolio sharpe-like ≤ current OR DD > 50% →
the event-driven architecture is in fact dominant for this data.

---

## The strictness manifesto

Compute scaling makes the multiple-testing problem worse, not better.
A 1,000-cell program at α=0.05 yields ~50 expected false positives by
chance alone. The entire reason this program is legitimate is that
we are committing in writing — BEFORE any data is seen — to
substantially stricter per-cell decision rules than we used in the
original 10-cell sweep. Specifically:

### Standard candidate decision rule (applies to Phases 0, 1, 2, 3, 4, 6)

A cell is a **candidate** for further (Phase 7) investigation only if
**ALL** of:

- sharpe-like Δ ≥ **+0.5** on **both** venues vs the control, AND
- max-DD Δ ≤ **-5pp** on **both** venues vs the control, AND
- sign-consistent direction of edge across **all three** non-overlapping
  sub-period thirds of the in-sample window, AND
- per-sub-period trade count ≥ **50** on Bybit (≥ 30 on Binance), AND
- total-return sign positive on both venues across the full window.

A cell **falsifies its underlying hypothesis** if:
- sharpe-like Δ < -0.5 on either venue, OR
- sign flip between venues OR between sub-periods, OR
- any sub-period DD > 60%.

Cells failing strict candidate criteria but not strict falsifiers are
*inconclusive* — recorded but not pursued further. Inconclusive cells
do not count toward the FDR ceiling below.

### FDR ceiling (pre-committed)

No more than **3 candidates** from Phases 2/3 combined, and no more
than **3 candidates** from Phase 6 combined, may enter Phase 7 OOS. If
more cells satisfy the candidate criteria, pick the top-3 from each
group by **combined-venue mean Sharpe** (pre-committed tie-break) and
**reject the rest as multi-testing artifacts.** The rejected cells are
NOT a "menu we'll come back to later" — they are decisively closed
unless re-pre-registered with new motivation.

### Phase 7 OOS gate (final)

A Phase 7 candidate becomes a **promotable finding** only if **ALL** of:

- sharpe-like > 0 on **both** pre-2023 venues, **all three** sub-periods,
- DD < 50% on both venues, all three sub-periods,
- sign-consistent edge direction vs in-sample, both venues, all thirds,
- per-sub-period trade count ≥ 20 on Bybit (≥ 15 on Binance).

A Phase 7 candidate is **rejected** if any of:
- single sub-period DD > 60%,
- sign flip between any two of {Bybit / Binance / sub-periods},
- sharpe-like < 0 on any sub-period of either venue.

Pass-and-promote candidates still go through forward demo for ≥30 days
reconciled against the same-config backtest before mainnet
consideration. The integrity standard's `paper_ready` label is the
only ladder rung to actual capital.

### What this manifesto forbids

- Loosening any threshold above after seeing any phase's results.
- Re-running any cell with different exits / windows to "see if it's
  better" without amending this document with a new dated entry.
- Citing any non-Phase-7 result as alpha evidence anywhere.
- Carrying forward a candidate that flunked Phase 7 into "well let's
  try it on demo anyway" — Phase 7 fail = closed.
- Adding a Phase 8 to chase a near-miss from Phase 7. Near-misses are
  filed as inconclusive and closed.

---

## Code changes required

### Change 1 — Rank-direction flag (~1h)

```python
# liquidity_migration/volume_events.py — VolumeEventResearchConfig:
liquidity_migration_rank_direction: str = "improvement"  # improvement|deterioration|both
```

Filter site in `_filter_liquidity_migration` and the matching tail-event
predicate switch on the direction value:

```python
delta = (pl.col("prior7_liquidity_rank").cast(pl.Int64)
         - pl.col("liquidity_rank").cast(pl.Int64))
if direction == "improvement":
    predicate &= (delta >= threshold)
elif direction == "deterioration":
    predicate &= (delta <= -threshold)
elif direction == "both":
    predicate &= (delta.abs() >= threshold)
```

CLI: `--liquidity-migration-rank-direction {improvement,deterioration,both}`

**Backward compat:** default `direction="improvement"` preserves current
behavior bit-for-bit.

**Tests:** pin all three direction values against synthetic fixtures.
Default-improvement run must match pre-change baseline.

### Change 2 — Sweep orchestrator parallelism (~2h)

> _Historical plan. This was implemented and now lives in `scripts/_sweep_runtime.py`
> (`ThreadPoolExecutor`, `SWEEP_MAX_WORKERS` env, memory-aware cap); the original
> `scripts/sweep_cells.py` was removed in the 2026-05-28 cleanup. Round 1 is COMPLETE._

`scripts/sweep_cells.py` currently runs cells sequentially. Replace inner
loop with `concurrent.futures.ThreadPoolExecutor(max_workers=N)` where
`N` defaults to 8 (configurable via env). Each cell already sets
`POLARS_MAX_THREADS=4` so 8 cells × 4 polars threads = 32 threads = full
5950X SMT occupancy. Status-row writes still flush after every cell
completes, with a lock for the shared `summary.csv` write.

### Change 3 — Legacy-archive manifest builder (~30min)

```bash
scripts/build_legacy_archive_manifest.py
```

Reads `archive_trade_manifest/**/*.parquet`, filters to rows where
`source == "bybit_public_trading_archive"` (i.e. drops v5-listing rows),
writes to a side-copy data root at
`~/SHARED_DATA/bybit_full_pit_archive_only/`. Klines, funding, OI etc.
are SYMLINKED from the main root — only the manifest differs. This lets
Phase 1 run against the 474-only universe without re-downloading
anything.

### Change 4 — Signal-research harness (~1 day)

New module `liquidity_migration/signal_harness.py`:

```python
def build_feature_panel(
    data_root: Path,
    *,
    start: str,
    end: str,
    feature_specs: list[FeatureSpec],
    forward_horizons: list[int] = [1, 3, 7],
) -> pl.DataFrame:
    """Return a (symbol, date, feature_1..feature_k, fwd_ret_1d, fwd_ret_3d, fwd_ret_7d) panel.
    All features are computed at end-of-day-close (decision_ts), all forward
    returns are entry+1h to entry+1h+Nd to match the executable fill model."""

def compute_univariate_ic(
    panel: pl.DataFrame,
    *,
    feature: str,
    target: str,
    sub_periods: int = 3,
) -> ICReport:
    """Per-day cross-sectional Spearman rank correlation, averaged across days,
    plus sub-period stability stats."""

def build_combined_signal_portfolio(
    panel: pl.DataFrame,
    *,
    surviving_features: list[str],
    weighting: str = "equal",  # or "ic_weighted"
    top_decile: float = 0.1,
    vol_target_per_name: float = 0.01,
) -> pl.DataFrame:
    """Return a daily portfolio (symbol, date, weight) that's the negative-
    Z-score-sum top-decile, sized by 1/realized-vol per name."""
```

CLI subcommand: `signal-harness {build-panel,compute-ic,combined-portfolio}`.

**Features in scope (20):**

| # | Feature | Source dataset | Hypothesis |
|---|---|---|---|
| 1 | `xs_rank_ret_1d` | klines_1h | 1d cross-sectional return rank — short-horizon mean rev |
| 2 | `xs_rank_ret_3d` | klines_1h | 3d cross-sectional return rank |
| 3 | `xs_rank_ret_7d` | klines_1h | 7d rank — momentum vs mean rev |
| 4 | `xs_rank_ret_30d` | klines_1h | 30d rank — longer momentum |
| 5 | `liquidity_rank` | derived | current liquidity rank (what the strategy already uses) |
| 6 | `liquidity_rank_delta_7d` | derived | rank Δ vs 7d ago, continuous version of event signal |
| 7 | `liquidity_rank_delta_30d` | derived | rank Δ vs 30d ago — slower migration |
| 8 | `turnover_delta_7d` | klines_1h | turnover Δ vs prior 7d mean, normalised |
| 9 | `turnover_delta_30d` | klines_1h | turnover Δ vs prior 30d mean |
| 10 | `funding_rate_z` | funding | cross-sectional Z-score of funding rate |
| 11 | `funding_rate_delta_7d` | funding | funding momentum |
| 12 | `oi_delta_7d` | open_interest | OI Δ vs 7d, normalised by ADV |
| 13 | `oi_to_adv` | open_interest + klines | OI / 30d ADV — positioning intensity |
| 14 | `premium_index_z` | premium_index_1h | cross-sectional Z of mark-index premium |
| 15 | `realized_vol_7d` | klines_1h | annualised 7d realized vol |
| 16 | `vol_of_vol_30d` | klines_1h | std of daily returns over 30d |
| 17 | `close_location_1d` | klines_1h | (close-low)/(high-low) for today's session |
| 18 | `range_extension_30d` | klines_1h | today's range / 30d avg range |
| 19 | `dist_from_30d_high` | klines_1h | (close - 30d high) / 30d high |
| 20 | `dist_from_30d_low` | klines_1h | (close - 30d low) / 30d low |

All features causal at decision_ts (end-of-day close). Forward returns
computed entry+1h → entry+1h+Nd to match the executable 1h delay.

---

## Phases

### Phase 0 — Filter LOO audit (cheap, do first, no dependencies)

**Purpose:** Empirically test H5 — gut filters that don't pay rent.

**Method:** For each of the current event_demo filter knobs, run the
strategy with that knob DISABLED (set to 0, the universe-wide default,
or `disabled` sentinel as appropriate). All other knobs at production
defaults. Compare to control (= 00_baseline from the current in-flight
sweep).

**Cells (~14 × 2 venues = 28 runs):**

| Cell | Filter disabled |
|---|---|
| `P0_noflt_turnover_ratio` | `--liquidity-migration-turnover-ratio-min 0` |
| `P0_noflt_event_rank_frac` | `--liquidity-migration-event-rank-fraction-max 1.0` |
| `P0_noflt_day_return` | `--liquidity-migration-day-return-min -1.0` |
| `P0_noflt_residual_return` | `--liquidity-migration-residual-return-min 0` |
| `P0_noflt_close_location` | `--liquidity-migration-close-location-min 0` |
| `P0_noflt_pit_age` | `--liquidity-migration-pit-age-days-min 0` |
| `P0_noflt_crowding` | `--liquidity-migration-crowding-filter none` |
| `P0_noflt_stop_pressure` | `--stop-pressure-stop-count 999` |
| `P0_noflt_realized_loss` | `--realized-loss-pressure-loss-count 999` |
| `P0_noflt_rank_min` | `--universe-rank-min 1` |
| `P0_noflt_rank_max` | `--universe-rank-max 99999` |
| `P0_noflt_cooldown` | `--cooldown-days 0` |
| `P0_noflt_max_active` | `--max-active-symbols 999` |
| `P0_noflt_entry_delay` | `--entry-delay-hours 0` |

**Window:** Full 2023-04-01 → 2026-05-28 (the longest clean window the
current data root supports without partial PIT outside the affordable
repair set; if `archive-download-klines-1h` repairs the 85 2023-24
partitions in advance, expand to 2023-01-01).

**Decision rule:** Standard candidate rule. Critically, a filter that
**improves** Sharpe AND DD when REMOVED is a candidate for **permanent
removal from the production config**. The standard 5pp/0.5 thresholds
apply — small improvements are inconclusive.

**Compute:** 28 cells × ~7 min (longer window) × parallel-8 ≈ **~25 min wall.**

### Phase 1 — Universe-isolation diagnostic

**Purpose:** Test H1 — quantify the universe-widening contribution to the
DD shift.

**Method:** Build legacy-archive manifest side-copy (Change 3). Re-run
several representative configs against it.

**Cells (~12 × 2 venues = 24 runs):**

| Cell | Universe | Config |
|---|---|---|
| `P1_baseline_474` | 474 archive-only | current promoted defaults |
| `P1_baseline_764` | 764 full | current promoted defaults (control) |
| `P1_turn10M_474` | 474 | + min turnover $10M (matches in-flight A3) |
| `P1_turn10M_764` | 764 | + min turnover $10M |
| `P1_rankmax200_474` | 474 | + universe_rank_max 200 (matches E1) |
| `P1_rankmax200_764` | 764 | + universe_rank_max 200 |
| `P1_rankimp200_474` | 474 | + rank_improvement_min 200 (matches B1) |
| `P1_rankimp200_764` | 764 | + rank_improvement_min 200 |
| `P1_hold2_474` | 474 | + hold_days 2 (matches D1) |
| `P1_hold2_764` | 764 | + hold_days 2 |
| `P1_combo_474` | 474 | turnover10M + hold=2 + rankimp200 |
| `P1_combo_764` | 764 | same combo |

**Window:** 2025-01-01 → 2026-05-28 (matches in-flight sweep for
direct comparison). Run BUT NEVER PROMOTE on a 474 cell.

**Decision rule:** **DESCRIPTIVE ONLY.** This phase produces deltas
quantifying the universe effect; it produces no promotion decision.
The 474 cells are labeled `biased_benchmark`. We commit in writing now
that no 474-restricted configuration will ever be traded in production,
regardless of how attractive the numbers.

**Interpretation rule (a priori):** If the avg sharpe-like Δ across the
6 paired 474-vs-764 cells is > +0.5 AND avg DD Δ is < -8pp, conclude
universe widening explains most of the DD shift. Phase 2's interpretation
of H2 evidence is then explicitly downweighted (we know we're seeing
universe contamination in the in-sample number).

**Compute:** 24 cells × ~5 min × parallel-8 ≈ **~16 min wall.**

### Phase 2 — Rank-direction full grid

**Purpose:** Test H2 + H3 thoroughly.

**Pre-requisite:** Change 1 (rank-direction flag) shipped + tested + merged.

**Cells (33 × 2 venues = 66 runs):**

For each direction in {improvement, deterioration, both}, for each
threshold in {25, 50, 75, 100, 125, 150, 175, 200, 250, 300, 400}:

| Cell template | Direction | Threshold |
|---|---|---:|
| `P2_imp_{T}` | improvement | T ∈ {25..400} |
| `P2_det_{T}` | deterioration | T ∈ {25..400} |
| `P2_both_{T}` | both | T ∈ {25..400} |

That's 33 cells. `P2_imp_150` is the control (= current production).

All other parameters at production defaults.

**Window:** 2023-04-01 → 2026-05-28 (3+ years, with three non-overlapping
sub-periods of ~13 months each for the cross-sub-period sign-consistency
check). If 85 2023-24 partitions are repaired in advance, extend to
2023-01-01.

**Decision rule:** Standard candidate decision rule + FDR ceiling
(max 3 candidates may forward to Phase 7).

**Compute:** 66 cells × ~10 min (longer window) × parallel-8 ≈ **~85 min wall.**

### Phase 3 — Exit selection for any Phase 2 candidate (conditional)

**Triggered if:** at least one direction-deterioration or direction-both
cell qualifies as a candidate in Phase 2.

**Purpose:** Continuation drains likely want different exits than mean-
reversion fades. Pick exits via principled measurement + bounded
sensitivity grid — NOT Sharpe ranking.

**Phase 3a — Measure the population (descriptive, no fitting).**

For the winning Phase 2 candidate, compute the empirical adverse /
favourable excursion distribution across all its trades:

- p25, p50, p75, p90 of adverse excursion (worst price against entry
  within hold window)
- p25, p50, p75, p90 of favourable excursion (best price for entry)
- p25, p50, p75, p90 of time-to-peak-adverse (hours from entry)
- p25, p50, p75, p90 of time-to-peak-favourable (hours from entry)

Output: `phase3a_excursions_<cell-id>.json`.

**Phase 3b — Derive 3 a-priori exit candidates from 3a.**

Rules committed in writing now:

| Candidate | SL | TP | hold_days |
|---|---|---|---|
| `P3_natural` | 1.5 × p75 adverse | 1.0 × p75 favourable | round(mean t-to-peak-fav / 24) |
| `P3_conservative` | 1.0 × p90 adverse | 1.0 × p50 favourable | as `P3_natural` |
| `P3_extended` | 1.5 × p75 adverse | none (let it run) | round(p90 t-to-peak-fav / 24) |

Floor SL at 0.06, TP at 0.10, hold at 1. Cap SL at 0.30, TP at 0.50,
hold at 7.

**Phase 3c — Sensitivity confirmation grid (informational only).**

An 8 × 8 × 5 = 320-cell grid sweep around the Phase 3b winner:

| Axis | Values |
|---|---|
| SL | {0.06, 0.08, 0.10, 0.12, 0.14, 0.16, 0.18, 0.20} |
| TP | {0.10, 0.14, 0.18, 0.22, 0.26, 0.30, 0.35, 0.40} |
| hold | {1, 2, 3, 4, 5} |

**Purpose of the grid:** verify the Phase 3b winner sits in a flat /
robust neighbourhood — i.e. small perturbations don't catastrophically
collapse Sharpe. If the Phase 3b winner is on a cliff edge (neighbours
30%+ worse), the candidate is rejected as unstable regardless of
absolute Sharpe.

**The grid is NOT used to pick a winner.** Selection remains by the
Phase 3b rule + Phase 2 decision rule. The grid is a falsifier (cliff
edge → reject) and a diagnostic (sensitivity heatmap published).

**Compute:** 3 (3b) + 320 (3c) × 2 venues = 646 runs × ~10 min × parallel-8 ≈ **~13h wall.** One overnight.

### Phase 4 — Hybrid event-types (conditional)

**Triggered if:** Phase 2 produces a candidate AND Phase 3 produces
viable exits.

**Purpose:** If both directions carry edge, is it better to run them as
ONE two-sided event with shared cooldown, or as TWO separate event
types with independent cooldowns and possibly different exits?

**Requires:** Code extension to add `liquidity_drain` as a separate
event type alongside `liquidity_migration`. ~3 hours.

**Cells (10 × 2 venues = 20 runs):**

| Cell | Events | Notes |
|---|---|---|
| `P4_lm_only` | liquidity_migration with control exits | control |
| `P4_ld_only` | liquidity_drain with Phase 3 winner exits | best-Phase-2-deterioration cell |
| `P4_lm_and_ld_sep` | both, separate slot pools (3 slots each) | independent |
| `P4_lm_and_ld_shr` | both, shared slot pool (3 total) | first-come-first-served |
| `P4_lm_or_ld_prio_lm` | both, shared pool, lm preempts ld | lm priority on conflict |
| `P4_lm_or_ld_prio_ld` | both, shared pool, ld preempts lm | ld priority on conflict |
| `P4_two_sided_event` | single two-sided event = both-direction cell | one event type |
| `P4_lm_ld_diff_exits` | both, separate, lm with control exits, ld with Phase 3 winner | direction-specific exits |
| `P4_lm_ld_corr_filter` | both, separate, skip if same symbol fired the other within 7 days | conflict-resolved |
| `P4_lm_ld_basket_corr_cap` | both, separate, cap basket pairwise corr at 0.6 | risk-managed |

**Compute:** 20 cells × ~10 min × parallel-8 ≈ **~25 min wall.**

### Phase 5 — Signal-research harness + univariate IC

**Purpose:** Test H6 — does our PIT data contain orthogonal forecasting
features we don't currently use?

**Pre-requisite:** Change 4 (signal harness module) implemented + tested.

**Phase 5a — Build the full feature panel.**

```bash
.venv/bin/python -m liquidity_migration --data-root ~/SHARED_DATA/bybit_full_pit \
  signal-harness build-panel \
  --start 2021-01-01 --end 2026-05-28 \
  --features all \
  --forward-horizons 1,3,7 \
  --output ~/SHARED_DATA/bybit_full_pit/feature_panel.parquet
```

Same for binance. Output panel size: ~5 years × ~500 symbols/day × ~250
trading days × 20 features × 3 forward horizons ≈ 7.5M rows × 25 cols.

**Phase 5b — Compute univariate IC per feature per venue.**

For each of 20 features × 3 horizons × 2 venues = 120 IC measurements.
Each measurement:
- Cross-sectional Spearman per day, averaged across all days = mean IC
- Per-sub-period mean IC for 3 sub-periods = sub-period IC
- IC t-stat (mean / std × √N_days)
- IC sign-consistency across sub-periods

Output: `signal_harness_ic_report.json` per venue.

**Decision rule:** A feature **survives** to Phase 6 if **ALL** of:
- |mean IC| ≥ 0.03 on **both** venues, AND
- sign-consistent across **all three** sub-periods on both venues, AND
- IC t-stat |t| ≥ 3 on both venues.

Surviving features are pre-committed candidates for Phase 6
combination. Non-surviving features are filed and not reused as
"alternative" tests later. FDR ceiling: no more than 5 features may
survive to Phase 6. If more do, top-5 by combined-venue mean |IC|.

**Compute:** Panel build = ~30 min each venue (one-off, then cached).
IC computation = ~5 min per feature × 20 = ~100 min single-threaded, but
embarrassingly parallel by feature → ~15 min wall on 8 cores. Total: **~75 min wall.**

### Phase 6 — Combined-signal portfolio (conditional on Phase 5)

**Triggered if:** ≥3 features survive Phase 5.

**Purpose:** Test H7 — does a combined-signal continuous-rank portfolio
beat the current event-driven discrete-trade strategy?

**Method:** Three combination schemes, each tested on both venues.

**Cells (3 × 2 venues = 6 runs, plus 2 calibration sweeps):**

| Cell | Combination | Sizing |
|---|---|---|
| `P6_equal_z` | Equal-weighted Z-score sum across survivors | 1/realized-vol per name |
| `P6_ic_weighted` | Survivor IC × Z-score, weighted | 1/realized-vol per name |
| `P6_top_decile_short` | Take top-decile short-side signal each day | 1/realized-vol per name |

Plus two calibration sweeps:
- `P6_horizon_sweep` — same 3 schemes × {1d, 3d, 7d} forward horizons = 9 cells
- `P6_decile_sweep` — same 3 schemes × {top 5%, 10%, 20%} threshold = 9 cells

Total: 24 cells × 2 venues = 48 runs.

**Decision rule:** Standard candidate decision rule vs the current
event-driven baseline. FDR ceiling: max 3 of these may forward to Phase 7.

**Compute:** 48 cells × ~10 min × parallel-8 ≈ **~60 min wall.**

### Phase 7 — Pre-2023 OOS gate (mandatory final)

**Triggered if:** ANY candidate emerges from Phases 0, 1, 2, 3, 4, or 6.

**Purpose:** Confirm in-sample candidates survive on truly clean data.

**Pre-requisite:** Pre-2023 dedicated OOS roots exist and are current
with the code (v5-listing supplement + Int64 fix). If not, rebuild
first (~6h data download).

**Cells:** For each finalist (max 3 from Phases 2-4 group + max 3 from
Phase 6 group = max 6 finalists):

- Each finalist × 2 venues × 3 sub-period thirds × 1 cell = 6 runs per
  finalist
- Plus baseline reference for each venue × sub-period = 6 reference
  runs

Worst case: 6 finalists × 6 runs + 6 ref = **42 runs.**

**Decision rule:** Phase 7 OOS gate (see Strictness Manifesto). All
sub-periods × both venues × sign-consistent × DD < 50% required.

**Compute:** 42 cells × ~10 min × parallel-8 ≈ **~55 min wall** (plus
data-rebuild overhead if needed).

---

## Compute plan

### Per-cell environment

```bash
export POLARS_MAX_THREADS=4
export RAYON_NUM_THREADS=4
```

8 cells in parallel × 4 polars threads = 32 threads = full 5950X SMT.

### Phase-by-phase wall-time on the 5950X

| Phase | Cells | Wall | Cumulative |
|---|---:|---:|---:|
| 0 — Filter LOO | 28 | 25 min | 25 min |
| 1 — Universe diagnostic | 24 | 16 min | 41 min |
| 2 — Direction grid | 66 | 85 min | 2h 06m |
| 3a/b — Exits (conditional) | 6 | 5 min | 2h 11m |
| 3c — Sensitivity grid | 640 | 13h | 15h 11m |
| 4 — Hybrid events | 20 | 25 min | 15h 36m |
| 5a — Build feature panel | (one-off) | ~30 min × 2 venues | 16h 36m |
| 5b — Univariate IC | 120 IC measurements | 75 min | 17h 51m |
| 6 — Combined-signal portfolio | 48 | 60 min | 18h 51m |
| 7 — Pre-2023 OOS | 42 | 55 min | 19h 46m |

**Total: ~20h** if every conditional triggers AND Phase 3c sensitivity
grid runs. Trivially one overnight (Phase 0-2 + 5 = ~5h) followed by a
daytime (Phase 3c if needed, plus 4, 6, 7 = ~15h). Plus pre-2023 data
rebuild (~6h) if those roots aren't current.

**Aggressive parallelism notes:**
- Phases 0, 1, 5 have no dependencies on each other or on the code
  change. They can run in three parallel tracks **simultaneously** on
  separate sub-pools of the 5950X (e.g. 4 cores each).
- Phase 2 requires Change 1 merged first.
- Phases 3, 4 require Phase 2 candidate.
- Phase 6 requires Phase 5 survivors.
- Phase 7 is the gate everything passes through.

---

## Threats to inference

Cross-referenced to `docs/backtesting_errors_we_never_repeat.md`.

| # | Threat | Mitigation |
|---|---|---|
| #1 | Future universe selection | The 474 cell is biased_benchmark only; never promotable. Full universe is the live, PIT-correct one. |
| #2 | Future info in signals | Phase 5 feature spec explicitly defines decision_ts = end-of-day close; all forward returns computed entry+1h → entry+1h+Nd. Tests pin causality per feature. |
| #4 | Revised / non-PIT data | All runs against full-PIT roots; no current ticker pre-filter. |
| #12 | Instrument lifecycle | Phase 5 panel explicitly includes delisted / migrated / renamed symbols via the manifest's full coverage. |
| #15 | Warm-started state | Standard `volume-events` cold-start; no carryover from sweep cells. |
| #16 | Same-code illusion | The rank-direction flag becomes config — live demo daemon honours whatever direction is set, no backtest-only branches. |
| #17 | Parameter mining | Standard candidate decision rule is pre-registered in this doc and tightened from the 2026-05-28 sweep. Phase 3c uses sensitivity grid as falsifier, not selector. |
| #18 | OOS reuse | Pre-2023 roots have been touched recently (the original "pre-2023 fails everything" call). Phase 7 compensates by requiring BOTH venues AND ALL 3 sub-periods. Any finalist still goes to ≥30-day forward demo before any mainnet consideration. |
| #19 | Multiple testing | Worst case ~1,000 cells. Standard decision rule with strict thresholds + FDR ceiling (max 3 candidates per phase group to Phase 7) brings effective FDR to a level where a hit is meaningful. Phase 5's univariate IC test uses a separate decision rule (|IC|≥0.03 AND sub-period sign-consistency AND |t|≥3) which roughly Bonferroni-corrects for the 20 features tested. |
| #20 | Bad accounting | Same `volume-events` accounting as baseline; Phase 6 portfolio uses identical fill/cost model. |
| #21 | Hidden common risk | Phase 4 P4_lm_ld_basket_corr_cap explicitly tests basket-correlation control. Phase 6 portfolios sized by 1/realized-vol per name limit per-name dollar exposure to vol regime. |
| #22 | Venue mechanics fantasy | Cost-multiplier 3× retained throughout; entry-delay-hours 1 honoured; max-mkt-order-qty cap honoured via Phase G1 split logic. |
| #23 | Pretty-report bias | All cells produce trade ledgers, equity curves, monthly P&L, config-hash. Mandatory artifacts. Phase 5 IC report includes per-day IC time-series, not just point estimate. |
| #24 | Unreconciled live drift | Any Phase 7-passing finalist goes to demo first with reconciliation against same-config backtest, never straight to mainnet. |
| #25 | All-or-nothing compute | Sweep orchestrator flushes summary CSV after every cell completion; if killed mid-sweep, every completed cell's report is preserved. |

### Special note on Phase 7 OOS reuse risk

The pre-2023 roots were already used as the kill-shot evidence for the
"strategy fails pre-2023" call. Using them again means they have been
touched. Strictly, this dilutes their evidentiary value. Two mitigations:

1. The Phase 7 decision rule requires **both** venues **and all three**
   sub-periods to pass — a much harder bar than a single single-venue look.
2. Any Phase-7-passing candidate goes to **forward demo** for ≥30 days
   reconciled against the same-config backtest before any mainnet
   consideration. Forward evidence is the only truly clean surface
   remaining.

If neither feels strong enough, the alternative is to acquire NEW data
not yet used — e.g. a recent 30-day window collected after Phase 6
completes. That's another ~1 month of waiting.

---

## Pre-registration commitments

By committing this plan to git, the operator and assistant commit in
advance to:

1. **Not trading** any Phase 1 universe-restricted parameter set,
   regardless of how attractive its backtest numbers are. The 474 set
   is today's archive coverage retroactively applied — a known
   survivorship/PIT contamination.
2. **Not citing** any pre-Phase-7 result as alpha evidence anywhere.
   Phase 0-6 findings are filed under `biased_benchmark`, `candidate`,
   `inconclusive`, or `rejected`, never `alpha`.
3. **Not adjusting** the candidate decision rule, FDR ceilings, or
   Phase 7 OOS gate after seeing any phase's results. Loosening
   thresholds post-hoc to fit a winner is explicit p-hacking and
   forbidden.
4. **Not running** any cell not in the menu above without amending this
   doc with a dated entry. New cells require a new pre-reg.
5. **Failure cases are informative.** If H1-H7 all falsify, we publish
   the null and stop. The strategy stays in its current state and we
   move on. There is no obligation to "ship something" because we spent
   compute.
6. **Respecting the FDR ceiling.** Max 3 candidates per phase group to
   Phase 7. If more cells satisfy candidate criteria, top-N by combined-
   venue Sharpe (pre-committed tie-break) and **closed-rejected** for
   the rest.
7. **Pre-2023 roots are the final gate.** A Phase 7 failure means closed;
   we do not try the cell on forward demo "anyway" or look for a Phase 8
   escape.

---

## Timeline

Assuming no surprises:

| Day | Activity |
|---|---|
| Today | Operator + assistant review this plan, agree or amend. |
| Day 1 morning | Implement Change 1 (rank-direction flag) + Change 2 (sweep parallelism) + Change 3 (legacy-archive manifest builder). Tests + pre-push gate. |
| Day 1 afternoon | Implement Change 4 (signal-harness module) + tests + pre-push gate. |
| Day 1 evening | Operator git-pulls on 5950X. Kicks off Phases 0, 1, 5a in parallel. |
| Day 2 morning | Read Phase 0/1/5a results. If Phase 1 confirms universe-widening dominance, consider deploying turnover floor / rank tightening to demo as a tactical hotfix (pre-registered, decision-ruled, but acted on). |
| Day 2 daytime | Kick off Phase 2 + Phase 5b in parallel. Aggregate Phase 0/1 readout. |
| Day 2 evening | Phase 2 + Phase 5b done. Determine Phase 2 candidate(s) and Phase 5 survivors. Kick off Phase 3a/b (conditional), Phase 3c sensitivity grid (overnight), Phase 6 (conditional). |
| Day 3 morning | Phase 3c sensitivity grid done. Determine Phase 3 winner. Phase 6 done. Kick off Phase 4 (conditional) + Phase 7 (mandatory if any candidate exists). |
| Day 3 afternoon | Phase 4 + Phase 7 done. Aggregate report. Honest verdict per H1-H7. |
| Day 4+ (conditional) | If a Phase 7 candidate exists, propose demo deployment. 30-day reconciled forward demo. |

**Hard end-date on Phase 7:** 2026-06-15. If by then no Phase 7
candidate has emerged, the inverse-direction edge hypothesis is
rejected and we move on. Committed.

---

## Open questions / things to confirm before kicking off

1. **Pre-2023 roots state.** Operator to confirm the 5950X has either
   the pre-2023 Bybit and Binance roots already, OR ~50 GB free space
   for a fresh rebuild. Phase 7 needs them.

2. **"Do nothing" arm.** Explicit commitment to leaving the current
   promoted profile in place if all of H1-H7 falsify. Recorded.

3. **Demo-deployment rule for a Phase-7-passing finalist.** 30-day
   forward demo reconciled against same-config backtest, then operator
   decision — same as current promoted profile? Or stricter?

4. **Hot-fix policy on Phase 1.** If Phase 1 confirms the universe-
   widening story strongly (Δ DD > 10pp on the paired comparisons),
   do we deploy the turnover floor / rank tightening to demo
   immediately as a tactical hotfix while Phases 2-7 continue? My
   recommendation: yes, with a dated pre-reg referencing this plan
   as motivation. The bar for that hotfix is the same standard
   candidate rule.

5. **Signal harness scope creep.** Phase 6 P6_horizon_sweep + P6_decile_sweep
   expand to 24 cells × 2. Acceptable, but if Phase 6 produces no candidate
   and we go fishing further, that's an amendment to this plan, not a
   silent expansion.

---

## Appendix A — exact CLI for the current baseline control (= `P2_imp_150`)

```bash
.venv/bin/python -m liquidity_migration \
  --data-root ~/SHARED_DATA/bybit_full_pit \
  --config configs/volume_alpha.default.yaml \
  volume-events \
  --start 2023-04-01 --end 2026-05-28 \
  --allow-partial-pit \
  --report-dir ~/SHARED_DATA/bybit_full_pit/reports/<phase-id>/<cell> \
  --event-types liquidity_migration \
  --thresholds 0.4 \
  --hold-days 3 \
  --sides reversal \
  --stop-loss-pcts 0.12 \
  --take-profit-pcts 0.26 \
  --cost-multipliers 3 \
  --gross-exposure 1.0 \
  --entry-delay-hours 1 \
  --entry-policy promoted_quality_squeeze \
  --max-active-symbols 3 \
  --cooldown-days 5 \
  --rank-exit-threshold 0.55 \
  --universe-rank-min 31 \
  --universe-rank-max 400 \
  --liquidity-migration-rank-direction improvement \
  --liquidity-migration-rank-improvement-min 150 \
  --liquidity-migration-turnover-ratio-min 6.0 \
  --liquidity-migration-event-rank-fraction-max 0.90 \
  --liquidity-migration-day-return-min 0.0 \
  --liquidity-migration-residual-return-min 0.08 \
  --liquidity-migration-close-location-min 0.30 \
  --liquidity-migration-pit-age-days-min 90 \
  --liquidity-migration-crowding-filter union_pathology \
  --stop-pressure-window-days 10 \
  --stop-pressure-stop-count 7 \
  --realized-loss-pressure-window-days 5 \
  --realized-loss-pressure-loss-count 6
```

Per-cell variations are documented in each phase's cell table.

---

## Appendix B — label mapping

Mapping to `docs/backtesting_errors_we_never_repeat.md` labels:

- **Phase 0 results:** `exploratory` (negative finding = "filter X
  earned its weight"). A removed filter that ships gets its own dated
  pre-reg commit.
- **Phase 1 results:** `biased_benchmark`. **Never** promotable.
- **Phase 2, 3, 4, 6 candidates:** `candidate`. Not promotion evidence on
  their own.
- **Phase 2-6 falsifiers and inconclusives:** `exploratory` (negative
  finding) or `rejected`.
- **Phase 5 IC report:** `exploratory` — informational regardless of
  outcome.
- **Phase 7 passing candidate (in-sample + OOS):** still only
  `candidate` pending forward demo.
- **Forward-demo-confirmed Phase 7 pass:** `paper_ready` — eligible for
  the demo→mainnet promotion process the rest of the repo's standards
  cover.

A run never skips a label rung. We do not declare alpha mid-stream.

---

## Appendix C — what is NOT in scope

Explicitly NOT in scope for this program:

- **Microstructure features.** We don't have order book or signed-flow
  data. The signed-flow pipeline was retired. Any work here requires
  rebuilding ingestion, separate effort.
- **News / sentiment signals.** No NLP work.
- **ML signal combination.** Phase 6 uses dumb combination (equal Z,
  IC-weighted, top decile). No xgboost / neural nets. We only graduate
  to ML if dumb-combination produces a Phase 7-passing finalist AND a
  separate pre-reg motivates the additional fitting complexity.
- **Cross-venue arb / pairs / spreads.** Each venue tested independently.
- **Long-side signals.** Strategy is short-side only. We are not adding
  long signals here.
- **Funding-harvesting standalone.** Funding is a candidate feature in
  Phase 5; we are not building a separate funding-arb strategy.
- **Re-promotion of the existing strategy.** If all of H1-H7 falsify
  AND filter LOO finds no removable filter, the current promoted
  profile stays. No "let's promote a slightly-different version of the
  same thing" path.

These exclusions are deliberate scope discipline. Each could be a
future research program on its own pre-reg.
