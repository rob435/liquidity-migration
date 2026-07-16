# Data Roots

Research roots and operational account roots are different trust domains. Never
point an order-writing runtime at a research root or use a demo ledger as a PIT
population source.

## Research full-PIT roots

The normal per-venue working roots are:

```text
~/SHARED_DATA/bybit_full_pit
~/SHARED_DATA/binance_full_pit
```

They are mutable local datasets, not committed artifacts and not statistical
holdouts by themselves. Inspect actual manifest, kline, funding, and ancillary
coverage for every claim; a directory name does not prove completeness.

The supported resumable builders are:

```bash
BYBIT_START=YYYY-MM-DD BYBIT_END=YYYY-MM-DD \
  bash scripts/build_full_pit_bybit.sh

BINANCE_START=YYYY-MM-DD BINANCE_END=YYYY-MM-DD \
  bash scripts/build_full_pit_binance.sh
```

All `END` values are exclusive. The Bybit builder combines archive-observed
membership with explicitly labelled current-listing inference, downloads
manifest-gated 1h klines, and adds funding/OI/mark/index/premium data. The
Binance builder uses USD-M archive klines plus the current-month daily tail and
adds funding/OI/mark/index/premium/taker flow. Read each script before a large
run; defaults and upstream availability can change.

For a targeted Bybit manifest rebuild:

```bash
python -m liquidity_migration --data-root ROOT archive-manifest \
  --start YYYY-MM-DD --end YYYY-MM-DD
```

Use command help for targeted kline/data downloads. Preserve the exact command,
root identity, source labels, boundary, warnings, config, and output receipt for
decision-influencing work.

## Population and exposure limits

“Full PIT” means full coverage under the repository's declared manifest
contract. It does not prove venue facts the sources never observed. Bybit
current-listing-derived tail rows remain inference; they are not archive
observations. See `docs/pit_gate.md`.

A full-history root can still support a prospectively held-out time window, but
only if that window has not influenced design. Once inspected or used to adapt
the system, it is spent. Cross-venue agreement is robustness evidence when the
claim needs it, not automatic independence.

## Operational roots

Exact VPS paths come from the strict files:

```text
/etc/liquidity-migration/account-execution.env
/etc/liquidity-migration/account-paper-execution.env
```

Each route names a separate account journal root, target inbox, and market
capture. Demo and paper roots must be absolute, real, owner-controlled,
pairwise-disjoint, and non-nested. Strategy roots hold signal inputs, caches,
and cycle telemetry; they are not position or P&L authority.

The demo account owner alone mutates Bybit. The paper owner alone advances the
deterministic paper account. Their canonical journals own lifecycle and
accounting state; Parquet views are rebuildable projections.

`deploy/sleeves.env` sets the repository ceiling for target producers and the
generated resolved file records effective host toggles. Turning a producer off
does not erase its last accepted target or prove the account flat.

Use `scripts/ops.sh status` for the bound runtime topology and
`scripts/ops.sh venue-accounting` for a named stopped demo accounting interval.
PIT validity remains a separate research check; there is no command that turns
PIT coverage plus a live ledger into strategy or deployment proof.
