---
name: pit-reconcile
description: Assess current engine-WAL, authenticated venue, PIT, and model evidence. Use for WAL/venue mismatches, fill or P&L evidence, archive_trade_manifest coverage, pit_membership_fail, or execution-accounting claims. Keep PIT validity separate from operational authorization and never treat projections as account authority.
---

# Reconcile account and PIT evidence

Start from current surfaces:

```bash
scripts/ops.sh help
scripts/ops.sh status
scripts/ops.sh attest-flat --environment demo
python -m liquidity_migration --help
```

These are demo or research tools. They never authorize real money.

## Account evidence

The Rust engine's write-ahead log (WAL) is the durable local order and fill
record. Sleeve books, strategy Parquet, dashboards, heartbeats, and
notifications are projections. The venue remains the authority for what the
account currently holds and which orders and executions it accepted.

For current credential-wide flatness, use the concrete realm:

```bash
scripts/ops.sh attest-flat --environment demo
```

This runs the installed venue adapter's two-scan proof across the credential,
not just the symbols in the heartbeat. It proves flatness only; it does not
prove historical fills, fees, funding, or P&L.

Read a WAL with the exact installed engine or the matching local build:

```bash
/opt/liquidity-migration-engine/bin/engine replay --wal /absolute/path/to/engine.wal
/opt/liquidity-migration-engine/bin/engine fills --wal /absolute/path/to/engine.wal
```

Record the engine version, checkout commit, WAL path and segment set. Compare
immutable client/order/execution identifiers and quantities with authenticated
venue results for the same account and interval. Do not turn a WAL total into a
venue-confirmed fee, funding, or P&L claim without that join.

For live mismatches, inspect the exact WAL segment set and replay head, owner
health, reconciliation events, immutable venue identifiers, and authenticated
venue snapshot. Preserve contradictory facts and stop unsafe writers. Do not
repair the headline by editing projections or resetting before flatness is
proved.

## PIT evidence

PIT membership is checked inside the research run whose claim depends on it.
For a targeted Bybit manifest rebuild:

```bash
python -m liquidity_migration --data-root ROOT archive-manifest \
  --start YYYY-MM-DD --end YYYY-MM-DD
```

The end is exclusive. Inspect manifest `source` values and kline coverage;
current-listing-derived tail rows are inference, not archive observations. A
partial/current-universe run may support only its declared narrower scope.

PIT coverage cannot prove execution, and a reconciled demo journal cannot repair
a survivorship-invalid historical claim. Apply `AGENTS.md`, `docs/data.md`
(Point-in-time membership), and `docs/architecture.md` (The account journal) to
keep those conclusions separate.
