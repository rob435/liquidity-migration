---
name: pit-reconcile
description: "Run the demo-forward reconciliation for ALL THREE sleeves (SHORT event/daily backtest<->paper<->demo, LONG v11a paper<->demo, CONTINUOUS fade paper<->demo + signal-consistency) and fix/diagnose PIT membership (archive_trade_manifest) problems. Use whenever asked to reconcile the demo/paper/backtest, when a reconcile shows paper-only / backtest-only mismatches, when a backtest reports pit_membership_fail, or when the archive manifest/klines are stale. Drives scripts/reconcile.sh, which now AUTO-provisions (pulls every sleeve, refreshes the manifest, auto-downloads recent klines, auto-recomputes rmom) and backtests a MINIMAL forward window. The canonical fix for the manifest-lag / missing-recent-coverage class of friction."
---

# PIT reconcile + membership runbook

The one command for a demo-forward reconciliation of **all sleeves** is:

```bash
bash scripts/reconcile.sh
```

It is now zero-friction and self-provisioning. In one shot it:
1. **pulls** the live demo+paper ledgers for every sleeve (short, long, continuous),
2. **refreshes** the archive manifest (PIT membership),
3. **auto-downloads** the recent klines the manifest covers but the local root lacks
   (the gap that used to need a hand-run `archive-download-klines-1h-api`),
4. **auto-recomputes** `residual_momentum.parquet` (the continuous gate),
5. checks PIT coverage (aborts a stale strict run),
6. backtests the promoted profile over a **minimal** forward window (only as far back
   as the forward ledger needs — ~45d warm-up — not a fixed 150-day slab),
7. reconciles each sleeve and prints one consolidated headline.

Safe by default: read-only against the VPS, demo only, never real money. Full
design: `docs/pit_gate.md`.

## The three sleeves it reconciles

- **SHORT** (event/daily): backtest ↔ paper ↔ demo (`reconcile-all`), +Bybit on request.
- **LONG** (v11a): paper ↔ demo (`reconcile-long-paper-demo`).
- **CONTINUOUS** (fade): paper ↔ demo (`reconcile-continuous-paper-demo`) **plus** a
  signal-consistency check (`scripts/continuous_demo_signal_check.py`) that replays the
  SHARED decile pipeline over the live root and confirms each demo entry was a genuine
  top-decile, rmom-low, liquid engine pick. Continuous is intra-hour decile-cross driven,
  so a ±1-decile difference vs a closed-bar replay is expected, not drift.

## When to use

- "reconcile the backtest / paper / demo / all sleeves", "is the live matching the model?"
- A reconcile shows `paper-only` / `backtest-only` / `pit_membership_fail`.
- The archive manifest looks stale, or the local klines are behind the forward trades.

## Decision flow

1. **Just run it.** `bash scripts/reconcile.sh`. Inspect first with `--dry-run`.
2. **Read the coverage table.** `✅` ⇒ the strict reconcile is valid. After the
   auto-kline-fill the local klines should reach today; a residual `⚠️` means the
   trading-day **archive** has not published yet (wait a day, or `--diagnostic`).
3. **Read the per-sleeve summary block.** `paired` / `backtest-only` / `paper-only` /
   `slip`. `paper↔demo` clean = the live executor matches the model. A single
   very-recent `paper-only` (SHORT) is the inherent ~1-day archive lag, not a bug.
4. **Per-trade detail** — each leg writes a `*_pairs.csv` next to its `.md` report.

## Flags (all optional)

- `--sleeves short,long,continuous` — pick a subset (default all three).
- `--dry-run` — print every command, run nothing.
- `--no-pull` / `--no-manifest` / `--no-kline-fill` / `--no-rmom` — skip a
  provisioning step that is already fresh.
- `--full-window` — fixed 150-day backtest (the old behaviour); `--warmup-days N`
  overrides the minimal warm-up (default 45d, which is exact: deepest kline lookback
  is 30d features + 5d cooldown + 3d hold; the 300d age gate is manifest-derived).
- `--no-backtest` — reconcile ledgers only (no SHORT backtest leg).
- `--diagnostic` — backtest with `--pit-membership current-universe` (biased,
  same-day; **never** promotion evidence).
- `--with-bybit` — also reconcile SHORT `demo↔Bybit` (needs API creds in `.env`).
- `--force` — run the backtest even if coverage is stale.
- `--bybit-root PATH` / `--config PATH` / `--vps`.

## Why the minimal window is exact (not a corner cut)

The `backtest_paper` reconcile auto-windows the **comparison** to the paper ledger's
first signal, so warm-up trades never become false `backtest-only` rows. Validated:
the 45-day-warmup backtest reproduces the identical forward trade set as the old
150-day slab — same `paired`, same `backtest-only=0` — at ~3× the speed.

## Guardrails

- A `current-universe` / `--diagnostic` run is a biased diagnostic — never cite it
  as promotion or OOS evidence (`docs/backtesting_errors_we_never_repeat.md`).
- The continuous signal-consistency check is a *consistency* check on live data, NOT
  promotion/OOS evidence (today's bars are incomplete; rmom completes ~2d late).
- Before promoting anything, the strict (non-`--diagnostic`) reconcile must be clean
  over the forward window.
