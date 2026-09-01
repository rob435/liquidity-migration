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

The worker normalizes inputs, computes the registered features, and publishes
immutable sequence-numbered observations. Frequent inputs enter an fsynced,
size- and count-bounded JSONL journal. Each entry carries a strict schema,
contiguous source sequence, and the exact output bytes it produced. At a hard
limit or the one-hour checkpoint-age boundary, the worker streams its state
through a pending atomic checkpoint. Restart finishes an interrupted
publication, replays later journal entries, and requires the regenerated output
bytes to match exactly.

The checkpoint owns the random 128-bit source generation used by LONG and
CARRY. Restart and ordinary checkpoint compaction preserve that generation and
its output sequences. Only initializing a genuinely new state root creates a
new generation at sequence one. The engine removes an observation only after
its exact bytes are durable in the WAL.

One persistent Bybit public WebSocket actor owns ticker deltas and confirmed
hourly candles. Each socket is a source epoch. Subscription replies are matched
to the exact request. Forty-five seconds without an accepted market event opens
a gap; acknowledgements and pongs do not reset that clock or the retry delay. A
transient refusal, timeout, idle stream, or disconnect reconnects forever with
capped backoff. A permanently bad topic is narrowed to the individual topic and
quarantined; accepted topics remain live, and a one-minute timer re-probes the
topic on the same socket. Bybit's request-wide unsupported-operation/category
response is surfaced as a global fault and is never split into topic
quarantines.

A reconnect clears transient ticker state. REST candle repair closes the gap,
and REST ticker snapshots can fill missing or stale fields without overwriting
WebSocket fields received after the REST request began. Each ticker field keeps
its own receipt clock, so a delta cannot refresh an unchanged field.

Instrument, funding, candle-repair, ticker-fallback, and optional Binance whale
work run independently but share one process-wide HTTP request bound. Cold
candle acquisition commits profile-sized chunks rather than retaining all raw
results. Each history producer holds one bounded job and waits for its commit
acknowledgement before fetching another. Page length, timestamp grid, requested
range, unique-row count, and immutable history are checked before durable
mutation. A venue or normalization fault pauses only that lane and opens repair
where needed. A sequence, state, spool, serialization, or disk fault remains a
process error; it is never recast as ordinary source degradation.
responses. The reviewed LONG and CARRY populations are the worker generation's
ceiling. The live WebSocket follows both the top-of-book quote and ticker for
their current trading members plus BTC, ETH, and the registered regime symbol.
The engine execution feed also keeps a separate clock for each L1 quote topic.
A promised L1 snapshot that stays silent for 45 seconds is re-subscribed on the
live socket without interrupting healthy topics.

The signal spool has a total bound and separate `current`, `lifecycle`,
`catchup`, and `other` quotas. Market snapshots, readiness, LONG features, and
CARRY features coalesce while an older output of the same kind waits for the
engine. After that file drains, the next eligible source wake republishes the
latest state. Funding lifecycle rows and CARRY scorer catch-up remain ordered,
non-replaceable records. One class reaching its quota does not stop unrelated
classes unless the total spool limit is reached.

The heartbeat binds the worker to its realm and exact inputs and reports source
generation, input and output sequences, the current LONG and CARRY horizons,
CARRY scorer catch-up, independent LONG and CARRY cycle completions, REST
fallback, WebSocket epochs and topic coverage, queue bounds, and spool pressure.
During restart it stays `starting` until both sleeve cycles complete or the
three-cadence startup window expires. `ready` describes current WebSocket
transport health. A degraded transport can still produce decisions through
fresh REST fallback; the separate sleeve clocks prove whether it actually does.

## Native directional reducers

The engine records a signal observation before waking a strategy. The reviewed
universe is the exact runtime ceiling for one worker generation, not a
compile-time list. An observation may ask the engine to add market subscriptions
for accepted symbols inside that artifact; the engine persists those admissions
in the WAL. The artifact identity cannot change in place.

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

An account view whose private-stream observation clock is zero is explicitly
invalid even if its last equity and margin numbers are finite. It cannot size or
grow a position. A durable signal, selection, or handoff timestamp ahead of the
current wall clock also cannot reopen or grow risk after a clock rollback; the
plug schedules the next wake at that timestamp. Both cases still allow exits,
reduction-only resizes, stop repair, and checkpoint progress.

CARRY keeps its one-minute admission selection in the checkpoint. A new symbol
joins that set only after the shared target planner has a valid price,
instrument rule, entry window, quantized quantity, and venue minimum and can
therefore emit an `Enter` step. Missing facts preserve the desired target for a
later retry without consuming the limited slots.

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
view freshness, pending exposure, and the rolling-loss trip: once the engine's
own closed trades have lost the profile's share of the capital reference inside
24 hours, entries wait until those trades age out. A limit can block new or
growing risk; it cannot block a genuine reduction. The venue account is the
fact about quantity. The WAL is the fact about which strategy opened that
quantity, and a close the venue itself started is charged to the sleeve whose
claim it reduced. Unknown or contradictory attribution stops new exposure.

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

Deployment builds and installs the Rust release before rendering the
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
| CARRY | `carry-sizing-anchors-v1-early-exits-v1-target-book-v1` | `early_exits`, `sizing_anchors`, `target_book` |
| Exodus | `exodus-state-v1-v4-event-tape-v1-identity-v2` | `carry_events`, `identity`, `legacy_paths`, `state` |

The importer treats only an absent CARRY early-exit map and an absent CARRY
pre-settlement event tape as canonical empty state. All other sources must be
present, and a present malformed source is refused.

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

`maker_canary` keeps the fourth durable strategy slot (strategy ID 3) in mainnet
and is disabled. Its
signal decay, fair price, inventory protection, and quote plan are one Rust
reducer, and its registered JSON is rendered to TOML by Rust.
