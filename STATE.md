# Operational State

Exact host truth comes from `scripts/ops.sh status`. Repository files describe
the generation a rollout installs; they do not prove that a host is running
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

### Directional decisions

Each realm has one credential-free Rust `signal-worker`. It acquires Bybit and
Binance public inputs, persists its normalized history and multi-output
transaction, and publishes immutable sequence-numbered observations. Its
checkpoint persists the random source generation used by the LONG and CARRY
namespaces. A replacement checkpoint gets a distinct source and starts its
sequence at one. The engine puts each observation in its WAL before waking a
strategy.

The engine runs three native directional reducers in fixed WAL order:

1. `carry_native` (`carry`)
2. `long_native` (`long`)
3. `exodus_native` (`exodus`)

Mainnet keeps `quoter` (`maker_canary`) in slot 4 with quoting disabled. The
slot stays present because fill attribution and strategy identity are indexed
by order.

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

The operational profiles set LONG to 6.0 times its base sizing and CARRY to 3.0
times, with entry leverage capped at 5.0. Exodus takes the exact abandoned
CARRY quantity and has no independent notional multiplier. Account risk is
shared; there is no per-sleeve capital allocation.

`deploy/sleeves.env` enables demo LONG and CARRY entries. A host override may
only narrow either permission to off. A disabled entry permission does not
stop signals or exits and does not flatten. Mainnet arming is separate from
these repository ceilings.

### Risk and exits

The engine applies account-wide gross, margin, leverage, instrument, quote-age,
account-view-age, and stop-loss limits. Growth can be refused; genuine
reductions continue. Each opening order carries a venue-native stop contract.

LONG owns signal admission, sizing, stop decay, cooldown, and time exit. It has
no take-profit. CARRY owns daily sizing anchors, settled and pre-settlement
exits, next-day drop exits, admission, and the $6 entry plus $1/5% resize
boundaries. Exodus covers on its registered settlement clock and retains a
durable retry until its attributed exposure and owned opening work are flat.

### Runtime controls

Pause and resume submit one durable entry-permission command per directional
sleeve. Pause blocks entries and growth while public observations, checkpoints,
settlement clocks, cancels, and exits continue.

Flatten first durably disables entries, then submits one replayable flatten
request per directional sleeve. Completion requires no venue position, no
attributed cover, no owned opening work, and no pending request. The signal
worker remains running throughout.

### State takeover

Rollout installs the trusted Rust binaries with all units stopped, renders the
exact native configs from registered JSON and the installed operational
profile, and runs account-bound state takeover before activation.

The importer holds both the WAL and authenticated account leases. It requires
the exact account ID and strategy order. It translates the complete LONG,
CARRY, and Exodus source bundle through strict native codecs, preserves pending
CARRY-to-Exodus events, and records source and canonical bundle hashes. Exact
retry is a no-op; partial or conflicting state is refused. A genuinely empty
generation receives canonical empty checkpoints.

### Observability

Liveness checks bind each engine and worker to the installed release, realm,
input hashes, engine-config hash, universe hashes, sequence progress, and
freshness clocks. The worker heartbeat also exposes its persisted source
generation directly.

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

## Deployment status

This repository generation requires an operational rollout before it becomes
host state. Run:

```text
scripts/ops.sh status
```

to read the installed commit, armed state, units, worker readiness, engine
identity, positions, and current health. Use the exact-commit operational
rollout for deployment; do not manually start individual units or copy
templates onto a running account.
