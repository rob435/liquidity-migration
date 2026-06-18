# The PIT membership gate (and how to never break the reconcile again)

> **2026-06-11:** the daily SHORT sleeve was ERASED from the system by operator
> order — the short engine, its CLI reconcile commands, and the short ledgers'
> live writers no longer exist. The PIT gate itself survives (it is generic);
> everything below that mentions the short sleeve is HISTORICAL. The full
> reconcile default is BOTH surviving sleeves; the `--quick` execution-only path
> defaults to LONG unless `--sleeves long,continuous` is supplied.

This is the operator + maintainer reference for the point-in-time (PIT) universe
membership gate — the thing that decides whether a backtest signal is allowed to
trade, and the thing that broke the backtest↔paper reconciliation on 2026-05-30.

TL;DR: the gate is correct now (the off-by-one is fixed), and the plumbing is
self-checking. There is ONE command for the whole reconciliation — it refreshes
PIT data, pulls the live ledgers, runs each sleeve's backtest over the forward
window, and reconciles the model against demo + paper, all in a single run:

```bash
bash scripts/reconcile.sh              # full demo<->backtest<->paper, both sleeves
bash scripts/reconcile.sh --quick      # fast paper<->demo execution check only
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
listing/delisting-boundary and recent-tail rows. (The original regression lock,
`tests/test_pit_membership_trading_day.py`, was deleted with the short engine in
e03e9ab; the surviving trading-day keying lives in
`liquidity_migration/volume_events_pit.py` and is exercised via
`tests/test_pit_coverage.py`.)

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

(The `--pit-membership` / `--allow-partial-pit` CLI flags were erased with the
`volume-events` subcommand in e03e9ab; the surviving knob is a run-config field.)

The surviving gate is `require_full_pit_universe` (default `True`): the run aborts
unless every archive-manifest `(trading-day, symbol)` within each symbol's traded
lifespan is covered by klines — the no-survivorship / no-look-ahead
universe-completeness check enforced in `volume_events_pit.py`.

| mode | config | meaning | use for |
| --- | --- | --- | --- |
| strict (default) | `require_full_pit_universe=True` | abort unless the full PIT universe is covered | all evidence / promotion |
| biased diagnostic | `require_full_pit_universe=False` | skip the completeness abort; run on whatever klines exist | a clearly-labelled same-day diagnostic — **never** promotion evidence |

`require_full_pit_universe=False` runs are labelled `biased_benchmark` /
`current_universe_biased` — never promotion evidence (it is exactly the
survivorship surface the methodology doc forbids for real decisions).

Note (pit-data-1, 2026-06-14): the former per-trade `require_pit_membership`
flag was REMOVED — it was inert (read by no enforcement path) and advertised a
per-trade membership gate that never ran. PIT membership is enforced at the
universe level by the gate above; do not re-introduce a flag implying per-trade
gating without an actual enforcement path.

## The one-command workflow

`scripts/reconcile.sh` is the single front door. By **default** it runs the full
demo ↔ backtest ↔ paper three-way for BOTH sleeves (see the next section). The
`--quick` flag routes to the FAST two-way (paper ↔ demo execution only, driver
`scripts/reconcile.py`, no PIT download / no backtest) — use it for a quick
"is the live executor matching the model?" pass once the root is already current:

```bash
bash scripts/reconcile.sh --quick              # LONG paper<->demo (quick default)
bash scripts/reconcile.sh --quick --sleeves long,continuous
```

The `--quick` path, in order:

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

`--quick` flags: `--sleeves long,continuous`, `--dry-run`, `--no-pull`,
`--no-rmom`, `--bybit-root PATH`, `--config PATH`, `--vps HOST`. The matching
skills are `.claude/skills/pit-reconcile` / `.codex/skills/pit-reconcile`.

Refreshing the manifest on its own is the manual command above
(`python -m liquidity_migration --data-root <root> archive-manifest`).

## The three-way (demo ↔ backtest ↔ paper) workflow — rebuilt 2026-06-17

This is the **default** of `scripts/reconcile.sh` (the whole reconciliation in
one run). The backtest leg that the daily-SHORT erasure removed was rebuilt
generically as `scripts/reconcile_three_way.py`, covering BOTH surviving sleeves:

```bash
bash scripts/reconcile.sh                    # long + continuous, full pipeline (default)
bash scripts/reconcile.sh --no-data-refresh  # skip the PIT download
bash scripts/reconcile.sh --sleeves long     # one sleeve
# (scripts/reconcile_three_way.sh is a back-compat alias for the same thing.)
```

It (0) refreshes PIT data on the research root over a **gap-only** tail window
(archive-manifest `--allow-degraded` + 1h klines + filter; the manifest stage
unions with the persisted manifest so a narrow rebuild augments, never wipes,
coverage), (1) pulls the live ledgers, then per sleeve:

The refresh **short-circuits** any dataset already current and runs each sub-stage
under a wall-clock timeout, so a current root does ~no work and nothing can hang
(the original tool stalled for hours re-checking already-present partitions over a
blind wide window). **Funding is OFF by default** (`--with-funding` to enable): it
changes only the backtest's PnL/cost, not which entries the model picks, so it is
irrelevant to the entry agreement and a full-universe funding backfill is slow.

- **LONG** (discrete-event): runs the v11a backtest over the forward window on
  the fresh root and reconciles the **backtest entries vs demo and vs paper** by
  `(symbol, side, signal-day)`, plus the demo↔paper execution leg. `model_only`
  is expected when the live sleeve was off/just re-enabled; the tripwire is a
  live entry with **no** matching backtest signal (`demo_not_in_model` /
  `paper_not_in_model` > 0 → possible look-ahead in live, stale-PIT in the
  backtest, or threshold drift). The backtest `run_label` is surfaced verbatim.
- **CONTINUOUS** (rebalance book): demo↔paper execution leg + a **backtest-match**
  that re-derives the deployed `continuous_ensemble_v2` ENTRY candidate set per
  component (`scripts/reconcile_fills.py`, default-ON rmom recompute as step 1b
  below). It reuses the engine's own `compute_continuous_decile_panel` +
  `_entry_event_expr` to reproduce the per-bar predicate `decile==9 & turnover≥liq
  & component-trigger` (the uncapped form of `select_continuous_entries`), then
  asks: is every live entry a genuine engine candidate? A live entry that is **not**
  a candidate is the tripwire — classified `hard` (off-decile → look-ahead/drift)
  vs soft (`near_decile ≥D8` boundary flip, or `no_panel_row` snapshot gap). Only
  `hard` fails. (This supersedes the older decile-membership-only check, which
  remains as a complementary signal-consistency leg.)

### Fill-level cross-check + the two recompute planes (2026-06-18)

`scripts/reconcile_fills.py` runs automatically inside the three-way and adds the
**entry-price** corner: for the entries the books share it joins `entry_price`
across backtest/model, demo, and paper and reports the pairwise delta in bps
(per-entry CSV at `data/reconcile/{long,continuous}_three_way_fills.csv`). LONG
uses the backtest `entry_price`; CONTINUOUS prices the model at the PIT kline close
and notional-weights paper's per-component legs into one symbol fill to match
demo's netted position.

- **Step 1b — rmom recompute (default ON for continuous):** `precompute_residual_momentum.py`
  refreshes the research-root `residual_momentum.parquet` so the independent-PIT
  plane runs on a fresh panel. Skip with `--no-rmom`; `--with-rmom` is a deprecated
  no-op.
- **Two planes:** the continuous backtest-match recomputes on the **live signal
  plane** (the demo root's current klines+rmom — verifies every entry NOW, this is
  the gate) and, on the **independent-PIT research plane** (freshly-downloaded
  `klines_1h` + recomputed rmom). The PIT plane is informational and **lags the live
  window** — but that is a DATA-FRESHNESS gap, not a horizon: rmom itself is causal
  (a ~2-3d completion lag, `rolling_sum(7).shift(3)`). `build_feature_panel` also reads
  `open_interest`/`premium`, and the **default** three-way refresh updates only the
  manifest + klines (not those derivative metrics), so on the research root they go stale
  (here OI ~05-26, premium ~05-30), truncating the factor panel — and via rmom's `shift(3)`
  its coverage — to ~06-02. Pass `--with-funding` (which refreshes funding + OI + mark +
  index + premium) to advance the plane. Until then recent continuous entries are reported
  `pending_rmom`, never failures.

Why the asymmetry: LONG entries pair 1:1 to the backtest trade ledger by
`(symbol, side, signal-day)`. CONTINUOUS is a path-dependent ensemble book — the
live engine caps entries (MAX_ACTIVE / max-new-per-cycle / cooldown / held-state),
so a backtest can't reproduce the exact entry SET; the recompute therefore yields
the UNCAPPED per-component candidate set and the check is directional (every live
entry must be a candidate; a candidate not taken live is expected capacity). The
backtest leg is agreement/execution evidence — never alpha proof and never a
promotion gate (`docs/backtesting_errors_we_never_repeat.md`). Matching skill:
`.claude/skills/pit-reconcile`.

## When a reconcile shows `paper-only` / `pit_membership_fail`

1. Refresh the manifest manually
   (`python -m liquidity_migration --data-root <root> archive-manifest`), then
   re-run `bash scripts/reconcile.sh`.
2. If a single very-recent signal is still `paper-only`, the trading-day archive
   has not published yet — wait for the next day (a current-universe diagnostic
   backtest is possible but is biased and must be labelled as such).
3. `paper↔demo` measures execution slippage and is independent of all of the
   above; if it is clean the live executor matches the model.

## Design receipt

The trading-day membership convention (membership keys on
`date(ts_ms − 1ms)`, the BINDING decision this gate implements) is recorded in
the archived `pit-membership-trading-day-fix` receipt in git history.
