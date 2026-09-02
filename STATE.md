# Operational State

Exact host truth comes from `scripts/ops.sh status`. Repository files describe
the generation a deploy installs; they do not prove that a host is running
that generation. This change does not deploy, arm, disarm, pause, resume, or
flatten either account.

## Repository generation

### Account owners

The demo and mainnet accounts each have one Rust engine. The engine is the sole
private-credential and order authority for its account. It owns the
account-writer lease, authenticated feeds and REST, WAL, fill attribution,
reconciliation, leverage, venue stops, risk admission, runtime controls,
heartbeat, and closed-trade log.

The funded engine starts only while the owner-controlled
`REAL_MONEY=true` switch is present in the funded credential file and funded
preflight passes. Git configuration cannot arm money.

Stopped native-state takeover and credential-wide flatness checks run through a
Rust inventory type with no order or account-mutation method. Mainnet prefers
the optional globally read-only attestor credential and otherwise uses the
existing execution credential from the funded environment file.

### Directional decisions

Each realm has one credential-free Rust `signal-worker`. It acquires Bybit and
Binance public inputs, persists its normalized history and multi-output
transaction, and publishes immutable sequence-numbered observations. Its
checkpoint persists the random source generation used by the LONG and CARRY
namespaces. Restart and ordinary checkpoint compaction keep that generation and
its sequences. Only a genuinely new state root creates a new source at sequence
one. The engine puts each observation in its WAL before waking a strategy.

The worker derives the tradable universe itself, on its hourly instrument
cadence, from the realm venue's instrument list and the public ticker page:
every trading USDT crypto perpetual is tradable; LONG's eligible set is the top
120 by 24-hour turnover with a $2M turnover floor and a 30-day listing age,
CARRY's the top 150 with a 7-day age; a member stays until it falls past rank
160 (LONG) or 200 (CARRY). Those ranks, floors, ages, and the stablecoin
exclusions are dials in `configs/signal-worker.<realm>.json`; nothing is frozen
and no file is reviewed by hand. The worker also reads the LLM entry gate's
candidates file every minute and hands each new publication to LONG as one
observation. The worker keeps one persistent Bybit WebSocket actor and separate
bounded REST lanes for instruments, funding, ticker fallback, candle repair,
and optional whale data. A disconnect opens a new source epoch, clears
transient ticker state, and reconnects forever with capped backoff. Exact
subscription replies separate topic faults from transient and request-wide
faults. Quarantined topics retry on the same healthy socket every minute;
accepted topics stay live while REST fills missing ticker fields and candle
intervals. Only accepted market data resets the retry and idle clocks. Each
ticker field keeps its own receipt clock. Cold history is acquired in bounded
chunks while heartbeat and shutdown remain responsive. Accepted lookbacks and
response grids place a hard row ceiling on each fetch. Kline, funding, and
whale lanes retain one job at a time and wait for its durable commit result
before fetching the next. Venue network, normalization, range, and
history-rewrite faults pause and retry only that lane before mutation.
Sequence, state, spool, serialization, and disk faults stop the process. A
bounded input journal absorbs frequent source deltas and compacts to an atomic
checkpoint. Current outputs coalesce while the engine is behind; ordered
lifecycle and scorer catch-up rows keep separate spool quotas.

The engine runs three native directional reducers in fixed WAL order:

1. `carry_native` (`carry`)
2. `long_native` (`long`)
3. `exodus_native` (`exodus`)

Mainnet keeps `quoter` (`maker_canary`) in the fourth durable slot (strategy ID
3) with quoting disabled. The slot stays present because fill attribution and
strategy identity are indexed by order.

CARRY publishes its pre-settlement handoff as an internal durable event.
Exodus consumes that event in the same engine and owns its checkpoint. The
current fleet manifest contains only the two account owners, two public-signal
workers, and their observation and operations units.

### Rule and sizing inputs

The registered profiles are:

| Sleeve | Rule |
| --- | --- |
| LONG | `configs/long_native_v12.json` |
| CARRY | `configs/lane2_carry_hold_v7.json` |
| Exodus | `configs/lane2_exodus_short_v1.json` |
| maker canary | `configs/lane2_toxic_flow_quoter_v1.json` |

One operational profile (`configs/operational.json`, schema 3) serves both
realms; deploy renders it once from the dials in the funded credential file and
installs the same bytes for each engine and worker. It sets LONG to 6.0 times
its base sizing and CARRY to 3.0 times, with entry leverage capped at 5.0.
Exodus takes the exact abandoned CARRY quantity and has no independent notional
multiplier. Account risk is shared; there is no per-sleeve capital allocation.
The capital reference follows each account's own equity, floored at $100, and
every cap is a ratio of it: gross 5 times, initial margin 1 time, rolling-loss
share 0.1. Nothing is pinned to a dollar figure on either realm.

`deploy/sleeves.env` enables demo LONG and CARRY entries. A host override may
only narrow either permission to off. A disabled entry permission does not
stop signals or exits and does not flatten. Mainnet arming is separate from
these repository ceilings.

### Risk and exits

The engine applies account-wide gross, margin, leverage, instrument, quote-age,
account-view-age, and stop-loss limits, and the rolling-loss trip: when its own
closed trades have lost a tenth of the capital reference, net of venue fees,
inside any 24 hours, it refuses entries and growth until those trades age out.
Growth can be refused; genuine reductions continue. A restart does not clear
the trip. A close the venue itself started (stop, take-profit, liquidation,
auto-deleverage) is charged to the sleeve that held the position and counts in
the window; a close the venue gives no reason for is not the engine's and still
latches it. Each opening order carries a venue-native stop contract.
The engine tracks every required top-of-book topic separately. Forty-five
seconds without that symbol's promised L1 snapshot triggers a same-socket
re-subscription while healthy symbols continue.

LONG owns signal admission, sizing, stop decay, cooldown, and time exit. It has
no take-profit. It has two entry triggers: the native feature batch, and the
LLM entry gate's judged events (score at least 6 on 4/12/24-hour triggers, core
ranks 1-10 and wide ranks 11-30), which enter at market through the same
sizing, stop, and time exit and carry their own order-log tag. CARRY owns daily
sizing anchors, settled and pre-settlement exits, next-day drop exits,
admission, and the $6 entry plus $1/5% resize boundaries. Exodus covers on its
registered settlement clock and retains a durable retry until its attributed
exposure and owned opening work are flat. LONG and CARRY keep their one-minute
entry-admission budget in their checkpoint; extra market, retry, control, or
boot wakes cannot reset it. Exodus keeps a temporarily blocked handoff pending
through account-health recovery. A CARRY candidate consumes an admission slot
only when the shared planner can emit its opening order; missing planner facts
remain retryable. An explicitly invalidated private account view and a future
durable opening timestamp block growth but do not block reductions. The maker
cancels recovered opening quotes and drains its attributed inventory when
quoting is globally disabled or that symbol is retired. A configured enabled
symbol is not flattened. Refused drains retry on a bounded timer.

### Runtime controls

Pause and resume submit one durable entry-permission command per directional
sleeve. Pause blocks entries and growth while public observations, checkpoints,
settlement clocks, cancels, and exits continue.

A control request the engine will never accept — unreadable, from another
schema generation, or semantically stale — is quarantined beside the spool as
`<name>.rejected` and reported as rejected; it cannot stop or restart-loop the
engine and never blocks a later command.

Flatten first durably disables entries, then submits one replayable flatten
request per directional sleeve. Completion requires no venue position, no
attributed cover, no owned opening work, and no pending request. The signal
worker remains running throughout.

### State takeover

Deploy installs the Rust binaries with all units stopped, renders the exact
native configs from registered JSON and the installed operational profile, and
runs account-bound state takeover before starting the fleet.

The importer holds both the WAL and authenticated account leases. It requires
the exact account ID and strategy order. It translates the complete LONG,
CARRY, and Exodus source bundle through strict native codecs, preserves pending
CARRY-to-Exodus events, and records source and canonical bundle hashes. Exact
retry is a no-op; partial or conflicting state is refused. A genuinely empty
generation receives canonical empty checkpoints.

### Observability

Liveness binds each engine and worker to the installed release and exact inputs,
then reports producer, LONG, CARRY, spool, transport, and memory faults
independently. LONG and CARRY must each complete within three configured
cadences; their current feature and action horizons remain separate from the
CARRY scorer catch-up cursor. Memory warns at 75% of a finite systemd limit and
becomes critical at 90%. Any blocked spool class, malformed class quota, or
class usage at its file cap or byte soft limit is independently critical even
when aggregate spool usage is below its cap. The checker binds the exact Rust
heartbeat schema, process ID, installed feature-contract hashes, and global-to-
class spool totals.

Fleet liveness also pages when an engine's heartbeat says its rolling-loss
trip is on. A separate host scope, independent of the fleet, watches the two
market recorders (Bybit and Binance), the hourly market-tape upload to Google
Drive, the six-hourly state backup to Google Drive, disk, and the host clock.

The market recorders are the standalone `market_tape` package, one config file
per venue under `deploy/capture/`; the Binance recorder exists in this
generation and both recorders are running.

Trade notifications derive entries from fresh engine-attributed venue
positions and exits from the engine trade log. Target files are takeover
evidence only and are not a notification or live-decision authority.

## Evidence state

The native reducer fixtures establish decision parity and
restart behavior. They do not establish profitable execution. Research results
remain subject to the Progressive Evidence Model in
`docs/research/governance.md`.

The maker canary remains disabled. Neither loadability nor a passing replay
authorizes deployment.

Authenticated venue state and the engine WAL are required for claims about
live positions, fills, fees, P&L, or flatness. Projections and target decisions
are not account evidence.

Venue-confirmed trade accounting binds the complete WAL family, the commit
and config hash each Boot record names for the fill it recorded and the order
it sent, exact execution and cash rows, and the authenticated account-history
window. The expected commit is the one the deploy recorded and the expected
config hash is that of the retained rendered config; both are independent of
the graded log. Missing or contradictory evidence withholds the
venue-confirmed label. Logs written by builds before boots carried a commit
cannot reach it.

## Deployment status

The host runs `f17719d1` (deployed 2026-09-02 21:04 UTC): both engines and
both signal workers report that commit, and both market recorders run the
tiered configs under `deploy/capture/`. The host is otherwise frozen except for
emergencies ([operations.md](docs/operations.md) §Host freeze): forward days of
tape and of Lane-2 evidence are the scarce resource, and the owner chooses when
the next deploy runs.
Run:

```text
scripts/ops.sh status
```

to read the installed commit, armed state, unit states, and heartbeat ages.
Use the exact-commit deploy (`scripts/ops.sh deploy`); do not copy templates
onto a running account by hand.
