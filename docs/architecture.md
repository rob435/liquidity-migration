# Architecture

The live trading path is Rust. Each venue account has one engine process, one
credential-free public-signal worker, one write-ahead log (WAL), and one
account-writer lease. Python is for research, evidence, notifications, and
deployment tooling. It has no directional live-decision or order path.

## Runtime flow

```text
Bybit and Binance public data
        |
        v
Rust signal worker ---- atomic immutable observation ----> signal spool
                                                            |
                                                            v
venue private feeds ---> Rust engine WAL ---> native reducer ---> risk ---> order
                              |                   |
                              |                   +--> CARRY event --> Exodus
                              v
                    heartbeat and trade log
                              |
                              +--> liveness, notifier, operator controls
```

The demo and mainnet realms are separate throughout: config, worker state,
signal spool, control spool, engine state, WAL, heartbeat, trade log,
credentials, account lease, and systemd unit. A realm cannot fall back to the
other realm's path or credential family.

[`deploy/fleet_manifest.tsv`](../deploy/fleet_manifest.tsv) is the unit
inventory. It declares lifecycle order, activation policy, dependencies,
health evidence, and runtime artifacts. Tests require the systemd source tree
to match that inventory exactly.

## Public-signal worker

`engine/signal-worker` gathers the public inputs needed by LONG and CARRY. It
has no private venue credential and cannot submit, cancel, amend, or inspect an
account order.

Its machine inputs are:

- the realm signal config;
- the registered LONG rule;
- the registered CARRY rule;
- the installed operational profile;
- the engine config;
- the reviewed candidate universe.

The worker normalizes observations, computes the registered feature physics,
and publishes an immutable, sequence-numbered JSON object. A multi-output
transaction is written to durable worker state before any object becomes
visible. Restart completes or rolls back that transaction without publishing a
half generation. The checkpoint also owns a random 128-bit source generation.
LONG and CARRY publish under generation-qualified source names, so restoring the
checkpoint continues its sequence while creating a new checkpoint starts a new
source at sequence one. The engine removes an object only after its bytes are in
the WAL.

The heartbeat binds the worker to its public realm, public hosts, source-file
hashes, feature-contract hashes, engine-config hash, universe hashes, source
generation, input sequence, output sequences, and latest feature clocks.
`ready` means the cold history is complete and at least one watermark has been
published. The worker keeps the full causal 90-day, 150-name CARRY envelope
within the shared 16 MiB observation limit; the reducer, not the transport,
applies the registered top-N rule.

## Native directional reducers

The engine records a signal observation before waking a strategy. Symbol
admission is dynamic, so a reviewed universe is a starting set rather than a
compile-time ceiling.

The three directional sleeves have typed pure reducers under
`engine/engine-strategies/src/native_*`:

- `long_native` owns LONG signal interpretation, sizing, admission, entry,
  stop decay, cooldown, and time exit.
- `carry_native` owns the daily score, sizing anchors, ordinary and
  pre-settlement exits, drop exits, admission, resize boundaries, and current
  target state.
- `exodus_native` consumes only typed durable CARRY pre-settlement events and
  owns short entry, retry, cover, and consumed-event state.

The plug layer supplies facts: durable observation, account view, attributed
positions, owned orders, instrument rules, and clock. The reducer returns the
next checkpoint, cross-sleeve events, signal receipt, and order effects. It has
no file, network, credential, or clock access.

A decision fingerprint covers the rule and the feature contract that can
change a decision. Operational cadence, retry timing, and an operator entry
permission are not decision physics. Restore accepts only the exact checkpoint
schema, fingerprint, and strictly validated payload.

## Ordered durability

State and action ordering is part of the contract:

1. The engine appends the input observation and crosses a WAL barrier.
2. The reducer runs once from that input and its prior checkpoint.
3. A CARRY handoff event is appended before the CARRY checkpoint that records
   it as fired.
4. The destination can consume an event only after its next state is durable.
5. Opening orders reach the venue only after their WAL records cross the order
   barrier.

Restart rebuilds strategy names, signal receipts, checkpoints, unconsumed
events, runtime controls, attributed positions, covers, and working orders from
the WAL, then wakes each reducer once to restore its timers and outstanding
work. Segment rotation restates the same durable state. A duplicate input,
event, fill, or control request is identified by its stable identity and is
not applied twice. When a checkpoint already contains an event that remains
pending, the destination repeats only the acknowledgement.

## Account and risk authority

Only the engine receives private account credentials. It owns authenticated
feeds and REST, the venue/account identity check, account-writer lease, order
registry, fill attribution, reconciliation, leverage, stops, and risk
admission.

`engine-risk` applies the installed account-wide capital limits, margin and
gross ceilings, stop-loss charge, instrument rules, quote freshness, account
view freshness, and pending exposure. A limit can block new or growing risk;
it cannot block a genuine reduction. The venue account is the fact about
quantity. The WAL is the fact about which strategy opened that quantity.
Unknown or contradictory attribution stops new exposure.

## Operator controls

Pause and flatten are durable engine commands, not process controls.

- Entry permission `false` blocks entries and growing resizes for one sleeve.
  Signals, checkpoints, settlement clocks, cancels, and exits continue.
- Resume can restore permission only when the committed strategy config also
  enables entries.
- Flatten requires entries disabled. The reducer persists the request, cancels
  owned openings, emits reduction-only exits, and consumes the request only
  after venue position, attributed cover, and owned opening work are flat.

The Telegram helper is a commit-bound root boundary that submits these commands
through the control spool. The signal worker stays running during a pause or
flatten. Engine heartbeats report effective entry permissions and pending
flatten requests, so an operator waits for reducer completion rather than for
mere command-file acceptance.

## Stopped state takeover

Deployment builds and installs the trusted Rust release before rendering the
engine configs. `engine render-native-config` derives the exact LONG, CARRY,
Exodus, and mainnet maker TOML from registered JSON and the installed
operational profile. The installed file is atomically replaced and checked
against a second Rust render.

With all realm units stopped, the takeover command holds both the WAL lock and
the authenticated account lease. It verifies the expected account ID and exact
strategy order, translates each complete source bundle through the selected
native strategy's strict codec, and appends canonical checkpoints and pending
events. An exact retry is a no-op. A different source, partial bundle,
different checkpoint, wrong account, corrupt payload, or mixed strategy order
is refused.

The load-bearing source formats are:

| Sleeve | Format | Named sources |
| --- | --- | --- |
| LONG | `long-book-state-v2` | `state` |
| CARRY | `carry-reducer-v2-target-book-v1` | `reducer_checkpoint`, `target_book` |
| Exodus | `exodus-state-v1-v4-event-tape-v1` | `carry_events`, `identity`, `state` |

A truly empty account generation initializes canonical empty checkpoints. A
nonempty WAL must either verify as a complete current native generation or
provide the complete takeover bundle.

## Research and replay

Research shapes and grades rules in Python, but decision replay crosses the
same Rust reducer through the persistent `strategy_contract` JSONL adapter.
LONG, CARRY, and Exodus fixtures compare exact discrete effects, event IDs,
checkpoint bytes, and target decisions. The maker research adapter streams
normalized events through the Rust quoter reducer. These are refactor and
decision-parity fences, not fill or profit evidence.

The standard CARRY v7 curve sends the full backward-only daily feature frame
to Rust; Rust owns top-N selection, hysteresis, weights, and lifecycle.

[`strategy_template.md`](strategy_template.md) defines the module boundary,
durability order, restore behavior, replay adapter, and tests required for any
additional native strategy.

Historical fills remain models. Live execution evidence comes from the engine
WAL, authenticated venue state, and trade log. Evidence grading is defined in
[`research/governance.md`](research/governance.md).

## Other strategies

`maker_canary` keeps strategy slot 3 in the mainnet config and is disabled. Its
signal decay, fair price, inventory protection, and quote plan are one Rust
reducer, and its registered JSON is rendered to TOML by Rust.
