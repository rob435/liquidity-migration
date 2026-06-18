# Baseline Receipt: Continuous V2 Forward Control

**Date:** 2026-06-18
**Recorded at:** 2026-06-18T20:26:00Z
**Stage:** accepted baseline control
**System:** `continuous_ensemble_v2`

## Purpose

This receipt freezes the v2-forward control surface for future continuous A/B
tests. Every experimental continuous feature must compare against this control
unless a later operator-approved receipt explicitly replaces it.

This is execution/reconciliation evidence only. It is not alpha proof, not a
paper-ready claim, and not a real-money approval. `REAL_MONEY` remains false.

## Control Identity

- Control profile: `continuous_ensemble_v2`
- Demo strategy id for new rows: `continuous_fade_v2`
- Paper strategy id for new rows: `continuous_fade_v2_paper`
- V2-forward start: `2026-06-18T19:54:00+00:00`
- V2-forward start ms: `1781812440000`
- Pre-purge Git HEAD at first receipt run:
  `42fdc3fc908ece5a6e49dce0070177dd3f26f042`

The v2 boundary is enforced in continuous readiness, rebalance cycle/order
audits, signal checks, and fill-level reconciliation. The active demo/paper
ledgers were hard-reset to this boundary; if archived pre-v2 rows are inspected
later, they must stay excluded from this baseline gate.

## Runtime Configuration

Live/demo profile hash over the deployed control fields:
`627412c1e79a989d00ddecfc597433087bbf7be3eeffc483a981bcd8fef6f0fd`

Forward object hash:
`0977d6ded240665f1a5cfc945bc2835f41d88daf1a055bd661dec2f9a06d14ba`

Pinned control fields:

- Ensemble weights: p3 `0.3333333333333333`, p4p3 `0.2222222222222222`,
  p4p5 `0.4444444444444444`
- BTC trend gate: `uptrend`
- Rmom quantile: `0.25`
- Liquidity turnover floor: `500000`
- Entry sizing: `inverse_vol`
- `target_vol_per_name`: `0.01`
- `vol_weight_clamp`: `2.0`
- Daily vol-target rebalance: enabled
- Rebalance target daily vol: `0.045`
- Rebalance max scale: `4.0`
- Rebalance drawdown half threshold: `-0.04`
- Rebalance resize cost: `10` bps
- Hedge: BTC+ETH 2f with BTC-vol regime overlay
- Hedge cost: `5` bps per hedge leg in the forward object
- Exits: component take-profit plus 24h max hold
- Server stop: off (`STOP_LOSS_PCT=0`)
- Daemon protective exits: off (`left_decile`, `stop_approach`,
  `failed_fade`, `breakeven`)

## Data Roots

- Demo ledger root: `data/bybit-continuous-demo-event`
- Paper ledger root: `data/bybit-continuous-paper-event`
- Reconcile research root: `~/SHARED_DATA/bybit_full_pit`
- Live signal plane: `data/bybit-continuous-demo-event/.cache/ws_klines/store.parquet`
- Independent PIT plane: `~/SHARED_DATA/bybit_full_pit`

The baseline reconcile was run with `--no-data-refresh`; the independent-PIT
rmom plane reported `rmom_through=2026-06-02`. Because the v2-forward control had
zero entries, this stale independent-PIT rmom horizon did not mask any entry.

## Reconcile Evidence

Command:

```bash
bash scripts/reconcile.sh --sleeves continuous --no-data-refresh --no-rmom
```

Result: passed.

Key output:

- Continuous readiness: `ok=True`
- Paper-only readiness mode: `True`
- Paper rebalance ok: `True`
- V2 start filter: `1781812440000`
- Strategy profile filter: `continuous_ensemble_v2`
- Paper strategy id filter: `continuous_fade_v2_paper`
- Demo strategy id filter: `continuous_fade_v2`
- Signal consistency: `0/0 confirmed D9`, `0 off-decile`, `0 no-panel-row`
- Fill join rows: `0`
- Hard off-decile drift: `0`
- Near-decile unmatched: `0`
- No-panel-row unmatched: `0`
- Pending rmom: `0`

Report paths:

- `data/bybit-continuous-paper-event/reports/continuous_forward_readiness/continuous_forward_readiness.md`
- `data/bybit-continuous-paper-event/reports/continuous_forward_readiness/paper_rebalance/continuous_rebalance_cycle_audit.md`
- `data/reconcile/continuous_three_way_fills.csv`

## Baseline Metrics

The v2-forward baseline has not produced a new v2 entry yet. Therefore the
baseline performance state is intentionally zero-entry and must not be read as a
return result.

Filtered demo rows at the reset snapshot:

- V2 cycle heartbeats: present and expected to increase while the daemon runs.
  Exact cycle-row counts are liveness telemetry, not a frozen control metric.
- V2 trades: `0`
- V2 open trades: `0`
- V2 closed trades: `0`
- V2 orders: `0`
- Observed rebalance days: `1`
- Cycle-derived total return: `0.0`
- Cycle-derived max drawdown: `0.0`
- Worst day return: `0.0`

Filtered paper rows at the reset snapshot:

- V2 cycle heartbeats: present and expected to increase while the paper daemon
  runs. Exact cycle-row counts are liveness telemetry, not a frozen control
  metric.
- V2 trades: `0`
- V2 open trades: `0`
- V2 closed trades: `0`
- V2 orders: `0`
- Observed rebalance days: `1`
- Cycle-derived total return: `0.0`
- Cycle-derived max drawdown: `0.0`
- Worst day return: `0.0`

Fill/equity status:

- `data/reconcile/continuous_three_way_fills.csv` contains only the header row.
- No demo/paper/model fill deltas exist yet because there are no v2 entries.
- Equity curve is flat at the v2-forward start for this receipt: no realized v2
  trade returns have occurred.

## Test Evidence

Focused tests:

```bash
.venv/bin/python -m pytest \
  tests/test_liquidity_migration_continuous_reconciliation.py \
  tests/test_runtime_scripts.py \
  tests/test_reconcile_fills.py -q
```

Result: `120 passed`.

Ruff:

```bash
.venv/bin/python -m ruff check \
  liquidity_migration/reconciliation.py \
  liquidity_migration/cli.py \
  liquidity_migration/cli_parsers.py \
  scripts/reconcile.py \
  scripts/reconcile_three_way.py \
  scripts/reconcile_fills.py \
  scripts/continuous_demo_signal_check.py \
  tests/test_liquidity_migration_continuous_reconciliation.py \
  tests/test_runtime_scripts.py \
  tests/test_reconcile_fills.py
```

Result: `All checks passed!`

Full pytest:

```bash
.venv/bin/python -m pytest -q
```

Result: `1988 passed`.

## Control Rule

Future continuous A/B tests must use this control unless explicitly superseded:

- Same v2 start boundary or a documented later forward boundary.
- Same profile and strategy IDs.
- Same costs, sizing, rebalance, hedge, and exit lifecycle.
- Same full-PIT and live-signal reconciliation gates.
- Same reporting of fills, trade count, equity, drawdown, and drift buckets.

Any experiment that changes more than one mechanism is not a clean A/B against
this baseline.
