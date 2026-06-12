# The PIT membership gate (and how to never break the reconcile again)

> **2026-06-11:** the daily SHORT sleeve was ERASED from the system by operator
> order — the short engine, its CLI reconcile commands, and the short ledgers'
> live writers no longer exist. The PIT gate itself survives (it is generic);
> everything below that mentions the short sleeve is HISTORICAL. The current
> reconcile default is the LONG sleeve (`--sleeves long`), with continuous as
> opt-in diagnostics.

This is the operator + maintainer reference for the point-in-time (PIT) universe
membership gate — the thing that decides whether a backtest signal is allowed to
trade, and the thing that broke the backtest↔paper reconciliation on 2026-05-30.

TL;DR: the gate is correct now (the off-by-one is fixed), and the plumbing is
self-checking. For a routine reconcile just run:

```bash
bash scripts/reconcile.sh
```

## What the gate is

A backtest may only trade a symbol that was genuinely a tradable member of the
venue at the decision time (no survivorship / look-ahead — see
`docs/backtesting_errors_we_never_repeat.md`, rules 1 and 12). Membership comes
from the **archive trade manifest**: for each `(symbol, date)` the symbol had
public trades on that UTC calendar day.

- On disk: `{data_root}/archive_trade_manifest/date=YYYY-MM-DD/symbol=SYMBOL/part.parquet`
  (columns `symbol, date, url, source`).
- Built by: `python -m liquidity_migration --data-root <root> archive-manifest`
  (a full rebuild, `append=False`). It merges two sources: the
  `public.bybit.com/trading` archive scrape (deep history) **and** the Bybit v5
  `instruments-info` listing (currently-Trading perps), the latter filling both
  the archive's symbol-coverage gaps and its ~24h publishing lag.
- Consumed by (historical): `volume_events_features._attach_event_archive_membership`
  / `volume_events_filters` — both erased with the SHORT sleeve 2026-06-11. The
  gate survives standalone as `liquidity_migration/volume_events_pit.py` (PIT
  membership + full-PIT universe validation), consumed by the surviving engines'
  membership attach (`long_native.py`, with shared frame helpers from
  `trade_lifecycle.py`); a failed gate still labels the run `pit_membership_fail`.

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

The fix (`_attach_event_archive_membership`): membership is keyed on the trading
day = `date of (ts_ms - 1 ms)`. The stamp-day `date` column is preserved as-is for
the age features, so nothing else moves. Numerically this only changes
listing/delisting-boundary and recent-tail rows; the regression lock is
`tests/test_pit_membership_trading_day.py`.

After the fix, a `2026-05-30 00:00` signal validates against the `2026-05-29`
manifest day — which Bybit publishes on `2026-05-30`. So a same-day reconcile
works as soon as today's manifest refresh runs. No residual extra lag.

## The ~1-day archive lag (structural, handled)

`public.bybit.com/trading` publishes day *D*'s CSV ~24h after close. The manifest
build's v5-listing supplement fills the tail for currently-Trading symbols up to
the build day, so building with `--end <today+2>` covers the latest trading day.
`download-data` refreshes klines/funding but **never** touches the manifest — that
asymmetry is the original trap. Two guards now exist:

- `download-data` prints a PIT coverage table after every run and a loud WARNING
  when the manifest lags the klines, plus `--refresh-manifest` to do both at once.
- `liquidity_migration.pit_coverage.coverage_status(root)` /
  `format_coverage(...)` is the cheap, reusable staleness check (it reads the
  `date=` partition names only).

## Membership modes

| mode | flag | meaning | use for |
| --- | --- | --- | --- |
| strict (default) | *(none)* | archive PIT membership on the trading day | all evidence / promotion |
| current-universe | `--pit-membership current-universe` | drop the per-trade PIT gate; trade whatever the manifest's current listing covers | a same-day diagnostic / reconcile before the archive publishes |

`--pit-membership current-universe` sets `require_pit_membership=False` and the run
is labelled `biased_benchmark` / `current_universe_biased` — **never** promotion
evidence (it is exactly the survivorship surface the methodology doc forbids for
real decisions). It exists only so a same-day reconcile can include a signal whose
trading-day archive has not published yet.

Note: `--allow-partial-pit` is a *different* knob — it relaxes only the
universe-*completeness* abort (every manifest symbol must have klines), not the
per-trade membership gate. Per-trade membership stays strict either way.

## The one-command workflow

`scripts/reconcile.sh` (driver: `scripts/reconcile.py`) reconciles the promoted
LONG sleeve by default (`--sleeves long`). Continuous is opt-in diagnostics only
via `--sleeves continuous`. In order:

1. **pull** — rsync every selected sleeve's demo + paper ledgers from the VPS
   (long `long_native_{demo,paper}_*`; when explicitly selected, continuous
   `continuous_fade_{demo,paper}_*` + the continuous rmom panel + WS kline
   store), read-only. Skip with `--no-pull`.
2. **rmom** — when continuous is selected, auto-recompute `residual_momentum.parquet`
   (the continuous gate) on the research root. Skip with `--no-rmom`.
3. **reconcile** — per sleeve: LONG `reconcile-long-paper-demo` (paper ↔ demo),
   and, only when selected, CONTINUOUS `continuous-forward-readiness --paper-only`
   + a signal-consistency replay. (The SHORT `reconcile-all` path was erased with
   the sleeve, 2026-06-11.)
4. **summary** — one unified headline across selected sleeves.

Flags: `--sleeves long,continuous`, `--dry-run`, `--no-pull`, `--no-rmom`,
`--bybit-root PATH`, `--config PATH`, `--vps HOST`. The matching skills are
`.claude/skills/pit-reconcile` / `.codex/skills/pit-reconcile`.

The old manifest-refresh / kline-fill / coverage / backtest provisioning steps
were removed with the erased SHORT sleeve's backtest leg. Refreshing the
manifest is the manual command above
(`python -m liquidity_migration --data-root <root> archive-manifest`).

## When a reconcile shows `paper-only` / `pit_membership_fail`

1. Refresh the manifest manually
   (`python -m liquidity_migration --data-root <root> archive-manifest`), then
   re-run `bash scripts/reconcile.sh`.
2. If a single very-recent signal is still `paper-only`, the trading-day archive
   has not published yet — wait for the next day (a current-universe diagnostic
   backtest is possible but is biased and must be labelled as such).
3. `paper↔demo` measures execution slippage and is independent of all of the
   above; if it is clean the live executor matches the model.
