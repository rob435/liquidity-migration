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

Current readers use `read_account_journal(..., verify=True)` or
`verify_account_journal(...)`. Strategy state, reconciliation, venue accounting,
owner health, and reset receipts reopen the verified account journal directly.
There is no separate journal CLI or parallel lifecycle journal.

## Epoch reset

The guarded reset does not edit journal history to manufacture flatness. It
first stops unsafe writers and proves the demo venue flat with no open orders,
then archives and verifies the prior account root before creating a new empty
account epoch. The old journal remains evidence in that archive. Paper reset
retires its deterministic epoch explicitly and never borrows the demo flatness
claim.

Mainnet authorization, alpha, and strategy equivalence are outside what a valid
journal can prove.
