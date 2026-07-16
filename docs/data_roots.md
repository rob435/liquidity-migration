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
manifest-gated 1h klines, validates coverage without deleting missing expected
membership, and adds funding/OI/mark/index/premium data. The
Binance builder uses USD-M archive klines plus the current-month daily tail and
adds funding/OI/mark/index/premium/taker flow. Read each script before a large
run; defaults and upstream availability can change.

`symbol=` partition components use canonical UTF-8 percent encoding from
`liquidity_migration/symbol_codec.py`; ordinary ASCII symbols are unchanged.
Consumers that inspect directory names must use the matching decoder rather
than treating the component as the exchange symbol. Unsupported, ambiguous, or
path-like identifiers fail before root mutation. This retains valid Unicode
venue symbols without allowing two upstream identifiers to collapse onto one
local path.

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

### Persistent local locks

Dataset and account filesystem mutexes use persistent `flock(2)` leaves. Lock
file contents are ignored for ownership; the kernel flock on the open file
description, not a PID, timestamp, or payload, determines whether the mutex is
held. Normal release and crash recovery never unlink the canonical leaf. Old
JSON or empty leaves are adopted in place and restricted to mode `0600` after
safe acquisition.

The protocol requires a local POSIX filesystem with working advisory flock
semantics and cooperative repository clients. Each real, non-group/world-
writable lock directory is the ownership authority for its leaves. Paper
`.locks` directories and leaves are owned by `liquidity-migration-paper`;
stopped-fleet root reset processes may open them, and a root-first leaf is
prepared with the directory owner before it becomes visible. Deployment
pre-creates paper `.locks` directories as mode `0700`, and reset restores that
boundary before restarting paper services.

Host maintenance and account-owner leases follow the same persistent-inode
rule. Their parent namespaces and leaves are opened with no-follow descriptors,
checked for single-link ownership and Linux mount identity. Normal owners retain
the validated descriptor directly. During reset, the maintenance and
account-owner lease identities are handed to the shell without truncation and
revalidated around acquisition; account-lease metadata is written only after
the inherited descriptor still matches the prepared path and holds the kernel
flock.

These are advisory, cooperative local-filesystem locks, not protection against
a hostile privileged process. The Bash descriptor handoff cannot itself request
`O_NOFOLLOW`; private/root-controlled parent namespaces and the helper's
post-open identity validation are therefore part of the boundary. That
validation detects a replacement but cannot undo an open-time side effect from
a special file planted by an actor already able to mutate the protected
namespace.

Never delete a canonical lock as “stale.” The current guarded reset preserves
them; only a separately designed full-root retirement may remove one while every
possible client is stopped under a stronger operational boundary. The retired
create/PID/unlink protocol and the persistent-flock protocol cannot safely
coexist. Explicitly forking inside a held critical section is unsupported;
fork/exec helpers and forks from other threads are cleaned up by the storage
at-fork handler.

`deploy/sleeves.env` sets the repository ceiling for target producers and the
generated resolved file records effective host toggles. Turning a producer off
does not erase its last accepted target or prove the account flat.

Use `scripts/ops.sh status` for the bound runtime topology and
`scripts/ops.sh venue-accounting` for a named stopped demo accounting interval.
PIT validity remains a separate research check; there is no command that turns
PIT coverage plus a live ledger into strategy or deployment proof.
