---
name: pit-reconcile
description: "Reconcile the live demo/paper ledgers for the LONG (v11a) and CONTINUOUS (fade) sleeves, and fix/diagnose PIT membership (archive_trade_manifest) problems. ONE command: `bash scripts/reconcile.sh` runs the full demo<->backtest<->paper three-way for both sleeves by default (downloads fresh PIT data, runs each sleeve's backtest over the live forward window, reconciles the model against demo+paper); add `--quick` for the fast two-way (paper<->demo execution only). Use whenever asked to reconcile the ledgers, run a demo-backtest-paper reconciliation, when a reconcile shows paper-only / demo-only mismatches, when a backtest reports pit_membership_fail, or when the archive manifest is stale. (Continuous is the LIVE demo book since 2026-06-09 — research-stage, NOT promoted. The backtest leg is agreement/execution evidence, never alpha proof or a promotion gate.)"
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
by default. In one shot it:
1. **refreshes PIT data** on the research root (`~/SHARED_DATA/bybit_full_pit`):
   archive-manifest + 1h klines, bounded to a gap-only tail window. Funding is
   OFF by default (it affects backtest PnL only, not which entries the model
   picks); pass `--with-funding` for costed PnL. Skip the whole refresh with
   `--no-data-refresh`.
2. **pulls** the live demo+paper ledgers from the VPS (read-only).
3. **LONG (discrete-event):** runs the v11a backtest over the forward window on
   the fresh root, then reconciles the backtest entries vs demo and vs paper by
   `(symbol, side, signal-day)`, plus the demo↔paper execution leg.
4. **CONTINUOUS (rebalance book):** demo↔paper execution leg + the engine-decile
   signal-consistency of the live entries (its faithful 'model' leg).
5. prints one **three-way summary** and a non-zero exit if any LIVE entry lacks a
   model justification (the look-ahead / drift tripwire).

(The old manifest-refresh / kline-fill / coverage steps from the two-way path are
now folded into the three-way's bounded PIT refresh. Manifest refresh can still be
run manually: `python -m liquidity_migration --data-root <root> archive-manifest`.)

## The sleeves it reconciles

- **Default (full three-way):** BOTH **LONG** (v11a) and **CONTINUOUS** (fade).
- **`--quick` (two-way):** **LONG** only by default (`reconcile-long-paper-demo`);
  add `--sleeves long,continuous` to include continuous diagnostics.
- The SHORT legs were ERASED 2026-06-11 with the sleeve.

> **CONTINUOUS** (fade) is the LIVE demo book (operator re-shape 2026-06-09:
> the VPS runs ONLY the continuous system). It remains research-stage — NOT
> promoted (rmom latency knife-edge stands; never present it as promoted). Its
> three-way 'model' leg is engine-decile signal-consistency of the live entries
> (`scripts/continuous_demo_signal_check.py`), not a trade-ledger pairing.

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
- `--with-rmom` — also recompute research-root rmom (slow, off by default).
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
