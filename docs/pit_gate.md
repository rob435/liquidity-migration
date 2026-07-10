# The PIT membership gate

This is the operator + maintainer reference for the point-in-time (PIT) universe
membership gate — the thing that decides whether a backtest signal is allowed to
trade, and the thing that broke the backtest↔paper reconciliation on 2026-05-30.

TL;DR: the known signal-day off-by-one is fixed and the plumbing has coverage
checks. The manifest still has explicit provenance limits described below.
There is one command for the whole reconciliation — it refreshes
PIT data, pulls the live ledgers, runs each sleeve's backtest over the forward
window, and reconciles the model against demo + paper, all in a single run:

```bash
bash scripts/reconcile.sh              # full demo<->backtest<->paper, both sleeves
bash scripts/reconcile.sh --quick      # fast paper<->demo execution check only
```

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

## The one-command workflow

`scripts/reconcile.sh` is the single front door. By default it runs the full
demo ↔ backtest ↔ paper three-way for BOTH sleeves (see the next section). The
`--quick` flag routes to the fast two-way (paper ↔ demo execution only, driver
`scripts/reconcile.py`, no PIT download / no backtest). It tests execution-plane
agreement, not agreement with the backtest model:

```bash
bash scripts/reconcile.sh --quick              # both active sleeves
bash scripts/reconcile.sh --quick --sleeves long
```

The `--quick` path, in order:

1. **pull** — rsync every selected sleeve's demo + paper ledgers from the VPS
   (long `long_native_{demo,paper}_*`; continuous
   `continuous_fade_{demo,paper}_*` + the continuous rmom panel + WS kline
   store), read-only. Skip with `--no-pull`.
2. **optional RMOM maintenance** — quick mode does not rebuild research RMOM by
   default. Request it explicitly with `--refresh-rmom`.
3. **reconcile** — per sleeve: LONG `reconcile-long-paper-demo` (paper ↔ demo),
   and, only when selected, CONTINUOUS `continuous-forward-readiness --paper-only`
   + a signal-consistency replay.
4. **summary** — one unified headline across selected sleeves.

`--quick` flags: `--sleeves long,continuous`, `--dry-run`, `--no-pull`,
`--refresh-rmom`, `--bybit-root PATH`, `--config PATH`, `--vps HOST`. The matching
skills are `.claude/skills/pit-reconcile` / `.codex/skills/pit-reconcile`.

Refreshing the manifest on its own is the manual command above
(`python -m liquidity_migration --data-root <root> archive-manifest`).

## The three-way (demo ↔ backtest ↔ paper) workflow

This is the **default** of `scripts/reconcile.sh` (the whole reconciliation in
one run), implemented by `scripts/reconcile_three_way.py` for BOTH active sleeves:

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
  a candidate is the tripwire — classified `hard` (D7 or below → look-ahead/drift)
  vs soft (`near_decile ≥D8` boundary flip, or `no_panel_row` snapshot gap). Only
  `hard` fails. The older decile-membership-only check remains as a complementary
  signal-consistency leg and uses the same D8-soft/D7-hard boundary.

### Fill-level cross-check + the two recompute planes (2026-06-18)

`scripts/reconcile_fills.py` runs automatically inside the three-way and adds the
**entry-price** corner: for the entries the books share it joins `entry_price`
across backtest/model, demo, and paper and reports the pairwise delta in bps
(per-entry CSV at `data/reconcile/{long,continuous}_three_way_fills.csv`). LONG
uses the backtest `entry_price`; CONTINUOUS prices the model at the PIT kline close
and notional-weights paper's per-component legs into one symbol fill to match
demo's netted position.

- **Step 1b — RMOM recompute (default ON for full continuous):**
  `precompute_residual_momentum.py` refreshes research-root
  `residual_momentum.parquet`. Skip with `--no-rmom`; `--with-rmom` is a
  deprecated no-op. The current COMMON4 RMOM spine is kline-based and is not
  truncated by funding/OI/premium coverage. `--with-funding` is for costed PnL,
  not an RMOM repair switch.
- **Research vs live plane:** the normal full path uses the independently
  refreshed research root. When data/RMOM refresh is explicitly skipped it can
  fall back to the live signal plane, which is current but not an independent
  data recompute. Read the printed plane and coverage rather than assuming either.

Why the asymmetry: LONG entries pair 1:1 to the backtest trade ledger by
`(symbol, side, signal-day)`. CONTINUOUS is a path-dependent ensemble book — the
live engine caps entries (MAX_ACTIVE / max-new-per-cycle / cooldown / held-state),
so a backtest can't reproduce the exact entry SET; the recompute therefore yields
the UNCAPPED per-component candidate set and the check is directional (every live
entry must be a candidate; a candidate not taken live is expected capacity). The
backtest leg is agreement/execution evidence. It does not itself support alpha
or authorize deployment (`docs/governance.md`). Matching skill:
`.codex/skills/pit-reconcile`.

## When a reconcile shows `paper-only` / `pit_membership_fail`

1. Refresh the manifest manually
   (`python -m liquidity_migration --data-root <root> archive-manifest`), then
   re-run `bash scripts/reconcile.sh`.
2. If a single very-recent signal is still `paper-only`, the trading-day archive
   has not published yet — wait for the next day (a current-universe diagnostic
   backtest is possible but is biased and must be labelled as such).
3. `paper↔demo` measures execution-plane agreement and slippage independently of
   the model leg. A clean quick result does not establish model agreement.

## Design receipt

The trading-day membership convention (membership keys on
`date(ts_ms − 1ms)`, the BINDING decision this gate implements) is recorded in
the archived `pit-membership-trading-day-fix` receipt in git history.
