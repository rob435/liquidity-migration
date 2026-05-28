# Phase 5 — signal-research harness + univariate IC (VERDICT)

**Date:** 2026-05-28 (panels built 2026-05-28 01:34, IC re-run 02:00 with NaN + Binance-autodetect fixes)
**Stage:** run-complete, **6 features survive at fwd_ret_3d** → **Phase 6 TRIGGERED**
**Pre-reg:** [docs/preregistration/round1/phase5-signal-harness-ic.md](phase5-signal-harness-ic.md)
**Phase label per parent plan Appendix B:** `exploratory` — IC report is
informational regardless of outcome; survivors are candidates for Phase 6 only.

## Headline

**6 features cleared the Phase 5 survival rule at fwd_ret_3d** on both
venues with sign-agreement. After applying the FDR ceiling (max 5
forward to Phase 6), the **top 5 by combined-venue mean |IC|** survive.
The 6th (`turnover_delta_30d`) is closed-rejected.

All 5 surviving features have **NEGATIVE** IC — high feature value
predicts low forward return. For a SHORT portfolio, signal-rank-top
goes short.

## Survivors (post-FDR-ceiling, forward to Phase 6)

| Rank | Feature | by_ic | bn_ic | avg \|IC\| | Direction (predict short) |
|---|---|--:|--:|--:|---|
| 1 | `vol_of_vol_30d`     | -0.097 | -0.077 | **0.087** | high vol-of-vol → short |
| 2 | `realized_vol_7d`    | -0.088 | -0.074 | **0.081** | high 7d realized vol → short |
| 3 | `dist_from_30d_low`  | -0.074 | -0.068 | **0.071** | far above 30d low → short |
| 4 | `xs_rank_ret_7d`     | -0.042 | -0.043 | **0.043** | top 7d returns → short (mean rev) |
| 5 | `xs_rank_ret_3d`     | -0.040 | -0.038 | **0.039** | top 3d returns → short (mean rev) |

### FDR-closed (closed-rejected)

| Feature | by_ic | bn_ic | avg \|IC\| | Closure reason |
|---|--:|--:|--:|---|
| `turnover_delta_30d` | -0.042 | -0.031 | 0.036 | 6th by avg \|IC\|; FDR ceiling = 5 |

The closed feature is NOT a "menu for later" — it cannot be resurrected
without a new dated pre-reg. The pre-reg's FDR ceiling is binding.

## Full per-feature IC table (fwd_ret_3d, primary survival target)

| Feature | by_ic | by_t | by_sc | bn_ic | bn_t | bn_sc | Survives | Notes |
|---|--:|--:|---|--:|--:|---|---|---|
| close_location_1d        | +0.006 | +1.4  | T | +0.006 | +1.2  | F |   | low t-stat both venues |
| dist_from_30d_high       | +0.033 | +6.6  | T | +0.015 | +3.4  | F |   | Binance sign-inconsistent |
| **dist_from_30d_low**    | -0.074 | -15.8 | T | -0.068 | -19.8 | T | YES |  |
| funding_rate_delta_7d    | +0.008 | +2.0  | T | -0.001 | -0.4  | F |   | Binance ~0 |
| funding_rate_z           | -0.014 | -3.6  | F | -0.010 | -2.5  | F |   | sign-inconsistent both |
| liquidity_rank           | +0.023 | +5.2  | T | +0.060 | +11.6 | T |   | \|IC\| < 0.03 on Bybit |
| liquidity_rank_delta_30d | +0.007 | +1.9  | T | +0.017 | +7.9  | T |   | Bybit \|t\| < 3 |
| liquidity_rank_delta_7d  | +0.003 | +0.7  | F | +0.014 | +6.8  | T |   | Bybit fails |
| oi_delta_7d              | -0.028 | -6.7  | T | n/a    | n/a   | F |   | Binance OI data <33 days |
| oi_to_adv                | -0.020 | -4.5  | T | n/a    | n/a   | F |   | Binance OI data <33 days |
| premium_index_z          | -0.029 | -7.7  | T | -0.007 | -1.7  | F |   | Binance below thresholds |
| range_extension_30d      | -0.027 | -5.9  | T | -0.029 | -9.8  | T |   | \|IC\| < 0.03 on Bybit |
| **realized_vol_7d**      | -0.088 | -17.1 | T | -0.074 | -17.5 | T | YES |  |
| turnover_delta_30d       | -0.042 | -9.9  | T | -0.031 | -11.4 | T | (FDR-closed) | rank 6 by \|IC\| |
| turnover_delta_7d        | -0.017 | -3.9  | T | -0.012 | -4.6  | T |   | \|IC\| < 0.03 both |
| **vol_of_vol_30d**       | -0.097 | -18.7 | T | -0.077 | -17.9 | T | YES |  |
| xs_rank_ret_1d           | -0.027 | -5.8  | T | -0.024 | -7.0  | T |   | \|IC\| < 0.03 both |
| xs_rank_ret_30d          | -0.024 | -5.0  | T | -0.035 | -8.8  | T |   | \|IC\| < 0.03 on Bybit |
| **xs_rank_ret_3d**       | -0.040 | -8.5  | T | -0.038 | -10.8 | T | YES |  |
| **xs_rank_ret_7d**       | -0.042 | -8.8  | T | -0.044 | -11.8 | T | YES |  |

T/F = sub_period_sign_consistent. \|IC\| threshold = 0.03; \|t\| threshold = 3.

## OI data-coverage caveat

`binance_usdm_open_interest` data only spans **2026-04-25 → 2026-05-27**
(33 days) on the local Binance root. The signal-harness autodetect found
the right dataset name (Binance prefix), but the data simply isn't
there for the 2021-01 → 2026-04 window. Both OI-derived features
(`oi_delta_7d`, `oi_to_adv`) therefore have nan IC on Binance and
fail the cross-venue survival rule. Bybit-only, both features show
significant -0.020 to -0.028 IC, but the Phase 5 rule (and the
parent plan's cross-venue requirement) properly excludes them.

If OI signal turns out to matter, the next step is to backfill Binance
OI history before any retest — that's a data-engineering task outside
Phase 5's scope.

## Bugs surfaced and fixed during this phase

1. **`compute_univariate_ic` NaN propagation** (storage.py + signal_harness):
   when `pl.corr` returns NaN (zero-variance days), `sum(ics)/n_days`
   propagated NaN to mean_ic. Affected features with frequent zero-
   variance days (funding rates have ~1 hourly observation per day,
   sometimes constant). Fixed: filter NaN explicitly before summing.
   Regression test added.

2. **Binance dataset naming mismatch**: `build_feature_panel` defaulted
   to Bybit dataset names (`funding`, `open_interest`, `premium_index_1h`),
   which don't exist on Binance (whose names are `binance_usdm_*`).
   Silently produced 100%-null derived features. Fixed: autodetect
   convention by sniffing which subdirs exist. Regression test added.

## Phase 6 trigger condition met

≥3 survivors required → 5 survivors after FDR ceiling. **Phase 6
(combined-signal portfolio) is TRIGGERED.** Pre-reg at
`docs/preregistration/round1/phase6-combined-signal-portfolio.md`.

Surviving feature list pinned for Phase 6: **vol_of_vol_30d,
realized_vol_7d, dist_from_30d_low, xs_rank_ret_7d, xs_rank_ret_3d**.
IC weights for the `ic_weighted` cell variant (Bybit IC values used,
sign-agreement was already verified):

```
vol_of_vol_30d=-0.0965
realized_vol_7d=-0.0880
dist_from_30d_low=-0.0741
xs_rank_ret_7d=-0.0423
xs_rank_ret_3d=-0.0401
```

## Secondary horizons (informational only)

| Horizon | Bybit survivors | Binance survivors | Notes |
|---|---:|---:|---|
| fwd_ret_1d | 7 | 10 | shorter-horizon mean rev appears strong |
| fwd_ret_3d | 7 | 8 | primary survival target |
| fwd_ret_7d | 6 | 6 | longer horizon signal weaker |

Phase 6 will exercise fwd_ret_1d/3d/7d in the `P6_horizon_sweep`
calibration, so the secondary horizons get tested without separate
verdict commitments.

## Pre-commitment compliance

- ✅ |IC| ≥ 0.03 threshold not loosened
- ✅ |t| ≥ 3 threshold not loosened
- ✅ Sub-period sign-consistency required on both venues
- ✅ Sign-agreement across venues required
- ✅ FDR ceiling (max 5 features) enforced
- ✅ Closed feature (`turnover_delta_30d`) not resurrected
- ✅ Primary target = fwd_ret_3d as pre-registered (secondary horizons
  informational only)

## Artifacts

- Pre-reg: `docs/preregistration/round1/phase5-signal-harness-ic.md`
- Phase 6 pre-reg: `docs/preregistration/round1/phase6-combined-signal-portfolio.md`
- Feature panels:
  - `~/SHARED_DATA/bybit_full_pit/feature_panel_2026-05-27.parquet` (371,560 rows × 28 cols)
  - `~/SHARED_DATA/binance_full_pit/feature_panel_2026-05-27.parquet` (478,017 rows × 28 cols)
- IC reports:
  - `~/SHARED_DATA/{bybit,binance}_full_pit/ic_report_fwd{1,3,7}d_2026-05-27.json`

## Forward pointer

**Next: Phase 6 (combined-signal portfolio).** 21 cell configurations
× 2 venues = 42 runs. Manifesto candidate criteria apply. Max 3
candidates may forward to Phase 7. Orchestrator to be drafted at
dispatch with the survivor list pinned above.

**Parallel: Phase 2 (rank-direction grid).** 66 runs. Independent of
Phase 5 outcome. Can dispatch first since it's the bigger compute
(~2-3h vs Phase 6's ~100 min).
