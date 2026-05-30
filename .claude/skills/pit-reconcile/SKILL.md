---
name: pit-reconcile
description: "Run the demo-forward reconciliation (backtest<->paper<->demo<->Bybit) and fix/diagnose PIT membership (archive_trade_manifest) problems for the liquidity-migration SHORT sleeve. Use whenever asked to reconcile the demo/paper/backtest, when a reconcile shows paper-only / backtest-only mismatches, when a backtest reports pit_membership_fail, when the archive manifest is stale vs the klines, or to refresh PIT membership. Drives scripts/reconcile.sh; the canonical fix for the 2026-05-30 off-by-one + manifest-lag class of bugs."
---

# PIT reconcile + membership runbook

The one command for a demo-forward reconciliation is:

```bash
bash scripts/reconcile.sh
```

It pulls the live ledgers from the VPS, refreshes the archive manifest (PIT
membership), checks coverage, runs the promoted backtest, runs `reconcile-all`,
and prints the headline. Safe by default: read-only against the VPS, demo only,
never real money. Read `docs/pit_gate.md` for the full design.

## When to use

- "reconcile the backtest / paper / demo", "is the live matching the model?"
- A reconcile shows `paper-only` / `backtest-only` / `pit_membership_fail`.
- The archive manifest looks stale (klines newer than the manifest).
- You need to refresh PIT membership before a same-day backtest.

## Decision flow

1. **Just run it.** `bash scripts/reconcile.sh`. Inspect first with
   `bash scripts/reconcile.sh --dry-run` if you want to see every command.
2. **Read the coverage table** it prints (step 3). `✅` ⇒ the strict reconcile is
   valid. `⚠️` ⇒ the manifest is behind the latest signal day; the tool refreshes
   it in step 2, so a `⚠️` after the refresh means the trading-day archive has not
   published yet (wait a day, or use `--diagnostic`).
3. **Read the summary** (step 6): `paired` / `backtest-only` / `paper-only` /
   `slip`. `paper↔demo` clean = the live executor matches the model. A single
   very-recent `paper-only` is the inherent ~1-day archive lag, not a bug.

## Flags (all optional)

- `--dry-run` — print every command, run nothing (use this first when unsure).
- `--no-pull` — use the local `data/bybit-{demo,paper}-event` ledgers as-is.
- `--no-manifest` — skip the manifest refresh (already fresh).
- `--no-backtest` — reconcile `paper↔demo` only (no backtest leg).
- `--diagnostic` — backtest with `--pit-membership current-universe` (biased,
  same-day; **never** promotion evidence) for a signal whose archive hasn't
  published yet.
- `--with-bybit` — also reconcile `demo↔Bybit` (needs API creds in `.env`).
- `--force` — run the backtest even if coverage is stale.
- `--bybit-root PATH` / `--config PATH` / `--paper-root` / `--demo-root` / `--vps`.

## What it is actually doing (so you can run a step by hand)

- Refresh membership: `python -m liquidity_migration --data-root ~/SHARED_DATA/bybit_full_pit archive-manifest --end <today+2>`
- Coverage check: `python -c "from liquidity_migration.pit_coverage import coverage_status, format_coverage; print(format_coverage(coverage_status('~/SHARED_DATA/bybit_full_pit')))"`
- Backtest: `python -m liquidity_migration --config configs/volume_alpha.default.yaml --data-root ~/SHARED_DATA/bybit_full_pit volume-events --start <fwd-150d> --end <today+1> --report-dir <dir> --allow-partial-pit`
- Reconcile: `python -m liquidity_migration reconcile-all --paper-data-root data/bybit-paper-event --demo-data-root data/bybit-demo-event --backtest-trades-csv <csv> --skip-bybit --output-dir <out>`

## Guardrails

- This is the SHORT sleeve. The long sleeve uses `reconcile-long-paper-demo`.
- A `current-universe` / `--diagnostic` run is a biased diagnostic — never cite it
  as promotion or OOS evidence (`docs/backtesting_errors_we_never_repeat.md`).
- `download-data` does NOT refresh the manifest; `reconcile.sh` and
  `download-data --refresh-manifest` do. Never trust a "fresh" root's PIT
  membership without checking the coverage table.
- Before promoting anything, the strict (non-`--diagnostic`) reconcile must be
  clean over the forward window.
