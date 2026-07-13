# The PIT membership gate

This is the operator + maintainer reference for the point-in-time (PIT) universe
membership gate — the thing that decides whether a backtest signal is allowed to
trade, and the thing that broke the backtest↔paper reconciliation on 2026-05-30.

TL;DR: the known signal-day off-by-one is fixed and the plumbing has coverage
checks. The manifest still has explicit provenance limits described below.
Validate PIT inside the exact research run whose claim depends on it. Runtime
account acceptance is a separate evidence problem; there is no combined
live-ledger/PIT command that proves both.

## What the gate is

A historical-universe backtest may trade only symbols supported by the declared
membership source at the decision time. The **archive trade manifest** contains
two different kinds of rows, distinguished by `source`:

1. Archive-observed rows: a public trade-archive file existed for that symbol/day.
2. V5-derived rows: a symbol is currently `Trading` and the builder fills missing
   days from its reported launch date through the build boundary.

The second kind closes current archive gaps and lag, but it is an inference. It
does not observe historical suspensions, every day of actual trading, or delisted
symbols absent from the archive. “Full PIT” in this repository therefore means
coverage under this manifest contract, not perfect knowledge of historical venue
status. Claims must retain that limitation under `docs/governance.md`.

- On disk: `{data_root}/archive_trade_manifest/date=YYYY-MM-DD/symbol=SYMBOL/part.parquet`
  (columns `symbol, date, url, source`).
- Built by: `python -m liquidity_migration --data-root <root> archive-manifest`
  (a full rebuild, `append=False`). It merges two sources: the
  `public.bybit.com/trading` archive scrape (deep history) **and** the Bybit v5
  `instruments-info` listing (currently-Trading perps), the latter synthesizing
  missing dates from reported launch through the build boundary and filling the
  archive's ~24h publishing lag.
- Consumed by: `liquidity_migration/volume_events_pit.py` for PIT membership
  and full-PIT universe validation, plus the active engines' membership attach
  paths (`long_native.py`, with shared frame helpers from `trade_lifecycle.py`);
  a failed gate still labels the run `pit_membership_fail`.

## The off-by-one (fixed 2026-05-30)

A daily-close signal is **stamped at 00:00 UTC of the day _after_ the bar** it
summarises (`volume_features` builds `ts_ms = day_start_ms + one period`). So the
signal at `2026-05-30 00:00` is the **2026-05-29 daily close**.

The bug: membership was keyed on the signal **stamp date** (`2026-05-30`) instead
of the signal's **trading day** (`2026-05-29`). Two consequences:

1. It asked the archive about the day *after* the decision — a mild look-ahead.
2. It inflated the publishing lag by a full day: a fresh signal could not
   PIT-validate until the *next* day's archive published. Extending the manifest
   to `2026-05-29` did **not** surface the `2026-05-30 00:00` HEMIUSDT signal,
   because the lookup wanted a `2026-05-30` row.

The fix lives in `liquidity_migration/volume_events_pit.py` and is exercised via
`tests/test_pit_coverage.py`. The kline archive membership set is built from the
**kline stamp date** (the `date=` partition name derived from each bar's `ts_ms`,
which for a 1h kline at 00:00 UTC of day *D* is exactly the trading day *D* — no
off-by-one at the kline plane). The bug above was confined to the
`volume_features` signal plane; the kline-plane gate was already correct. The
`_required_pit_date_symbols` helper additionally drops pre-listing and
post-delist phantom manifest entries (genuine empty trade-archive files) while
keeping genuine mid-history gaps flagged — survivorship-preserving scoping.

After the fix, a `2026-05-30 00:00` signal validates against the `2026-05-29`
manifest day — which Bybit publishes on `2026-05-30`. So a same-day reconcile
works as soon as today's manifest refresh runs. No residual extra lag.

## The ~1-day archive lag (structural, handled)

`public.bybit.com/trading` publishes day *D*'s CSV ~24h after close. The manifest
build's v5-listing supplement infers tail membership for currently-Trading
symbols up to the build day, so building with `--end <today+2>` covers the latest
day under the repository contract. It does not turn that inferred row into an
archive observation.
`download-data` refreshes klines/funding but **never** touches the manifest — that
asymmetry is the original trap. Two guards now exist:

- `download-data` prints a PIT coverage table after every run and a loud WARNING
  when the manifest lags the klines, plus `--refresh-manifest` to do both at once.
- `liquidity_migration.pit_coverage.coverage_status(root)` /
  `format_coverage(...)` is the cheap, reusable staleness check (it reads the
  `date=` partition names and the recent tail manifest `symbol` column only).
  It also warns when global max dates look current but individual symbols have
  latest signal-day klines without matching archive-manifest coverage.

## Membership modes

The active knob is a run-config field.

The gate is `require_full_pit_universe` (field default `True`): when enabled, the
run aborts unless every required archive-manifest `(trading-day, symbol)` within
the modeled lifespan is covered by klines. Individual runtime profiles can and
do override the field, so inspect the actual config and run label.

| mode | config | meaning | use for |
| --- | --- | --- | --- |
| strict | `require_full_pit_universe=True` | abort unless the manifest-defined universe is covered | historical-universe performance claims |
| partial diagnostic | `require_full_pit_universe=False` | skip the completeness abort; run on available klines | explicitly scoped diagnostics or current-universe claims |

`require_full_pit_universe=False` cannot support a historical-universe claim.
It may still support a narrower diagnostic when the missing population is not
part of the proposition. Report the run label and scope rather than treating the
flag as either harmless or universally useless.

Note (pit-data-1, 2026-06-14): the former per-trade `require_pit_membership`
flag was REMOVED — it was inert (read by no enforcement path) and advertised a
per-trade membership gate that never ran. PIT membership is enforced at the
universe level by the gate above; do not re-introduce a flag implying per-trade
gating without an actual enforcement path.

## Current workflow

Treat PIT membership as a research-data validity gate, not as an execution
reconciler.

1. Inspect the active command's help and the selected root's manifest/kline
   coverage.
2. Rebuild the manifest for that exact root and an explicit end-exclusive
   boundary when the manifest is missing or stale:

   ```bash
   python -m liquidity_migration --data-root ROOT archive-manifest \
     --start YYYY-MM-DD --end YYYY-MM-DD
   ```

3. Close any named kline gap with the appropriate current archive-download
   command, using its `--help` rather than copying a dated command line.
4. Re-run the exact backtest or registered experiment. Preserve its config,
   run label, warnings, root identity, and output artifact.

The former `scripts/reconcile.sh`, `scripts/reconcile.py`,
`scripts/reconcile_three_way.py`, and sleeve-local reconciliation CLI commands
were retired on 2026-07-13. They mixed PIT/model checks with demo and paper
compatibility projections that are not authoritative under the target-only
account-owner architecture. Their archived reports can describe the historical
run that produced them; they are not a current operational gate.

For runtime evidence, `scripts/ops.sh account-parity` compares non-empty
historical, paper, and demo account journals structurally. It does not inspect
PIT coverage and cannot prove common market-tape provenance, common strategy
scheduling, fresh venue rules, credentialed demo execution, or fill/P&L
agreement. The open runtime gates are listed in
`docs/account_execution_cutover.md`.

## When PIT membership fails

1. Read the emitted warning and coverage report; identify whether the missing
   object is manifest membership, kline data, or both.
2. Rebuild only with a boundary justified by the claim. The `--end` value is
   exclusive.
3. Confirm the manifest row's `source`. A V5-derived tail row is inferred
   current-listing coverage, not an archive observation.
4. Re-run the same research command. Do not switch to the current universe or
   disable `require_full_pit_universe` after seeing the result and then present
   it as the original historical-universe claim.
5. If the full population is not needed for a narrower diagnostic, label that
   diagnostic and its missing population explicitly under `docs/governance.md`.

## Design receipt

The trading-day membership convention (membership keys on
`date(ts_ms − 1ms)`, the BINDING decision this gate implements) is recorded in
the archived `pit-membership-trading-day-fix` receipt in git history.
