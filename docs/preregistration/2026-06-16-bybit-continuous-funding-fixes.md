# Fix receipt: get bybit continuous working + funding/resolver/panel root causes

**Date:** 2026-06-16
**Author:** operator-directed (`/goal`: "get the bybit working, fix all the root
causes so we never encounter such difficulties again").
**Label:** correctness fix (methodology-gate change for the funding snapshot guard).
Not a parameter change to a working dataset; no alpha claim. Evidence below.

## Symptom

`bash scripts/equity_curves.sh --sleeves continuous --root ~/SHARED_DATA/bybit_full_pit`
crashed; the binance continuous + long curves silently ran funding-uncosted. Three
independent root causes, each fixed at the source (no workarounds left in place).

## Root cause 1 — funding snapshot guard false-positive (the bybit blocker)

`continuous_events._assert_funding_one_per_settlement` compared each symbol's observed
stamp cadence to the **stored `funding_interval_min`**, which venues leave at a stale 8h
default. Many Bybit alts genuinely settle funding every 1h/2h (BERA: main-root funding
matches a fresh funding-history download byte-for-byte at 120-min cadence, 372/372
stamps+rates), so the guard misread correct sub-8h funding as a snapshot scrape and hard-
failed the run. 89 symbols flagged; ALL genuine (modal rate-change gap == modal stamp gap
for every one checked — a real hourly snapshot of an 8h symbol would show change_gap=480).

**Fix:** detection is now **data-intrinsic** (`trade_lifecycle.funding_cadence_stats` /
`derive_funding_interval_min`): the true settlement interval is the modal gap between
funding-RATE-change events (the rate is constant within a settlement and changes at it),
never the stored `funding_interval_min`, never a live exchangeInfo (not a PIT source). The
engine passes the derived `interval_by_symbol` to `_funding_lookup`, which already supports
collapsing genuine intra-interval snapshot rows. The guard now flags ONLY true over-
sampling (`change_gap >= 2*stamp_gap`, clean venue ratio, >=3 changes) that is left
uncorrected — so genuine sub-8h alts are charged every settlement and a real snapshot is
collapsed (or, uncorrected, still raises). Applied to both the continuous and long paths.

## Root cause 2 — dataset resolver misclassified binance-canonical roots

`storage.resolve_dataset_name` suppressed the `funding -> binance_usdm_funding` fallback
whenever a canonical `klines_1h/` dir was present (the "Bybit-native marker"). But
`binance_full_pit` stores klines under the canonical name, so it was misread as Bybit and
funding silently resolved to an absent `funding/` -> `funding_mode=missing` on a fully
populated `binance_usdm_funding`. (continuous_events used the OTHER detector,
`_autodetect_dataset_names`, which was already correct — a dual-logic inconsistency.)

**Fix:** a present `binance_usdm_*` variant is authoritative (it only ever exists on a
Binance root), so it is preferred even under canonical kline naming, matching
`_autodetect_dataset_names`. pit-data-6 safety is preserved by canonical precedence: a real
Bybit root always carries its own `funding/`, which still wins over any binance PROXY
dataset on the same root. Dead `_BYBIT_NATIVE_MARKERS` / `_root_has_bybit_native_marker`
removed. Verified the binance funding now resolves with NO symlink workaround.

## Root cause 3 — hard-coded `feature_panel_<date>.parquet` dependency

`continuous_deployed_equity_refresh.load_extended_panel` hard-read
`feature_panel_2026-05-27.parquet`, which existed on NEITHER root -> FileNotFoundError on
assembly. That parquet was only ever a cached daily rollup of the klines_1h partitions.

**Fix:** when the frozen file is absent, build the daily panel from klines on demand
(`_daily_panel_from_klines`, same close=last/turnover=sum rollup) and cache it back so later
runs take the fast path. No hard-coded-filename crash on any root/box.

## Evidence (forced fresh runs, no cached reports)

- **CONTINUOUS bybit — NOW WORKS, fully costed.** All 4 components `funding=modeled`;
  panel `source=klines`. Window 2023-04-01→2026-06-02 (3.17y): ret +87.6%, ann +21.9%,
  maxDD -5.4%, Sharpe 2.53, MAR 5.07 (4x: +993%, MAR 14.85). Was: hard crash at the guard.
- **CONTINUOUS binance — fully costed via code fix** (no symlink/panel workaround):
  see the run log. Was: `funding=partial`/missing (resolver bug).
- **LONG bybit unchanged:** `full_pit_universe`, +32.9% / DD -3.5% / Sharpe 1.89 / 188
  trades — clean regression (the long path's majors are 8h-settling, unaffected by the
  interval derivation).
- **Tests:** ruff clean; full suite 1969 passed (+4: derive/cadence/collapse, guard
  no-false-positive on genuine sub-8h, guard raises-then-corrects on a realistic snapshot,
  resolver binance-variant + bybit-canonical-precedence).

## Methodology note

Banked lesson (do not re-introduce): the stored per-row `funding_interval_min` is an
unreliable 8h default and must NEVER be used to detect snapshot over-sampling or to bucket
settlements — derive the true cadence from the realized rate-change structure. A guard that
trusts it false-positives genuine sub-8h funding.
