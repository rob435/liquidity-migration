---
name: pit-reconcile
description: Reconcile LONG and CONTINUOUS demo and paper ledgers, run the full demo, backtest, and paper model check, and diagnose archive_trade_manifest or PIT coverage problems. Use scripts/ops.sh reconcile or bash scripts/reconcile.sh whenever checking execution agreement, model drift, unmatched rows, fill slippage, stale membership, or pit_membership_fail. Distinguish quick execution-plane evidence from the full model leg and verify current options with help.
---

# Reconcile ledgers and PIT membership

Use the single wrapper and inspect current options:

```bash
bash scripts/reconcile.sh --help
bash scripts/reconcile.sh --quick --help
scripts/ops.sh reconcile quick --help
scripts/ops.sh reconcile full --help
```

The commands are read-only against the VPS but can download data and write local
reports. They never authorize real money.

## Quick: paper and demo execution plane

```bash
bash scripts/reconcile.sh --quick
bash scripts/reconcile.sh --quick --sleeves long
bash scripts/reconcile.sh --quick --dry-run
```

Quick mode currently defaults to both active sleeves. It pulls paper/demo
ledgers and compares their execution/lifecycle surfaces; it does not run a
backtest or refresh PIT data. It does not rebuild research RMOM unless
`--refresh-rmom` is explicitly requested.

A clean quick result supports paper/demo execution agreement for the inspected
window. It does not establish agreement with the backtest model, alpha, OOS
performance, or deployment readiness.

## Full: data, model, demo, and paper

```bash
bash scripts/reconcile.sh
bash scripts/reconcile.sh --sleeves long
bash scripts/reconcile.sh --no-data-refresh
bash scripts/reconcile.sh --with-funding
bash scripts/reconcile.sh --dry-run
```

Full mode currently defaults to both sleeves. It:

1. refreshes the bounded manifest/kline tail unless disabled;
2. pulls demo and paper ledgers unless disabled;
3. refreshes the kline-based COMMON4 residual-momentum panel for continuous
   unless data/RMOM refresh is disabled;
4. runs the LONG forward-window backtest/model comparison;
5. re-derives CONTINUOUS per-component entry candidates and compares fills;
6. writes per-row artifacts and a combined status.

Funding refresh is off by default because it is not needed for entry agreement.
Use `--with-funding` when interpreting costed PnL. It is not an RMOM repair flag.
When independent refresh is skipped, read the printed plane and coverage because
the continuous check may use the live signal plane instead of an independent
research recompute.

## Interpret the legs separately

- Paper versus demo: execution agreement, misses, lifecycle, and fill/slippage
  differences.
- Model versus paper/demo: signal/config/data agreement for the modeled window.
- PIT status: coverage under the manifest contract in `docs/pit_gate.md`, whose
  V5-derived rows have explicit provenance limits.
- Funding-off PnL: not a net-performance result.

Read per-trade CSVs and report classifications directly. Do not hardcode current
bucket thresholds into this skill; source code and report output own them.
Investigate any unexplained live row absent from the model, material lifecycle
divergence, impossible fill, stale plane, or manifest failure before calling the
execution object consistent.

## Diagnose PIT failures

Run or preview the full wrapper first. For a targeted manifest refresh:

```bash
python -m liquidity_migration --data-root ROOT archive-manifest
```

Verify `source` provenance, end-exclusive boundary, archive lag, kline coverage,
and the active profile's `require_full_pit_universe` value. Do not loosen the
gate to rescue a historical-universe claim. A partial run can still support an
explicitly narrower diagnostic under `docs/governance.md`.
