# Ledger month-partitioning — deploy + migration runbook (reconcile-ledger-5 / quality-dup-5)

## What changed
The demo/paper trade+order ledgers were one monolithic `part.parquet` per dataset —
every hot-path write rewrote the whole file under the lock (O(history)), and ws_risk
re-read the whole thing every ~60s reconcile pass. They are now month-bucketed
(`_ledger_month=YYYYMM/part.parquet`), keyed on a per-row IMMUTABLE timestamp
(trades → `entry_ts_ms`, orders → `ts_ms`) so a row can never change buckets on update
(which would strand a phantom OPEN copy). ws_risk's per-pass reconcile uses a windowed
read (`read_ledger_window`, last 6 months + the legacy tail); bootstrap still does a
FULL read so a restart re-loads every open trade regardless of age.

## This is backward-compatible — safe to deploy WITHOUT migrating first
After deploy, NEW writes go to month buckets and reads union the legacy monolith with
the buckets (deduped on the dataset key, freshest `updated_at_ms` wins). The live
daemons keep working with a still-present legacy monolith. The migration is an
OPTIMIZATION (drains the monolith so the windowed read stops always pulling it), not a
correctness prerequisite — run it whenever convenient.

## Migration (optional, optimization) — run with all demo daemons STOPPED
```bash
# 1. stop the systemd units so no writer races the migration
# 2. dry-run (reports legacy row counts, writes nothing):
.venv/bin/python scripts/migrate_ledger_buckets.py --dry-run <DEMO_ROOT> [<LONG_ROOT> <CONT_ROOT> ...]
# 3. migrate (idempotent + dedup-safe; removes each legacy part.parquet only after the
#    deduped key-set/row-count is proven unchanged):
.venv/bin/python scripts/migrate_ledger_buckets.py <DEMO_ROOT> [more roots...]
# 4. restart the daemons
```
Re-running is safe (idempotent). If any dataset's key-set or row-count would change, the
script ABORTS that dataset and leaves its legacy monolith in place.

## Verification
- `pytest tests/test_liquidity_migration_storage.py tests/test_migrate_ledger_buckets.py`
  pins: month-bucketing, the immutable-key no-phantom-open invariant, half-migrated
  monolith+bucket coexistence dedup, cross-bucket schema-drift union, the windowed read,
  and idempotent dedup-safe migration.
