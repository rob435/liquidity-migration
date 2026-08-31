# Rust engine

The engine is the sole order and account authority for one venue account. It
owns private feeds, account reconciliation, the write-ahead log (WAL), strategy
state, risk admission, orders, stops, fills, and position attribution. The
public-signal worker has no account credential and Python has no live order
path.

The workspace is under [`engine/`](../engine):

| Crate | Responsibility |
| --- | --- |
| `engine-types` | typed market, order, strategy, signal, control, checkpoint, event, and WAL contracts |
| `engine-wal` | checksummed append-only storage, locking, barriers, replay, and segment rotation |
| `engine-risk` | account-wide admission and exposure accounting |
| `engine-venue` | venue adapters, credentials, authenticated account state, and account lease |
| `engine-strategies` | pure strategy reducers and thin runtime plugs |
| `engine-core` | boot recovery, event loop, scheduling, execution, controls, heartbeat, and commands |
| `signal-worker` | credential-free public data collection and normalized observation publication |

## One account, one writer

`engine run --config PATH` resolves the configured venue, builds strategies,
locks the WAL, authenticates the account, and takes the account-writer lease.
It refuses to run when either lock is already held.

Mainnet is a separate venue realm. Naming `bybit_mainnet` in TOML does not arm
it: the venue adapter requires the owner-managed `REAL_MONEY=true` credential
contract. Demo and mainnet use different configs, credentials, accounts,
leases, WALs, spools, state roots, heartbeats, and trade logs.

The engine process uses one current-thread Tokio runtime. Strategy callbacks
run on that ordered core. Network work may be asynchronous, but its answers
return as ordered engine events.

## Boot order

Boot establishes identity before accepting risk:

1. Parse and hash the exact config bytes.
2. Resolve the compiled venue and strategy plugs.
3. Lock and replay the WAL, including its strategy and symbol name table.
4. Build the venue once for the selected realm.
5. Authenticate the account and take its single-writer lease.
6. Subscribe the private stream and wait for its readiness watermark.
7. Reconcile WAL orders, fills, positions, stops, and attribution against the
   authenticated account snapshot.
8. Restore current strategy checkpoints, pending strategy events, signal
   receipts, runtime controls, covers, and working orders.
9. Wake every strategy once to re-plan restored state and re-arm its clocks.
10. Start market, private-order, signal, and control inputs.

No opening order is admitted while account identity, private-feed readiness,
or recovery is unresolved.

## Machine config

The installed TOML contains an `[engine]` block, one `[risk]` block, and an
ordered list of `[[strategy]]` blocks. Engine-owned fields reject unknown keys.
Each strategy receives only its own flattened parameter table.

The load-bearing engine paths are:

- `wal_path`: durable account and strategy ledger;
- `signal_spool_path`: immutable public observations;
- `control_spool_path`: immutable operator commands;
- `heartbeat_path`: atomic current health and account view;
- `trades_path`: one attributed closed-position row per completed round trip.

The directional strategy blocks contain only `name`, `sleeve`, and
`config_json`. Rust derives those blobs from registered JSON and the installed
operational profile:

```sh
engine render-native-config \
  --realm demo \
  --signal-config configs/signal-worker.demo.json \
  --long-rule configs/long_native_v12.json \
  --carry-rule configs/lane2_carry_hold_v7.json \
  --exodus-rule configs/lane2_exodus_short_v1.json \
  --operational-config configs/operational.demo.json \
  --long-entries-enabled true \
  --carry-entries-enabled true \
  --exodus-entries-enabled true \
  --template deploy/engine.demo.toml.template \
  --output /tmp/engine.demo.toml
```

`--check` renders again and compares exact bytes without changing the output.
Mainnet also supplies `--maker-rule` so the disabled maker block is generated
from its registered JSON instead of copied by hand.

Strategy order is durable identity. The deployed order is CARRY, LONG,
Exodus, then the disabled mainnet maker. Append a new strategy; never insert or
reorder one without an explicit WAL migration.

## Signal path

The signal worker publishes files named by sequence. Each file contains one
canonical observation envelope with realm, source, schema, strategy,
subscription, sequence, event time, payload bytes, and payload hash.

The engine validates the filename and envelope, appends the exact observation
to the WAL, crosses a durability barrier, and only then wakes the reducer. It
records consumption separately. A crash can therefore replay a durable
unconsumed observation, while a duplicate sequence or changed payload cannot
be silently applied.

The worker writes its own multi-output transaction to durable state before it
makes any output visible. The same checkpoint persists a random 128-bit source
generation used in both LONG and CARRY source names. Restore keeps that source
and its sequences; a new checkpoint creates a distinct source whose sequence
starts at one. The heartbeat binds that generation alongside the source config,
rules, feature contracts, operational profile, engine config, universe, input
sequence, output sequences, and feature watermarks.

Subscriptions are restored from the WAL. A new symbol can be admitted while
the process is running; the symbol table is append-only and its full mapping is
restated in the WAL.

## Strategy contract

A strategy plug supplies subscriptions and converts engine facts into one
typed reducer input. The reducer receives all time as data and returns effects;
it performs no I/O.

Native directional checkpoints contain:

- schema version;
- strategy kind;
- config fingerprint;
- last applied signal or event identity; and
- strictly validated typed state.

The engine appends a checkpoint only after validating it through the selected
strategy. Restore rejects another schema, strategy kind, fingerprint, malformed
payload, or conflicting input receipt.

CARRY publishes a typed event for Exodus. Publication is durable before the
CARRY checkpoint can say the event fired; consumption is durable only with the
destination's next checkpoint. Stable event IDs make retry exact.

See [`trading_logic.md`](trading_logic.md) for each reducer's economic rules.
The module and replay contract for a new strategy is
[`strategy_template.md`](strategy_template.md).

## WAL and barriers

The WAL is a checksummed binary frame stream carrying human-readable typed
records. It contains, among other things:

- boot/config identity and the append-only name table;
- signal observations and consumption receipts;
- strategy checkpoints, imports, events, and event consumption;
- accepted and consumed runtime controls;
- intents, risk verdicts, sent orders, amendments, cancels, and venue updates;
- account snapshots, reconciliation findings, stops, fills, covers, and trade
  diagnostics; and
- rotation base state needed to replay without older segments.

Opening order bytes cannot leave before their `OrderSent` record is durable.
Checkpoint and cross-sleeve ordering use the same explicit barrier contract.
Cancels do not add exposure, so recovery can safely repeat a cancel whose final
answer was lost.

Buffered records flush on `group_flush_ms`, which is constrained to 1–1000 ms.
Order and state barriers do not wait for that group tick. When the active WAL
passes `wal_rotate_mb`, rotation writes a complete segment base and archives
the old segment in place. Retention is an operator action.

A torn final frame is truncated only while holding the WAL lock. A corrupt
interior frame or inconsistent replay refuses boot.

## Risk and execution

The reducer proposes; the engine and risk kernel decide whether an action can
reach the venue. Admission accounts for:

- fresh authenticated equity and account state;
- live and pending gross exposure;
- account loss, margin, symbol, order, and notional caps from the installed
  operational profile;
- current quote freshness;
- instrument tick, quantity, and minimum-notional rules;
- stop-loss charge and venue leverage; and
- current sleeve attribution and one-way-symbol ownership.

Stale or missing facts block new or growing risk. They do not block a genuine
reduction. Rounding that turns a reduction into growth is refused.

Only one sleeve may own a venue symbol. The current owner can exit; another
sleeve waits until the account is flat and attribution is complete. The engine
serializes flat-first direction changes for one-way accounts.

The execution registry tracks each client order ID through send, venue
acknowledgement, fills, cancel/amend ambiguity, and terminal state. Pending
orders remain charged until the venue resolves them. Fill attribution comes
from the durable order owner, not from the latest desired state.

Venue-native stops are attached or repaired from the attributed position's
rule. A position cannot borrow another sleeve's stop.

## Runtime controls

Controls are immutable envelopes in the control spool. The engine validates
and WAL-records each request before applying it.

```sh
engine set-strategy-entry-permission \
  --config /etc/liquidity-migration/engine.toml \
  --strategy long \
  --entries-enabled false \
  --request-id operator-unique-id

engine flatten-strategy \
  --config /etc/liquidity-migration/engine.toml \
  --strategy long \
  --request-id operator-unique-id
```

Entry permission `false` blocks openings and growing resizes. It does not stop
signals, clocks, cancels, or exits. Resume is bounded by the committed config:
an operator cannot enable entries that the installed strategy config disables.

Flatten requires a durable disabled-entry override. It cancels owned opening
work, asks the reducer for reduction-only effects, and remains pending until
the authenticated position, attributed cover, and owned opening orders are
flat. Request IDs are idempotent; the same ID with different bytes is refused.

## Native state takeover

Takeover commands run only with the realm stopped. They lock the WAL and the
authenticated venue account, then require
`EXPECTED_ENGINE_ACCOUNT_USER_ID` to match the venue response.

`initialize-native-strategy-state` is valid only for a truly empty WAL and
appends one atomic segment-base seed containing canonical empty checkpoints
for every native strategy.

`import-strategy-state` verifies exact WAL strategy names, passes a complete
named source bundle to the selected strategy's strict decoder, and appends its
canonical checkpoint plus provenance and pending events. An exact retry is a
no-op. Partial sources, changed bytes, another account, another strategy
order, or conflicting state are refused.

`verify-native-strategy-state` locks the stopped WAL and checks current names,
checkpoint identities, payload validation, complete provenance, and the final
frame. Deployment runs verification before considering takeover complete.

## Heartbeat and trade log

The heartbeat is an atomic one-line JSON snapshot. It reports process/config
identity, authenticated account and lease, account freshness, replay and
reconciliation state, latches, strategy attribution, effective entry
permissions, pending flatten requests, positions, working orders, and recent
input progress. Per-symbol entry blockers describe ordinary strategy choices;
strategy errors name a sleeve whose reducer or input contract is broken.
Liveness treats stale, unreadable, wrong-realm, latched, or strategy-error
heartbeats as faults.

The trade log is append-only JSONL produced from attributed fills and closes.
It contains entry, exit, fee, realized profit and loss, hold time, and strategy
identity. Notifications use the heartbeat for actual open positions and this
log for completed exits; reducer targets are not accounting evidence.

`engine fills`, `engine latency`, and `engine replay` read WAL evidence without
touching the venue. Funding is not present unless reconciled from authenticated
venue history; it must not be inferred from price fills.

## Shutdown

SIGTERM and SIGINT finish the current ordered work, flush the WAL, stop the
loop, and release the account lease. Systemd restarts recover from the WAL and
authenticated account, not from process memory.

Stopping the signal worker is not an operator pause. Keep it running so held
positions continue to receive settlement facts and exit clocks.

## Development checks

From the repository root:

```sh
cargo fmt --manifest-path engine/Cargo.toml --all -- --check
cargo clippy --manifest-path engine/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path engine/Cargo.toml --workspace --all-targets
```

Use `engine bench` for local-loop measurements, `engine wal-cost` for the
storage barrier, and venue canaries only through their explicit realm and
execution contracts. A local benchmark is not live venue evidence.
