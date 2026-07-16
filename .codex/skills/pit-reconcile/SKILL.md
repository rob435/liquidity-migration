---
name: pit-reconcile
description: Assess current account-journal, venue-accounting, PIT, and model evidence. Use for journal/venue mismatches, fill or P&L evidence, archive_trade_manifest coverage, pit_membership_fail, or execution-accounting claims. Keep PIT validity separate from operational authorization and never treat projections as account authority.
---

# Reconcile account and PIT evidence

Start from current surfaces:

```bash
scripts/ops.sh help
scripts/ops.sh status
scripts/ops.sh venue-accounting --help
python -m liquidity_migration --help
```

These are demo/paper or research tools. They never authorize real money.

## Account evidence

The canonical account journal is position, order, fill, fee, funding, and P&L
authority. Sleeve trade/order Parquet, dashboards, and notifications are
projections.

For a stopped demo interval, use:

```bash
scripts/ops.sh venue-accounting \
  --account-root /absolute/demo-account-root \
  --account-id bybit-demo-unified \
  --start-time-ms START_MS \
  --output /absolute/new/venue-accounting.json
```

Inspect current help for optional end time, sample floors, and tolerances. The
command captures Bybit executions, closed P&L, funding, positions, and orders;
replays the journal; checks lineage and totals; and requires local/venue
flatness where claimed. It is evidence only for the named interval.

For live mismatches, inspect the exact journal head/hash chain, owner health,
reconciliation events, immutable venue identifiers, and authenticated venue
snapshot. Preserve contradictory facts and stop unsafe writers. Do not repair
the headline by editing projections or resetting before flatness is proved.

There is no current combined historical/paper/demo structural-comparison command.
A request for model-versus-forward agreement needs a newly declared claim and
artifacts appropriate to that claim.

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
a survivorship-invalid historical claim. Apply `docs/governance.md`,
`docs/pit_gate.md`, and `docs/account_execution.md` to keep those conclusions
separate.
