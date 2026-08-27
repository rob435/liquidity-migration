# Operations

The Rust engine is the only account owner. Python producer units have no venue
credentials and can only publish absolute target books. Run operator commands
through `scripts/ops.sh`; direct module invocation is for tests and development.

## Command surface

| Command | Effect |
| --- | --- |
| `status [ARGS...]` | Read-only VPS and release verification |
| `units` | List fleet units and timers |
| `logs UNIT [LINES]` | Read one unit's journal |
| `start\|stop\|restart UNIT...` | Change explicitly named units |
| `equity [ARGS...]` | Render descriptive research curves |
| `research-refresh [ARGS...]` | Plan or run the append-first research workflow |
| `real-money preflight` | Read-only arming report; never prints credential values |
| `real-money render-profile` | Render the non-secret operational risk profile; writes only with `--execute` |
| `flatten --environment demo\|mainnet` | Preview known-position reduction; `--execute` stops producers and publishes zero targets |
| `deploy MODE [ARGS...]` | Install, activate, stage, roll out, stop mainnet, or atomically disarm mainnet |

Mutating commands require explicit targets. Preview is the default where the
command supports it. `REAL_MONEY=true` exists only in the root-owned funded
credential file and remains the sole funded arming switch.

## Fleet boundary

Each realm has one engine process, one Rust state directory, one exact venue
account ID, and one account lease. The engine owns private REST/WS, orders,
fills, positions, stops, reconciliation, risk, and WAL recovery. LONG, CARRY,
and Exodus each have a distinct target-book source and Rust sleeve name.

Producer units require:

- their engine-visible target-book path;
- LONG's durable requested-book state path where applicable;
- the engine heartbeat path;
- the exact expected venue account user ID;
- a non-secret operational profile and candidate universe where configured.

They explicitly reject venue keys and `REAL_MONEY`. Liveness observers receive
only a root-generated Telegram projection, never funded venue credentials.

## Release verification

Install and rollout bind the checkout commit, release marker, binary SHA-256,
engine heartbeat version, realm, and exact venue account ID. Cargo builds use
the pinned toolchain and `--locked`; CI pins third-party action revisions.

A generation-changing rollout must prove venue-global flatness before it stops
units, changes checkout, quarantines targets, or replaces producer state. The
current venue abstraction cannot enumerate unknown or delisted symbols, so it
cannot produce that proof. Funded and `--require-flat` rollouts therefore abort
with status 3 before mutation. This is an intentional safety block.

Do not replace it with a configured-symbol scan. Such a scan can omit the exact
residual exposure a generation boundary must protect.

## Flatten semantics

`flatten --execute` is a de-risking tool, not a reset or flat attestation. It:

1. requires a running Rust engine and readable heartbeat;
2. stops producers;
3. publishes explicit zero targets for observed configured-symbol positions;
4. waits for those observed positions to disappear;
5. leaves producers stopped and producer state unchanged;
6. exits 6 with `global_flat=unproven` even after known positions close.

Unknown or delisted exposure may remain. Verify the venue independently before
any manual restart or state intervention.

## Real money

Before arming, verify all of the following:

- the funded API key is contract-trading only, withdrawal-disabled, and IP
  allowlisted;
- the funded engine env contains the exact venue account user ID;
- the Rust toolchain and release binary match the pinned release;
- the operational profile is the exact render of reviewed dials;
- target, WAL, heartbeat, and data roots are real, disjoint local paths with
  appropriate ownership;
- Telegram delivery and the off-box dead-man signal are provisioned;
- the venue account is independently verified against the intended realm,
  position mode, margin mode, open orders, and positions.

Arming is a host-side operator act. A repository commit cannot arm funded
trading.

## Incident rules

- `may_open=false`: leave the engine running for reductions, inspect its WAL and
  reconciliation report, and use the explicit Rust reconcile-clear workflow
  only after resolving the discrepancy.
- Stale/unreadable heartbeat: stop producers first. Do not infer flatness or
  publish an empty book.
- Producer failure: the last valid target remains active until its own validity
  rules expire; engine exits and stop protection continue independently.
- Engine/private-feed failure: the engine exits for supervised recovery. Do not
  start a second writer or remove a lease file.
- Credential uncertainty: disarm and stop the funded fleet, then rotate the key
  through the venue. Never paste credentials into logs or chat.

Systemd topology, users, environment files, and credential projections are
listed in [`deploy/systemd/README.md`](../deploy/systemd/README.md). Engine
recovery and risk contracts are in [`engine.md`](engine.md).
