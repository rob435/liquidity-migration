# Phase 5 — signal-research harness + univariate IC (pre-registration)

**Date:** 2026-05-27
**Stage:** pre-registered, not yet run.
**Parent plan:** [2026-05-27 multi-phase research plan](2026-05-27-rank-direction-edge-and-universe-isolation-research-plan.md)
**Phase label per plan Appendix B:** `exploratory` (IC report is
informational regardless of outcome; surviving features feed Phase 6
which produces candidates).

## Purpose

Test H6 from the parent plan: *one or more orthogonal features in our
PIT panel has stable forecasting power for next-3-day returns*. The
current strategy fires on ONE event type with many filters; a
Jane-Street-y read of the same data extracts many orthogonal features
and combines them. If any individual feature shows stable univariate IC
≥ 0.03 with sign-consistent direction across sub-periods on both
venues, it becomes a candidate input to a combined-signal portfolio
(Phase 6).

## Pre-requisite

Code change 4 (`liquidity_migration.signal_harness`) merged — done as
of commit `ed7c5d8` on 2026-05-27. The 20 features listed in the parent
plan are registered in `FEATURE_REGISTRY` and exposed via the CLI
subcommand `signal-harness {build-panel,compute-ic,combined-portfolio}`.

## Window

**Panel build:** 2021-01-01 → 2026-04-30 (cross-venue minimum).

Long history matters here — IC reliability scales with sample size, and
the rank-based features stabilize over multiple regime cycles. 2021-01
is Bybit's earliest perp data; 2026-04-30 is Binance's last available
date.

**Sub-period split:** the in-window date range is split into 3
non-overlapping thirds for sign-consistency. ~17 months per third.

## Phase 5a — build the feature panel (one-off per venue)

```bash
# Bybit
.venv/Scripts/python.exe -m liquidity_migration \
  --data-root ~/SHARED_DATA/bybit_full_pit \
  signal-harness build-panel \
  --start 2021-01-01 --end 2026-04-30 \
  --features all \
  --forward-horizons 1,3,7 \
  --universe-min-daily-turnover 1000000 \
  --output ~/SHARED_DATA/bybit_full_pit/feature_panel_2026-05-27.parquet

# Binance
.venv/Scripts/python.exe -m liquidity_migration \
  --data-root ~/SHARED_DATA/binance_full_pit \
  signal-harness build-panel \
  --start 2021-01-01 --end 2026-04-30 \
  --features all \
  --forward-horizons 1,3,7 \
  --universe-min-daily-turnover 1000000 \
  --output ~/SHARED_DATA/binance_full_pit/feature_panel_2026-05-27.parquet
```

`--universe-min-daily-turnover 1000000` filters out dead pairs that
would pollute the cross-sectional rank distribution. 1M USD/day is a
conservative floor — it doesn't change the rank ordering of liquid
names; it just drops tiny pairs whose noise would distort cross-sectional
Z-scores.

**Expected output:** ~7.5M rows × 25 cols per venue. ~30 min wall per
venue on the 5950X. **Build once; reuse for every IC test below.**

## Phase 5b — compute univariate IC for each feature × forward horizon

```bash
# For each (venue, forward-horizon): compute IC for all 20 features.
for venue in bybit_full_pit binance_full_pit; do
  for horizon in 1 3 7; do
    .venv/Scripts/python.exe -m liquidity_migration \
      --data-root ~/SHARED_DATA/$venue \
      signal-harness compute-ic \
      --panel ~/SHARED_DATA/$venue/feature_panel_2026-05-27.parquet \
      --target fwd_ret_${horizon}d \
      --sub-periods 3 \
      --features all \
      --output ~/SHARED_DATA/$venue/ic_report_fwd${horizon}d_2026-05-27.json
  done
done
```

**Output:** 6 JSON IC reports (2 venues × 3 horizons), each listing
mean_ic / t_stat / sub_period_ics / sub_period_sign_consistent per
feature. ~5 min wall total (computation is fast once the panel is
built).

## Decision rule for "feature survives to Phase 6"

A feature SURVIVES iff **ALL** of (per the parent plan, no loosening):

- `|mean_ic| ≥ 0.03` on **both** venues (at the chosen target horizon), AND
- sub-period sign-consistent across **all three** sub-periods on **both**
  venues, AND
- `|IC t-stat| ≥ 3` on **both** venues.

The conjunction roughly Bonferroni-corrects the 20-feature multiple-
testing exposure for the ~3 features that the plan expects to survive
under H6's expectation.

### FDR ceiling

Max **5 features** may survive to Phase 6. If more than 5 features pass
all three criteria on both venues, take the top 5 by combined-venue
mean |IC|; the rest are CLOSED-REJECTED (not a "menu for later").

### Target horizon selection

The primary target is `fwd_ret_3d` (matches the existing strategy's
hold-day default). IC reports for `fwd_ret_1d` and `fwd_ret_7d` are
informational only — useful for context but not used in the survival
decision unless explicitly amended.

## Estimated cost

- Panel build: ~30 min × 2 venues = **~60 min wall** (one-off; cached
  after; Phase 6 reuses the same panels).
- IC computation: ~5 min total across 6 reports.
- **Total: ~65 min wall** if both venues built fresh; **~5 min wall** if
  panels already cached.

## Pre-commitments

1. **No threshold loosening.** A feature with |mean_ic| = 0.025 on one
   venue and 0.04 on the other does NOT survive — both venues must
   clear 0.03. A feature with t-stat = 2.8 on one venue does NOT
   survive — both must clear 3.
2. **No off-menu features.** The 20-feature catalogue is committed;
   adding feature 21 mid-run requires a plan amendment + dated pre-reg.
   New features are NOT added to chase a near-miss.
3. **Panel-build parameters are committed.** Window, horizons,
   universe-min-turnover floor are all locked. Re-building with a
   different floor or horizon set creates a NEW panel artifact and
   requires a new dated pre-reg.
4. **IC sign-consistency is binary.** A feature whose mean IC is
   +0.04 on Bybit and -0.04 on Binance is sign-FLIPPED and rejected;
   the magnitudes don't compensate.

## Forward pointer

- **≥3 surviving features → Phase 6** (combined-signal portfolio) is
  triggered. Phase 6 then produces its own candidates that go to Phase 7.
- **<3 surviving features → Phase 6 does NOT run.** Combined-signal
  portfolio requires a minimum diversification base; 2 features is too
  few. The signal-harness arm concludes "H6 falsified — no orthogonal
  edge in the PIT panel beyond what the event-driven strategy already
  captures". Phase 7 still runs for Phase 2-4 candidates.
- **All-zero IC → publish the null.** The current event-driven
  architecture is correct in spirit even if its specific filters are
  off; the alternative-architecture branch dies here.
