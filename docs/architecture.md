# Architecture

The runtime has one execution authority: the Rust engine. Python researches
strategies, consumes credential-free market data, and publishes desired
positions. It cannot place, amend, cancel, adopt, or reconcile an order.

## Runtime boundary

```text
public market data -> Python strategy -> durable absolute target book
                                      -> Rust follower and risk kernel
private venue WS/REST -> Rust account state -> Rust WAL -> signed venue order
                                      -> exact-identity engine heartbeat
```

The demo and mainnet accounts each have one engine process and one state root.
The engine owns private venue credentials, authenticated transport, order
state, positions, risk admission, native stops, reconciliation, and crash
recovery. The two strategy producers own only their sleeve's decision logic and
target files. Telegram, liveness, reporting, and research are read-only with
respect to the venue.

The systemd inventory and credential boundaries are defined in
[`deploy/systemd/README.md`](../deploy/systemd/README.md). Engine internals are
defined in [`engine.md`](engine.md).

## Absolute target books

LONG and CARRY write versioned JSON books through
[`engine_targets.py`](../liquidity_migration/rules/engine_targets.py). A target
is signed USDT notional plus leverage and a mandatory stop fraction. Omission
means target zero. Empty means an explicit flat sleeve. No file means no new
decision and never means flat.

Publication has three boundaries:

1. Render and strictly validate deterministic bytes.
2. Durably create an immutable content-addressed object under
   `.target-book-objects/<sha256>.json`.
3. Atomically replace the engine-visible target path with those exact bytes.

The strategy host then appends a hash-chained activation receipt containing
the event identity, strategy identity, content hash, object path, and target
keys. The immutable object makes every activated historical book replayable;
the active path remains the low-latency latest-value interface.

The Rust follower owns entry quoting, working-order cancellation, convergence,
resizing, expiry, and exits. It must cancel an owned resting entry whenever its
target disappears or expires before considering new work.

## Strategy state and scheduling

Each producer is a plug on
[`strategy_host.py`](../liquidity_migration/strategy/strategy_host.py). The host
owns public WS caches, deterministic event tapes, deadlines, price-touch wakes,
cycle health, target evidence, and shutdown. It runs on confirmed bars,
semantic engine-account changes, strategy deadlines, price crossings, and a
bounded idle floor.

The engine rewrites telemetry every five seconds, but telemetry-only changes do
not wake a strategy cycle. The host compares a canonical projection of account
identity, positions, blockers, `may_open`, configured strategies, and engine
version. This prevents heartbeat cadence from becoming an expensive feature
recompute loop.

LONG persists its own requested-book state. Request time and fill time are
different fields: hold and stop-decay clocks begin only when the engine reports
an attributable fill. Each signal generation is submitted at most once, and an
unfilled request retains its original validity deadline across later cycles.

CARRY persists the equity anchor for each daily decision before publication.
A restart therefore cannot resize the same decision from mark-to-market noise.
Early-exit and Exodus state also reach durable storage before their
engine-visible books advance.

## Account truth and producer gate

[`engine_account_health.py`](../liquidity_migration/runtime/engine_account_health.py)
is the only Python view of account state. It opens one stable heartbeat
snapshot and requires:

- the configured realm;
- the exact expected venue account user ID;
- finite positive equity and finite available margin;
- a recent venue-observation wall timestamp;
- strict, non-duplicated position rows.

Producers read it again at the publication boundary. A long feature build or
market-data delay can therefore never size additions from an account sample
that aged past the configured bound during computation. Unknown account state
blocks additions and resizes. CARRY may still publish a strictly removal-only,
already-expired book derived from the prior verified target bytes.

Heartbeat positions and entry blockers are strategy-attributed. Producers must
filter by their exact configured Rust sleeve name; account-global positions
cannot prove sleeve ownership.

## Rust risk and durability

Risk admission lives only in `engine-risk`. The engine enforces the
equity-anchored envelope, gross and margin caps, leverage and instrument rules,
book age, account identity, working-order ownership, and venue-native stop
discipline. Reduction-only work is never blocked by a growth cap.

Every order command and execution transition is written to the Rust WAL. A
provider send happens only after the required durable state exists. Fills carry
venue execution IDs and are idempotent by that identity. Private and public
transport phases have explicit time and size bounds; malformed or stale input
fails closed to no new exposure.

## Research boundary

Historical Python runners replay registered rules chronologically and report
their declared cost and data assumptions. They do not emulate the live account
owner and cannot establish venue execution quality. Demo and funded evidence
comes from Rust WAL/fill/account artifacts. Promotion standards and caveats are
in [`research/governance.md`](research/governance.md).

## Verification

Use the narrowest relevant tests first, then the repository gates:

```text
scripts/dev.sh doctor
scripts/dev.sh check
cd engine && cargo test --workspace --all-targets
cd engine && cargo run --release -- bench
```

An execution-generation deployment remains blocked until the venue adapter can
enumerate every raw position and order for the exact venue account, including
unknown or delisted symbols. A configured-symbol scan is not a flat proof and
must never be used to authorize state replacement.
