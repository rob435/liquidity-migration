# The PIT membership gate (and how to never break the reconcile again)

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
- Consumed by: `volume_events_features._attach_event_archive_membership`, which
  sets `tradable_membership_flag`. `volume_events_filters` drops any event whose
  flag is `False`; the run is then labelled `pit_membership_fail`.

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
  `date=` partition names only). `scripts/reconcile.sh` calls it before the
  backtest and refuses a stale strict run.

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
per-trade membership gate. `scripts/reconcile.sh` uses it so a bounded forward
window doesn't trip the whole-history universe check; per-trade membership stays
strict.

## The one-command workflow

`scripts/reconcile.sh` (driver: `scripts/reconcile.py`) does, in order:

1. **pull** — rsync the demo + paper `event_demo_trades` ledgers from the VPS
   (`root@5.223.42.109:/opt/liquidity-migration/data/bybit-{demo,paper}-event`),
   read-only.
2. **manifest** — refresh `archive_trade_manifest` to `today+2` on the research
   root (so the tail is covered).
3. **coverage** — print the PIT coverage table; abort the strict backtest if the
   manifest can't validate the latest signal day (override: `--diagnostic` / `--force`).
4. **backtest** — run the promoted `volume-events` profile over the forward window
   → `volume_event_best_trades.csv`.
5. **reconcile** — `reconcile-all` (backtest↔paper↔demo, `+--with-bybit` for the
   venue leg).
6. **summary** — print the headline (paired / backtest-only / paper-only / slip).

Common flags: `--dry-run` (print commands only), `--no-pull`, `--no-manifest`,
`--no-backtest`, `--diagnostic`, `--with-bybit`, `--force`. The matching skill is
`.claude/skills/pit-reconcile`.

## When a reconcile shows `paper-only` / `pit_membership_fail`

1. Run `bash scripts/reconcile.sh` (it refreshes the manifest first). If the
   coverage table says ✅, the strict run is valid.
2. If a single very-recent signal is still `paper-only`, the trading-day archive
   has not published yet — wait for the next day, or use `--diagnostic` for a
   labelled current-universe check.
3. `paper↔demo` measures execution slippage and is independent of all of the
   above; if it is clean the live executor matches the model.
