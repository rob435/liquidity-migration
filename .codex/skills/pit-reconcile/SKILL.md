---
name: pit-reconcile
description: "Run the demo-forward reconciliation for the promoted LONG v11a sleeve (paper<->demo) and fix/diagnose PIT membership (archive_trade_manifest) problems. Use whenever asked to reconcile the demo/paper ledgers, when a reconcile shows paper-only / demo-only mismatches, when a backtest reports pit_membership_fail, or when the archive manifest is stale. Drives scripts/reconcile.sh, which pulls the live ledgers, recomputes rmom for continuous diagnostics, reconciles each selected sleeve, and prints one unified summary. (Continuous is the LIVE demo book since 2026-06-09 — research-stage, NOT promoted; reconcile it for diagnostics via --sleeves continuous. Manifest refresh is the separate manual archive-manifest command.)"
---

> **ERASURE NOTE (2026-06-11, operator order):** the daily SHORT sleeve was
> ERASED from the system — the short backtest<->paper<->demo reconcile legs and
> short commands NO LONGER EXIST. reconcile.sh now covers LONG (paper<->demo)
> + continuous diagnostics only; ignore short instructions below.

# PIT reconcile + membership runbook

The one command for a demo-forward reconciliation of the **promoted sleeves (LONG; continuous opt-in)** is:

```bash
bash scripts/reconcile.sh
```

In one shot it:
1. **pulls** the live demo+paper ledgers for every selected sleeve (default: long),
2. **auto-recomputes** `residual_momentum.parquet` (only when continuous is
   explicitly selected for diagnostics; skip with `--no-rmom`),
3. **reconciles** each selected sleeve — LONG: `reconcile-long-paper-demo`
   (paper ↔ demo); CONTINUOUS: `continuous-forward-readiness --paper-only` +
   the signal-consistency replay,
4. prints one **unified summary** across selected sleeves.

(The old manifest-refresh / kline-fill / coverage / backtest provisioning steps
were removed with the erased SHORT sleeve's backtest leg. Manifest refresh is
now the manual `python -m liquidity_migration --data-root <root> archive-manifest`
command.)

Safe by default: read-only against the VPS, demo only, never real money. Full
design: `docs/pit_gate.md`.

## The sleeves it reconciles (LONG; continuous opt-in)

- **LONG** (v11a): paper ↔ demo (`reconcile-long-paper-demo`). The default sleeve.
- The SHORT legs were ERASED 2026-06-11 with the sleeve.

> **CONTINUOUS** (fade) is the LIVE demo book (operator re-shape 2026-06-09:
> the VPS runs ONLY the continuous system). It remains research-stage — NOT
> promoted (rmom latency knife-edge stands; never present it as promoted). It is
> NOT reconciled by default; run `--sleeves continuous` for diagnostics
> (`continuous-forward-readiness --paper-only` + the
> `scripts/continuous_demo_signal_check.py` signal-consistency replay).

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

- `--sleeves long,continuous` — pick a subset (default `long`; add `continuous`
  explicitly for diagnostics).
- `--dry-run` — print every command, run nothing.
- `--no-pull` — skip the VPS ledger rsync; use local ledgers as-is.
- `--no-rmom` — skip the automatic `residual_momentum` recompute (continuous only).
- `--bybit-root PATH` / `--config PATH` / `--vps HOST`.

## Guardrails

- A `current-universe` / partial-PIT run is a biased diagnostic — never cite it
  as promotion or OOS evidence (`docs/backtesting_errors_we_never_repeat.md`).
- The continuous signal-consistency check is a *consistency* check on live data, NOT
  promotion/OOS evidence (today's bars are incomplete; rmom completes ~2d late).
- Before promoting anything, the strict reconcile must be clean over the forward
  window.
