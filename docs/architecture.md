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
recovery. The LONG, CARRY, and Exodus producers own only their decision logic,
durable public-data state, and target files. Telegram, liveness, reporting, and
research are read-only with respect to the venue.

[`deploy/fleet_manifest.tsv`](../deploy/fleet_manifest.tsv) is the canonical
inventory for unit identity, lifecycle and stop order, timers, operator policy,
health checks, and runtime artifacts. Systemd files implement that inventory;
tests reject missing or extra units and invalid dependencies. Engine internals
are defined in [`engine.md`](engine.md).

## Absolute target books

LONG, CARRY, and Exodus write versioned JSON books through
[`engine_targets.py`](../liquidity_migration/rules/engine_targets.py). A target
is signed USDT notional plus leverage and a mandatory stop fraction. Version 2
may also carry a direct entry deadline and/or an exact signed base quantity;
when quantity is present it is authoritative and not re-derived from a later
price. Omission means target zero. Empty means an explicit flat sleeve. No file
means no new decision and never means flat.

Publication has three boundaries:

1. Render and strictly validate deterministic bytes.
2. Durably create an immutable content-addressed object under
   `.target-book-objects/<sha256>.json`.
3. Atomically replace the engine-visible target path with those exact bytes.

For LONG and CARRY, the strategy host then appends a hash-chained target
capture containing the event identity, strategy identity, content hash,
object path, and target keys. Exodus does not run through that host: after it
publishes, its own daemon writes the current path, immutable-object path, and
content hash to the `exodus_cycles` row. It writes no target-capture tape. The
immutable object keeps the exact published bytes addressable; the active path
remains the low-latency latest-value interface.

The Rust follower owns entry quoting, working-order cancellation, convergence,
resizing, expiry, and exits. It must cancel an owned resting entry whenever its
target disappears or expires before considering new work.

## Strategy state and scheduling

LONG and CARRY are plugs on
[`strategy_host.py`](../liquidity_migration/strategy/strategy_host.py). The host
owns their public WS caches, deterministic event tapes, deadlines, price-touch
wakes, cycle health, target captures, and shutdown. It runs them on confirmed
bars, semantic engine-account changes, strategy deadlines, price crossings,
and a bounded idle floor.

Exodus has its own `ExodusProducerDaemon`. It polls CARRY's durable event tape
on a 60-second floor, shortens the wait for its next cover clock, and writes its
own cycle health. It has no public-market cache or hosted event/target-capture
layer.

The engine rewrites telemetry every five seconds, but telemetry-only changes do
not wake a strategy cycle. The host compares a canonical projection of account
identity, positions, blockers, `may_open`, configured strategies, and engine
version. This prevents heartbeat cadence from becoming an expensive feature
recompute loop.

LONG persists its own requested-book state. Request time and fill time are
different fields: hold and stop-decay clocks begin only when the engine reports
an attributable fill. Each signal generation is submitted at most once, and an
unfilled request retains its original validity deadline across later cycles.
Live and research decisions both call the pure typed reducer
`decide(DecisionInput, PriorState, StrategyConfig) -> DecisionOutput` in
[`long_contract.py`](../liquidity_migration/rules/long_contract.py). That reducer
owns the signal, sizing, entry, stop-decay, and time-exit rules. It emits no
take-profit.

CARRY persists the equity anchor for each daily decision before publication.
A restart therefore cannot resize the same decision from mark-to-market noise.
For a pre-settlement exit, CARRY parses a typed venue/account input, calls a
pure planner, then appends the planned event to a hash-chained tape. Only after
that append is durable does it transition and persist its private exit mask.
The independent Exodus producer consumes that tape, owns its
consumed-event and open-short state, and publishes its own book. CARRY never
updates Exodus state or target bytes. CARRY durably appends the event before
advancing its reduced book; that event rebuilds the exit mask if its separate
state write fails. Exodus makes new exposure and tape consumption durable
before publication. A due cover deliberately reverses that order: Exodus
publishes an explicit zero, then removes the open record only after its engine
sleeve conclusively has no position or working entry.

## Effective configuration

All three producers resolve typed effective configuration before planning and
report field-level provenance for the resolved fields. LONG and CARRY bind
their rule, sizing, execution, cycle, exchange, target-book, heartbeat, account
identity, and invocation inputs. LONG also binds its data root, cycle mode and
interval, event debounce, ticker reconciliation and stale tolerance, engine
account stale tolerance, evidence capture, state, and transition paths. CARRY
also binds its data root, candidate
projection, event tape, cycles dataset, sizing-anchor path, and early-exit state
path. Exodus binds its
registered profile and rule, execution environment,
event, target-book and heartbeat paths, account identity, invocation, and entry
leverage. Operational profiles are the sole live LONG/CARRY notional sizing
source; Exodus target quantity comes from a complete CARRY event. LONG and
CARRY live commands accept no per-field sizing flags.

## Account truth and producer gate

[`engine_account_health.py`](../liquidity_migration/runtime/engine_account_health.py)
is the only Python view of account state. It opens one stable heartbeat
snapshot and requires:

- the configured realm;
- the exact expected venue account user ID;
- finite positive equity and finite available margin;
- a recent venue-observation wall timestamp;
- strict, non-duplicated position rows.

LONG and CARRY read it again at the publication boundary. A long feature build
or market-data delay can therefore never size additions from an account sample
that aged past the configured bound during computation. Exodus takes one recent
snapshot immediately before it plans and publishes. Unknown account state
blocks additions and resizes; Exodus leaves new tape events unconsumed until
health returns. CARRY may still publish a strictly removal-only,
already-expired book derived from the prior verified target bytes, and Exodus
may continue publishing due covers while retaining their state until the
engine conclusively reports flat.

Heartbeat positions and entry blockers are strategy-attributed. Producers must
filter by their exact configured Rust sleeve name; account-global positions
cannot prove sleeve ownership.

## Rust risk and durability

Risk admission lives only in `engine-risk`. The engine enforces the
equity-anchored envelope, gross and margin caps, leverage and
instrument rules, book age, account identity, working-order ownership, and
venue-native stop discipline. Reduction-only work is never blocked by a growth
cap.

Every order command and execution transition is written to the Rust WAL. For a
contiguous group of sibling placements, the engine validates and reserves risk
in deterministic order—including cumulative opposite-side pending quantity—
appends every accepted `OrderSent` record, starts one durability barrier, and
dispatches the group while that barrier runs. A later placement group and every
private order update settle the outstanding barrier first. The venue adapter
owns wire scheduling: Bybit
overlaps distinct-symbol chains over a ten-connection warm pool and preserves
same-symbol order, while the default preserves serial order for nonce-sensitive
venues. A whole-position stop is tied to the fill that grows or crosses the
position and can only stay equal or tighten on same-side growth. Cancels,
amends, and stop changes
flush a pending placement group first, so action order is preserved. Fills
carry venue execution IDs and are idempotent by that identity. Private and
public transport phases have explicit time and size bounds; malformed or stale
input fails closed to no new exposure.

If a blank-client-id venue stop flattens a position with one target-book owner,
the engine writes a durable per-sleeve target latch before the foreign-fill
barrier. The follower restores it immediately and after restart, so stale
nonzero bytes cannot reopen the symbol. The producer's explicit zero target
clears the latch; a later nonzero target can then open normally. The stop fill
remains foreign for attribution and accounting.

## Replay and research boundary

One recorded, hash-chained strategy-event tape carries the Python decision
input, prior state, effective-config identity, quote, instrument rule, and
account snapshot. Python verifies the tape and produces the recorded decisions,
live state, and exact target-book bytes. Rust verifies the same event payload,
parses those target bytes, and compares the resulting planner, risk, and WAL
events with the fixture. Drift anywhere across that seam is a failing refactor
fence.

The hourly LONG runner is a diagnostic signal replay. It calls the shared
reducer but hourly bars cannot reproduce the live ticker wake, minute entry
path, Rust fills, or target dead-band resizes. The minute live-physics runner
also calls the reducer and adds causal one-minute wakes, fill-anchored clocks,
current resize and capital-reference deadbands, fees, funding, and no
take-profit. Entry and stop tests use mark-price minutes; fills, accounting,
and resizes use traded-price minutes; funding value uses the exact settlement
minute's mark. Minute OHLC still cannot establish tick order, queue position,
historical instrument-step rules, other-sleeve reservations, or exact live
fills; its output is a minute execution bound.

Historical Python runners report their declared cost and data assumptions.
They do not emulate the live account owner and cannot establish venue execution
quality. Demo and funded evidence comes from Rust WAL/fill/account artifacts.
Promotion standards and caveats are
in [`research/governance.md`](research/governance.md).

## Verification

Use the narrowest relevant tests first, then the repository gates:

```text
scripts/dev.sh doctor
scripts/dev.sh check
cd engine && cargo test --workspace --all-targets
cd engine && cargo run --release -- bench
```

Generation-changing rollouts fetch and pin the target commit, stop downstream
producers before both account owners, persist the boot fence, require the fleet
quiescent, then install and activate the target. The outgoing generation does
not need a release marker or account-inventory capability, which keeps the
upgrade path compatible with installed releases that predate those artifacts.

Activation is a two-authority commit protocol. Before the candidate topology
starts, a root watchdog maintains a six-second tmpfs permit bound to the boot,
rollout PID/start ticks, release commit, and five installed artifact hashes.
It pins one inode and renews under an exclusive lock; launchers use shared locks,
and an inode/path mismatch revokes without recreating a deleted permit. Trusted
launchers also reject writable critical checkout ancestry or Git metadata before
they trust the commit; deploy makes the same check before using Git. They
supervise their children and poll that ten-field
permit every two seconds. Only after the complete topology is enabled, active,
verified, and synced does
rollout atomically install the persistent six-field completion receipt and then
retire the watchdog and permit. Thus process death or power loss before the
receipt yields a stopped generation; receipt-permit-receipt reads make the
handoff linearizable. After it, reboot authorization depends only on the
digest-bound receipt and not on ephemeral `/run` state.

The optional manual Bybit inventory command discovers every advertised linear
settlement coin, retains USDT and USDC,
and strictly paginates linear, inverse, and option positions; unified-wallet
assets and liabilities; and linear, inverse, option, and spot orders. Mainnet
also reads spread orders, both RFQ quote roles and inquiries, active
venue-native TWAP, chase, iceberg, and POV strategies, and cross-account asset
and bot categories. It requires two full scans with stable scope.
These are several venue reads, not an atomic snapshot, and Bybit cannot
enumerate every bot instance; the funded UID therefore also requires a
dedicated-account operator acknowledgement. Unknown and delisted rows stay
visible by name. A configured-symbol scan is not a flat proof and never
authorizes state replacement.
