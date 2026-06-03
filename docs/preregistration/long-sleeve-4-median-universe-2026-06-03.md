# Pre-registration — long-sleeve-4: live long universe by 90d-median turnover

- **Date:** 2026-06-03
- **Status:** DRAFT — built on branch `audit/fixes-2026-06-03`, NOT deployed. Blocked on the
  operator steps below (RSS cap + forward-demo re-validation) before any deploy.
- **Run class:** PIT/faithfulness correction (NOT an alpha hypothesis). Selection-affecting,
  so it is pre-registered per AGENTS.md.

## What changes
The live long sleeve previously ranked its universe by CURRENT 24h turnover and truncated to
50 names **before** the kline fetch (`_build_long_universe`), so only 50 names reached
`build_long_features`; the backtest's 90d-MEDIAN-turnover gate (`universe_rank`,
`min_samples=90`) was neutered (rank 1–50, cutoff trivially true) and today-pump names were
admitted where the median gate could not reject them.

The change:
- `_build_long_universe` fetches a **superset** (`universe_superset_size=120`, was top-50)
  by 24h turnover, so the median gate has real names to rank.
- `lookback_days` 90→100 and `ws_klines_lookback_days` 90→100 so ≥90 daily bars survive the
  trims and `turnover_median_90d` populates; daemon `_LONG_KLINE_UNIVERSE_SIZE` 50→120 so the
  WS store covers the superset.
- New `_apply_median_universe_selection` re-selects `in_universe` on the latest bar to the
  top-`universe_size` by 90d-median (keyed on `strategy.universe_size` — the SAME value
  `build_long_features` used), with a 24h-turnover backfill **only** when fewer than
  `universe_size` names have a finite median (cold start), surfaced as `universe_fallback_24h`.
- **No signal/threshold change.** In steady state (every member has a finite median, which
  requires ≥90 bars ⟹ age ≥ 90 ≥ `min_listing_history_days=30`, so the age filter is subsumed)
  the helper is a **no-op byte-match** to the universe `build_long_features` already computed —
  pinned by `test_median_universe_selection_steady_state_is_byte_match_noop`.

## Hypothesis
Live `in_universe` matches the backtest's median-ranked `in_universe` once warm; today-pump
mis-admissions disappear. This is a correctness fix, not new alpha.

## Predicted / Falsifier
- **Predicted:** after warm (≥90 daily bars accrued, `universe_fallback_24h == 0` for ≥2 days),
  live entry overlap with backtest `in_universe` ≈ 100%.
- **Falsifier:** overlap < 90% after warm ⇒ a residual keying bug; raise `lookback_days` or
  re-examine the helper.

## Roots / scope
Forward demo + paper only (no internal pre-2023 OOS root exists). Decision is the forward-demo
arbiter, not an in-sample re-run (the 16 GB box cannot run the full backtest).

## Decision rule
Accept iff, after warm, `reconcile-long` shows live `in_universe` matching backtest `in_universe`
within the falsifier bound; else raise `lookback_days` and re-warm. Do **not** treat cold-start
cycles (`universe_fallback_24h > 0`) as evidence.

## OPERATOR STEPS / DEPLOY BLOCKERS (must clear before deploy)
1. **RSS cap.** The 120-name × 100-day WS store is ~66 MB; the systemd memory cap was sized for
   the top-50/top-10 era. **Bump the long-daemon `MemoryMax` (and watch RSS on first deploy)
   before pushing** — otherwise the cap trips and the daemon restart-loops on a live push. (I did
   NOT edit the systemd unit; that file is deploy-gated/operator-owned.) Fallback if RSS is tight:
   set `universe_superset_size=90`.
2. **Cold start is expected.** After deploy the WS store re-bootstraps (100 days, 120 symbols);
   early cycles show `universe_fallback_24h > 0` and 24h-ranked selection until ≥90 daily bars
   accrue. This is NOT a blackout — communicate it so it is not read as a regression.
3. **Re-validate** per the decision rule before accepting the parameter as alpha-neutral.
