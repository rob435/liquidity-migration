---
name: pit-reconcile
description: "Reconcile the live demo/paper ledgers for the LONG (v11a) and CONTINUOUS (fade) sleeves, and fix/diagnose PIT membership (archive_trade_manifest) problems. ONE command: `bash scripts/reconcile.sh` runs the full demo<->backtest<->paper three-way for both sleeves (downloads fresh PIT data, runs each sleeve's backtest over the live forward window, reconciles the model against demo+paper); add `--quick` for the fast two-way (paper<->demo execution only). Use whenever asked to reconcile the ledgers, run a demo-backtest-paper reconciliation, when a reconcile shows paper-only / demo-only mismatches, when a backtest reports pit_membership_fail, or when the archive manifest is stale. Continuous is demo/paper research-stage and promoted-in-code only by operator override; the backtest leg is agreement/execution evidence, never alpha proof or a promotion gate."
---

> **ERASURE NOTE (2026-06-11, operator order):** the daily SHORT sleeve was
> ERASED — its specific short reconcile commands no longer exist. The
> demo<->backtest<->paper *backtest leg* was REBUILT generically (2026-06-17) for
> the surviving LONG + CONTINUOUS sleeves. **2026-06-18:** the scripts were
> consolidated behind ONE front door — `scripts/reconcile.sh` now runs the full
> three-way by default; `--quick` is the fast paper<->demo check.

# PIT reconcile + membership runbook

There is ONE command for the whole reconciliation. By default it runs the full
**demo ↔ backtest ↔ paper** three-way for BOTH sleeves (a fresh PIT download + a
backtest over the live forward window + the model-vs-demo-vs-paper reconcile):

```bash
bash scripts/reconcile.sh                    # full three-way, both sleeves (default)
bash scripts/reconcile.sh --no-data-refresh  # full three-way, skip the PIT download
bash scripts/reconcile.sh --sleeves long     # one sleeve
bash scripts/reconcile.sh --dry-run          # print every command, run nothing
bash scripts/reconcile.sh --quick            # FAST two-way (paper<->demo execution only)
```

Safe by default: read-only against the VPS, demo only, never real money. Full
design: `docs/pit_gate.md`. (`scripts/reconcile_three_way.sh` remains as a
back-compat alias for the default full run.)

## The fast two-way — `bash scripts/reconcile.sh --quick`

`--quick` skips the PIT download + backtest and runs only the paper↔demo
execution check (driver `scripts/reconcile.py`). Use it for a quick "is the live
executor matching the model?" pass once the data root is already current. In one
shot it:
1. **pulls** the live demo+paper ledgers for every selected sleeve (default: long),
2. **auto-recomputes** `residual_momentum.parquet` (only when continuous is
   explicitly selected for diagnostics; skip with `--no-rmom`),
3. **reconciles** each selected sleeve — LONG: `reconcile-long-paper-demo`
   (paper ↔ demo); CONTINUOUS: `continuous-forward-readiness --paper-only` +
   the signal-consistency replay,
4. prints one **unified summary** across selected sleeves.

## Three-way (demo ↔ backtest ↔ paper) — the default of `reconcile.sh`

The full three-corner reconciliation of **both** sleeves (rebuilds the backtest
leg lost with the erased SHORT sleeve) is what `bash scripts/reconcile.sh` runs
by default:

```bash
bash scripts/reconcile.sh                    # both sleeves, full pipeline
bash scripts/reconcile.sh --no-data-refresh  # skip the PIT download (root as-is)
bash scripts/reconcile.sh --sleeves long     # one sleeve
bash scripts/reconcile.sh --dry-run          # print every command, run nothing
```

In one shot it:
1. **refreshes PIT data** on the research root (`~/SHARED_DATA/bybit_full_pit`):
   archive-manifest + 1h klines, bounded to a gap-only tail window (incremental
   CLI stages, `--allow-degraded` manifest). **Funding is OFF by default** — it
   affects the backtest's PnL/cost only, NOT which entries the model picks, and a
   full-universe funding backfill is slow (retry-on-empty across ~800 symbols).
   Pass `--with-funding` when you actually want costed PnL. Skip the whole refresh
   with `--no-data-refresh`.
2. **pulls** the live demo+paper ledgers from the VPS (read-only).
   - **1b. rmom recompute (continuous, default ON):** `precompute_residual_momentum.py`
     refreshes the research-root `residual_momentum.parquet` so the independent-PIT
     plane (step 5) runs on a fresh panel. Skip with `--no-rmom`; `--with-rmom` is a
     deprecated no-op.
3. **LONG (discrete-event):** runs the v11a backtest over the forward window
   (default start = the 2026-06-04 demo start) on the fresh root — **windowed**:
   it reads/features only `[window_start − 150d warmup, window_end]`, not the full
   multi-year sample (`read_start_date` / `--read-warmup-days`, with date-partition
   pruning at the read), so the full-PIT gate is scoped to the window and the read
   skips the multi-year tail. Then it reconciles the **backtest entries vs demo and
   vs paper** by `(symbol, side, signal-day)`, plus the demo↔paper execution leg.
4. **CONTINUOUS (rebalance book):** demo↔paper execution leg + an engine-decile
   **signal-consistency** leg over BOTH the demo and paper live entries (the shared
   decile pipeline confirms each live entry was a genuine D9 pick — a costed
   `continuous-events` run cannot reproduce `FROZEN_FORWARD_CONFIG`'s ensemble+hedge).
   This is now the *complementary* check; the **stronger per-component backtest-match
   is step 5**, which reproduces the actual entry candidates, not just decile membership.
5. **fill-level entry-price cross-check + backtest-match** (`scripts/reconcile_fills.py`,
   runs automatically): joins the actual **entry prices** across all three corners and
   reports the pairwise delta in bps, AND verifies each live entry is reproduced by an
   independent recompute of the engine selection.
   - **LONG** uses the backtest `entry_price`, keyed `(symbol, side, signal-day)`.
   - **CONTINUOUS** reproduces the deployed `continuous_ensemble_v2` ENTRY candidate set
     by re-running the engine's OWN shared functions — `compute_continuous_decile_panel`
     then the per-component `decile==9 & turnover≥liq & _entry_event_expr(trigger)` filter
     (the current components are p3/p4p3/p4p5) — so `in_model` means
     "the engine, recomputed on the signal-plane klines+rmom, generates this exact entry."
     The PIT kline close at the entry bar is the model fill price; paper's per-component
     legs notional-weight into one symbol fill to match demo's netted position.
   Per-entry tables: `data/reconcile/{long,continuous}_three_way_fills.csv`
   (`in_model/in_demo/in_paper`, `px_*`, `bps_*`, continuous adds `model_components`).
6. prints one **three-way summary** and a non-zero exit if a LIVE entry lacks a model
   justification — for continuous, only a genuine **off-decile (≤D7)** unmatched entry is
   HARD (look-ahead/drift); a `near_decile (≥D8)` flip (one decile below, or top-decile but
   failing the marginal turnover gate at the closed bar vs the live synthetic-price bar), a
   missing-panel-row snapshot gap, and entries after rmom coverage (`pending_rmom`) are
   reported but do NOT fail the gate.

**Seamless by default — one command does it all.** `bash scripts/reconcile.sh` runs the
full continuous chain with no flags: PIT kline/manifest **download** → **rmom recompute**
on the research root (step 1b, default ON) → **engine recompute → entry+fill cross-check**.
The backtest-match runs on TWO planes:
- **live signal-plane (primary, gates now):** recompute on the demo root's current
  klines+rmom — verifies every live entry immediately. This is what passes/fails the run.
- **independent-PIT (secondary, informational):** recompute on the research root's
  freshly-downloaded `klines_1h` + freshly-recomputed rmom. This is the fully-independent
  data check, but its rmom coverage currently ends ~2 weeks back: `build_feature_panel` reads
  `open_interest`/`premium`, and the DEFAULT refresh updates only manifest+klines, not those
  derivative metrics — so they go stale on the research root (OI ~05-26, premium ~05-30) and
  truncate the factor panel (→ rmom ~06-02). Pass `--with-funding` to top them up. So recent
  entries sit in `pending_rmom` and back-fill once those inputs advance. This is a
  DATA-FRESHNESS gap — rmom itself is causal (~2-3d lag, `shift(3)`), not a forward horizon.
`--no-rmom` skips the recompute (uses the on-disk panel); `--no-data-refresh` skips both the
download and the recompute. The independent-PIT line still prints (just with older
`rmom_through`). Both planes fail the gate only on a HARD off-decile unmatched entry.

Run the fills leg alone (no backtest/VPS) against a local snapshot:
`python scripts/reconcile_fills.py --sleeve continuous` (defaults to the live demo plane;
add `--research-root <path>` for the independent PIT recompute) or `--sleeve long
--long-trades-csv <path>`. The listing-age floor (210/240d) is delegated to the live engine
whenever the kline history is shorter than the floor (a short WS store can't support it).

**Why the two sleeves differ:** LONG entries pair 1:1 to the backtest trade ledger by
`(symbol, side, signal-day)`. CONTINUOUS is a path-dependent ensemble book — the live
engine caps entries (MAX_ACTIVE / max-new-per-cycle / cooldown / held-state), so a
backtest can't reproduce the exact entry SET; instead the recompute generates the UNCAPPED
per-component candidate set and the check is directional: every live entry must be a
candidate (else tripwire), while a candidate not taken live is expected capacity. Keys are
`(symbol, signal-bar)` (component-agnostic, robust to sniper/re-entry sharing a signal_ts);
`model_components` records which components generated it.

**Reading the fills line:** `Δ demo↔paper` is the live execution gap (real Bybit
fill vs the idealized model fill — the PostOnly-sniper/cap drag); `Δ paper↔model`
near 0 means the live paper book faithfully replays the PIT model price, a large
value flags data-revision / look-ahead; `Δ demo↔model` is the total real-vs-clean
slippage. bps are SIGNED (`+` = filled higher); the book is short, so `+` is
favourable.

**Reading the continuous `backtest-match` line:** `confirmed` = live entries the engine
recompute reproduces. `off_decile(HARD)` is the real alarm — a live entry the recompute
puts at ≤D7 (look-ahead / drift / a reproduction bug); it fails the gate. The soft buckets
are expected noise and do NOT fail: `near_decile (≥D8)` (one decile below at the closed-bar
recompute — the live engine ranks with a synthetic live-price current bar so marginal names
flip — OR top-decile but failing the marginal turnover gate at the closed bar),
`no_panel_row` (the WS snapshot lacked that bar), `pending_rmom` (entry after the rmom
panel's coverage end — on the independent-PIT plane the research root's rmom coverage is
currently ~2 weeks stale because its factor-panel inputs lag, so recent entries can't be
confirmed there yet; the live plane is current). A handful of soft unmatched is normal; a
rising `off_decile` is the thing to chase.

**Reading the LONG three-way line:** `model_only` (backtest signalled, live didn't
act) is EXPECTED when the live sleeve was off / just re-enabled (LONG re-enabled
2026-06-16) — not a drift signal. The tripwire is `demo_not_in_model` /
`paper_not_in_model` > 0: a live entry with no matching backtest signal means
possible look-ahead in live, stale-PIT in the backtest, or a threshold drift —
stop and explain it. The backtest `run_label` is printed verbatim; a non
`full_pit_universe*` label is flagged as a biased diagnostic.

**Honesty:** the backtest leg is agreement/execution evidence, NOT alpha proof and
NOT a promotion gate (`docs/backtesting_errors_we_never_repeat.md`). Runtime is
dominated by the PIT download (network); use `--no-data-refresh` to re-run the
reconcile quickly once the root is current.

**Won't stall:** the PIT refresh is gap-aware (it starts a few days before the
stalest gating dataset — manifest/klines, plus funding only under `--with-funding`
— not a blind multi-week window), it SHORT-CIRCUITS any dataset already current to
the target day (a current root does ~no download work), and every sub-stage runs
under a hard wall-clock timeout (`--data-refresh-timeout`, default 2700s) so a
rate-limited stage aborts loudly instead of hanging. If the refresh ever does
abort, just re-run — it resumes from current coverage. (The earlier multi-hour
stall was a blind wide window re-checking already-present partitions for the whole
universe, plus a needless full-universe funding crawl — both removed.)

Extra flags: `--with-funding` (also refresh funding for costed PnL — slow, off by
default), `--no-rmom` (skip the research-root rmom recompute — it is ON by default
now for the continuous independent-PIT plane; `--with-rmom` is a deprecated no-op),
`--backtest-start YYYY-MM-DD` (override the forward-window start),
`--data-refresh-timeout SECONDS` (per-stage stall guard), `--no-data-refresh`,
`--no-pull`, `--bybit-root PATH`, `--vps HOST`.

## The sleeves it reconciles

- **Default (full three-way):** BOTH **LONG** (v11a) and **CONTINUOUS** (fade).
- **`--quick` (two-way):** **LONG** only by default (`reconcile-long-paper-demo`);
  add `--sleeves long,continuous` to include continuous diagnostics.
- The SHORT legs were ERASED 2026-06-11 with the sleeve.

> **CONTINUOUS** (fade) is the live demo/paper book. It remains research-stage
> and is promoted-in-code only by operator override, not by a real-money gate. Its
> three-way "model" leg is engine-decile signal-consistency of the live entries,
> not a trade-ledger pairing.

## When to use

- "reconcile the paper / demo ledgers (LONG; continuous opt-in)", "is the live matching the model?"
- A reconcile shows `paper-only` / `demo-only` mismatches, or a backtest reports
  `pit_membership_fail`.
- The archive manifest looks stale (fix: the manual `archive-manifest` command below).

## Decision flow

1. **Just run it.** `bash scripts/reconcile.sh`. Inspect first with `--dry-run`.
2. **Read the per-sleeve summary block.** `paired` / `paper-only` / `demo-only` /
   `slip`. `paper↔demo` clean = the live executor matches the model.
3. **Per-trade detail** — each leg writes a `*_pairs.csv` next to its `.md` report.
4. **Stale manifest / `pit_membership_fail` in a backtest?** Refresh PIT
   membership manually on the research root:
   `python -m liquidity_migration --data-root <root> archive-manifest`
   (the trading-day archive publishes ~1 day late; a same-day signal may not
   PIT-validate until the next day's manifest refresh — wait a day rather than
   loosening the gate).

## Flags (all optional)

Default (full three-way):
- `--sleeves long,continuous` — pick a subset (default: both).
- `--no-data-refresh` — skip the PIT download (use the root as-is; backtest may be stale).
- `--with-funding` — also refresh funding (slow; affects backtest PnL only, off by default).
- `--backtest-start YYYY-MM-DD` — override the forward-window start.
- `--data-refresh-timeout SECONDS` — per-stage stall guard.
- `--dry-run` — print every command, run nothing.
- `--no-pull` — skip the VPS ledger rsync; use local ledgers as-is.
- `--bybit-root PATH` / `--vps HOST`.

`--quick` (fast two-way) flags:
- `--sleeves long,continuous` — pick a subset (default `long`).
- `--no-rmom` — skip the automatic `residual_momentum` recompute (continuous only).
- `--dry-run` / `--no-pull` / `--bybit-root PATH` / `--config PATH` / `--vps HOST`.

## Guardrails

- A `current-universe` / partial-PIT run is a biased diagnostic — never cite it
  as promotion or OOS evidence (`docs/backtesting_errors_we_never_repeat.md`).
- The continuous signal-consistency check is a *consistency* check on live data, NOT
  promotion/OOS evidence (today's bars are incomplete; rmom completes ~2d late).
- Before promoting anything, the strict reconcile must be clean over the forward
  window.
