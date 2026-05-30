# Receipt: PIT membership keyed on the signal trading day (off-by-one fix)

**Date:** 2026-05-30
**Author:** claude (for owner)
**Stage:** accepted
**Type:** PIT correctness fix (not a parameter change / not a tuning knob)

## What's changing

`volume_events_features._attach_event_archive_membership` now keys archive
`tradable_membership_flag` on the signal's **trading day** = `date(ts_ms - 1 ms)`
instead of the signal **stamp date** = `date(ts_ms)`.

A daily-close signal is stamped at 00:00 UTC of the day *after* the bar it
summarises (`volume_features` builds `ts_ms = day_start_ms + one period`). The
stamp-date key therefore asked the archive whether the symbol traded on the day
*after* the decision — a (mild) look-ahead — and required the *next* day's
archive to publish before a fresh signal could validate. The trading-day key
asks the correct question ("was the symbol a tradable member on the day whose
close produced the signal?") and removes a full, spurious day of publishing lag.

The stamp-day `date` column is preserved for the `symbol_age_days` / `pit_age_days`
features, so no feature values move; only the membership JOIN key changes.

## Why this is a correctness fix, not a parameter

Per `docs/backtesting_errors_we_never_repeat.md` (rules 1, 2, 12, 13) and
`AGENTS.md`, PIT / no-look-ahead is a correctness gate, not a tunable. This fix
makes the gate match the decision timeline; it is not selected to improve any
metric and was not chosen on the validation window.

## Predicted numeric impact (bounded, pre-stated)

Mid-history, a continuously-listed symbol trades on both the trading day D and the
stamp day D+1, so both keys return the same membership — **no change**. The trade
set can only differ at:

1. **The recent tail** (intended): a just-closed signal whose trading-day archive
   exists but whose stamp-day archive has not published. These now correctly pass
   (this is the HEMIUSDT 2026-05-30 case that broke the reconcile).
2. **Delisting boundaries**: a symbol's final signal (stamped the day after its
   last trade) was keyed on a day with no archive and rejected; it now keys on the
   last trading day and passes. Such trades almost never fill (no post-signal
   klines to execute the +1h-delayed entry), so the realised forward-window PnL is
   expected to be unchanged.

**Acceptance bar:** the filled-trade set on `~/SHARED_DATA/bybit_full_pit` over the
full history is numerically unchanged except for the recent tail and
zero-fill delisting boundaries (NaN positions match elsewhere; equity curve
within float tolerance). Membership-flip candidates are bounded and enumerated by
the recon analysis.

## Validation

- Regression lock: `tests/test_pit_membership_trading_day.py` (trading-day keying,
  stale-tail rejection, mixed batch).
- Updated `test_attach_event_archive_membership_flags_symbol_dates` to the
  corrected trading-day expectation.
- Coverage / staleness diagnostics: `tests/test_pit_coverage.py`.
- Before/after `volume-events` on `bybit_full_pit`: filled-trade delta confined to
  the predicted boundary/tail set. (Numbers attached on run.)
- End-to-end: `bash scripts/reconcile.sh` produces a clean backtest↔paper pairing
  for the forward window once the manifest covers the latest trading day.

## Not changed

No strategy parameter default, no cost model, no execution logic, no universe
thresholds. The strict membership path is byte-identical in behaviour except for
the corrected date key.
