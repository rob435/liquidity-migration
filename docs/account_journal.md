# Account Journal

The account kernel journal is the execution/accounting authority for historical,
paper, and demo account state. Its implementation lives in
`liquidity_migration/account_kernel.py`.

## Storage authority

For an account root:

```text
account_journal/
  transactions/*.json   authoritative atomic kernel transactions
  events.jsonl           rebuildable human/tooling projection
  journal.lock           cross-process writer lock
```

New writes commit one complete transaction segment by atomic replacement and
filesystem sync before updating the JSONL projection. A crash can therefore
expose the prior transaction set or the whole new transaction, not half a target
batch. If transaction segments exist, readers ignore the projection as an
authority source. A non-empty JSONL projection without transaction segments is
rejected and requires an explicit account-root reset; it is never auto-migrated.

Strategy read models, health, notifications, and reports are projections or
consumers. They do not override the account journal.

## Event and state integrity

Each event records schema, stable UUID, global sequence, type, correlation and
causation IDs, account/sleeve/symbol, wall and monotonic time, payload,
`prev_event_hash`, reducer `state_hash`, and `event_hash`.

The event ID is deterministic from account ID plus the caller's idempotency key.
Redelivery with identical content is a no-op; reuse with changed content raises
an integrity error. Verification rejects sequence gaps, duplicate IDs, mixed
account IDs, event-hash changes, hash-chain breaks, illegal transitions, and
state-hash disagreement.

The reducer owns these control-plane types:

```text
market_input_ref -> decision -> target -> risk_decision -> order_command
                 -> ack -> fill -> protection -> close -> pnl
```

Supplemental `ack_observation`, `order_status`, and `venue_snapshot` facts retain
transport timing, terminal partial-fill/cancel state, and authenticated venue
truth without rewriting earlier events.

Fills require a known accepted command and unique execution ID. Partial fills
advance position state by their positive delta. Target replacement, cross-sleeve
aggregation, risk decisions, commands, fills, close facts, fees, funding, P&L,
and venue snapshots are reconstructed by replay rather than trusted from a
mutable projection.

## Transactions and readers

`AccountJournal.transact` serializes writers with the journal lock, gives the
builder an isolated committed state, validates and reduces every proposed event,
writes one immutable transaction, then publishes the new in-process cache. A
projection-write failure cannot roll back or replace the committed transaction.
While a local transaction is between atomic segment replacement and cache
publication, concurrent in-process readers deliberately continue to see the
prior coherent cache. They must not interpret the newly visible segment as an
external commit and replay the whole immutable history under the cache lock:
that would make the writer wait behind an O(history) read while it still owns
the cross-process writer lock. Once the writer publishes all cache fields
together, later readers see the new state. A failed publication clears the
local guard so the next reader reconstructs the already-authoritative segment
set rather than hiding a durable commit.

Cold/stateful readers use `read_account_journal(..., verify=True)` or
`verify_account_journal(...)`. Strategy state, reconciliation, venue accounting,
owner startup and the liveness journal audit reopen and reduce
the full verified journal. Hot owner-health consumers instead scan the immutable
transaction filename sequence and authenticate only the latest transaction
payload before matching its exact sequence, account ID, and state hash to a
fresh health projection. That head read avoids replaying payload history; it is
valid only because every serving owner generation completed the full startup
verification, and it does not replace the independent full liveness audit.
There is no separate journal CLI or parallel lifecycle journal.

## Epoch reset

The guarded reset does not edit journal history to manufacture flatness. It
first stops unsafe writers and proves the demo venue flat with no open orders,
then archives and verifies the prior account root before clearing epoch payload
in place. Persistent owner, route, journal, inbox, and dataset lock inodes stay
intact; they are synchronization infrastructure, not carried-forward account
state. Before either lease is written, descriptor-rooted preflight rejects
symlinked parents, hardlinks, special files, and Linux bind/mount boundaries.
The clear binds every root, ancestor, directory, and preserved lock identity as
one batch, then performs a final exact rescan; any late or redirected entry seen
by that rescan makes the reset fail. Archive creation and the final pre-clear
check bind the recovery artifact to one exclusively created inode and SHA-256
sidecar rather than reopening a predictable output path. The old journal
remains evidence in that archive. Paper reset retires its deterministic epoch
explicitly and never borrows the demo flatness claim.

This is a fail-closed epoch transition, not an atomic transaction across six
filesystem roots. The archive output is descriptor-bound, while the tar input
walk remains pathname-based under the stopped-fleet and owner-lease boundary.
Once the first unlink occurs, an I/O failure or unmanaged writer can leave a
partial clear; the reset then leaves all managed units stopped rather than
claiming rollback or restarting into mixed epochs. A non-cooperating writer
after the final rescan is outside what descriptor validation can exclude.

Mainnet authorization, alpha, and strategy equivalence are outside what a valid
journal can prove.
