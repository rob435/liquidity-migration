# Operations

Use [`scripts/ops.sh`](../scripts/ops.sh) as the operator entry point. The
current host state comes from `scripts/ops.sh status`; repository docs describe
the contract and do not assert that a rollout happened.

The live topology is two Rust processes per realm:

- `liquidity-migration-signal-worker-{demo,mainnet}.service` collects public
  data and publishes normalized observations; and
- `liquidity-migration-engine{,-mainnet}.service` owns the private account,
  WAL, reducers, controls, risk, and orders.

Python services are observers, research jobs, notification handlers, or data
capture. They do not decide a live directional position.

## Operator commands

```text
scripts/ops.sh status
scripts/ops.sh units
scripts/ops.sh logs UNIT [LINES]
scripts/ops.sh start UNIT...
scripts/ops.sh stop UNIT...
scripts/ops.sh restart UNIT...
scripts/ops.sh attest-flat --environment demo|mainnet
scripts/ops.sh flatten --environment demo|mainnet [--reason TEXT] [--execute]
scripts/ops.sh real-money preflight
scripts/ops.sh real-money render-profile [--execute --output PATH]
scripts/ops.sh deploy [deploy|verify|stop-mainnet|disarm-mainnet]
```

`start`, `stop`, `restart`, `flatten --execute`, profile rendering, and deploy
modes mutate the host. A unit name without the
`liquidity-migration-` prefix is qualified automatically.

## What runs

[`deploy/fleet_manifest.tsv`](../deploy/fleet_manifest.tsv) is the machine
inventory. It defines each unit, realm, lifecycle order,
activation policy, dependency, health evidence, and artifact. Deployment and
liveness both consume it; a stray service is not made legitimate by existing
on the host.

Current account owners:

| Realm | Owner | Public signal worker | Engine state |
| --- | --- | --- | --- |
| demo | `liquidity-migration-engine.service` | `liquidity-migration-signal-worker-demo.service` | `/var/lib/liquidity-migration-engine` |
| mainnet | `liquidity-migration-engine-mainnet.service` | `liquidity-migration-signal-worker-mainnet.service` | `/var/lib/liquidity-migration-engine-mainnet` |

The signal worker has its own state and heartbeat under the public runtime
tree. Each realm has distinct signal and control spools. Units absent from the
manifest are disabled and removed during installation.

## Release layout

The engine binary and signal worker are installed outside the checkout under
`/opt/liquidity-migration-engine/bin`. Runtime config and credentials remain
root-owned under `/etc/liquidity-migration`. State remains under `/var/lib` and
is not recreated by a code checkout. Root SSH access and the pushed `main`
branch are the security boundary: the deploy installs exactly the commit it is
given, and that commit must already be on `origin/main`.

## Deployment modes

The deploy entry point accepts:

- `deploy`: fetch and check out the exact commit, build the Rust release,
  install binaries, units, and rendered configs, run state takeover if it is
  still pending, and restart the fleet — worker first, then the account owner,
  then the downstream units, waiting for a fresh heartbeat at each step. The
  funded realm starts only while `REAL_MONEY=true` is present in the funded
  credential file; otherwise its units stay stopped;
- `verify`: read-only fleet summary — installed commit, arming state, unit
  states, heartbeat ages, and disk;
- `stop-mainnet`: stop the funded realm; and
- `disarm-mainnet`: stop the funded realm and set `REAL_MONEY=false` in the
  credential file.

```sh
EXPECTED_COMMIT=<40-lowercase-hex> scripts/ops.sh deploy
```

`EXPECTED_COMMIT` defaults to the local checkout's `origin/main` tip. The host
refuses a commit that is not on `origin/main`. Rust renders the exact native
directional blocks and, for mainnet, the maker block; the installed TOML is
atomically replaced.

## Native state takeover

Deploy handles each realm while its owner is stopped:

1. `verify-native-strategy-state` accepts an already complete native WAL and
   skips stopped-state import.
2. A truly empty WAL and absent takeover sources are seeded with
   `initialize-native-strategy-state`.
3. A generation without native checkpoints must provide the complete LONG,
   CARRY, and Exodus source bundle. Each strategy imports through its Rust
   strict codec.
4. Final verification checks strategy order, checkpoint identity and payload,
   source provenance, and WAL tail.

Takeover takes both the WAL lock and authenticated account lease and requires
`EXPECTED_ENGINE_ACCOUNT_USER_ID`. Each realm uses its own credential file.
An exact retry is a no-op. A partial bundle, another account, or a conflicting
checkpoint stops installation.

The seven persistent stopped-state source roles are:

| Sleeve | Source roles |
| --- | --- |
| LONG | `state` |
| CARRY | `early_exits`, `sizing_anchors`, `target_book` |
| Exodus | `carry_events`, `identity`, `state` |

Only `carry_early_exits.json` and `carry_presettlement_events.jsonl` may be
absent: deployment supplies their canonical empty bytes to the strict importer
because the retired producers create those files only after the first event.
Every other source is required, and a present malformed file is refused.

Deployment also generates the Exodus `legacy_paths` source from the exact
installed event, target, and engine-heartbeat paths. These are takeover inputs
only. The running native reducers do not read them.

The reviewed candidate-universe file is copied byte-for-byte from an incumbent
producer source path only when the native path is absent. Its schema, realm,
endpoint, populations, counts, self-hash, ownership, and mode are checked both
before and after the atomic copy. An existing native file wins and must pass the
same checks.

## Runtime pause and resume

Pause is a durable engine control, not a process stop. The helper submits
`entries_enabled=false` for LONG, CARRY, and Exodus and waits for the engine
heartbeat to acknowledge every sleeve. The signal worker and engine stay
running so exits and settlement clocks continue.

The trusted helper actions are:

```text
pause-demo
resume-demo
pause-mainnet
resume-mainnet
status-fleet
```

Demo pause also saves the owner-controlled LONG/CARRY entry switches and writes
their resolved off state. Resume restores those switches and submits the
matching durable permissions. Exodus resumes with the realm when the committed
config permits entries.

Mainnet resume cannot arm an account. It requires the funded engine already
running under the separately owner-managed `REAL_MONEY=true` credential.

## Flatten

Preview first:

```sh
scripts/ops.sh flatten --environment demo --reason "operator request"
```

Execution:

```sh
scripts/ops.sh flatten --environment demo --reason "operator request" --execute
```

The flatten script requires a fresh exact-realm engine heartbeat. It durably
disables entries for LONG, CARRY, and Exodus, submits one replayable flatten
request per sleeve, and waits for all of these engine facts:

- no attributed directional position;
- no owned directional opening order;
- none of its flatten requests remains pending; and
- all three entry permissions remain false.

It leaves entries paused. It deliberately reports venue-global flatness as
unproven because the heartbeat is scoped to engine-attributed inventory.
Follow with the authenticated two-scan proof:

```sh
scripts/ops.sh attest-flat --environment demo
```

Only credential-wide venue evidence can support a global-flat claim or a later
state reset.

## Real-money authority

`REAL_MONEY=true` in the root-owned mainnet credential file is the only funded
arming switch. Repository config, a registered research rule, service enable
state, and a deploy command do not substitute for it.

`scripts/ops.sh real-money preflight` is read-only. It reports missing account,
credential, profile, release, and topology requirements. Operational profile
rendering reads the owner dials and writes a reviewed account-wide risk
document only when `--execute` is explicit. The funded credential file carries
two dials, both ratios:

| Dial | Default | Meaning |
| --- | --- | --- |
| `RM_CARRY_STOP_LOSS_FRACTION` | 0.35 | venue-native stop distance on CARRY entries |
| `RM_ROLLING_LOSS_FRACTION` | 0.10 | share of the capital reference the engine's own closed trades may lose, net of fees, inside any 24 hours before it refuses new entries |

A changed dial takes effect at the next deploy, which re-renders the profile.
The demo profile carries the same rolling-loss share in
`configs/operational.demo.json`; against its pinned $250,000 reference that
limit is $25,000.

The signal worker never receives private credentials. The engine service gets
only the credential family for its own realm. The notification and liveness
services get no order credential.

## Verification

After any intended host change, run:

```sh
scripts/ops.sh status
scripts/ops.sh units
scripts/ops.sh logs engine.service 200
scripts/ops.sh logs signal-worker-demo.service 200
```

Status reports the installed commit, arming state, every manifest unit's
active state, heartbeat ages, and disk. Do not replace a failed check with an
interpretation of an old doc.

Liveness separately pages on an inactive manifest unit, a stale or unreadable
heartbeat, an engine that reports it cannot open positions, an engine whose
rolling-loss trip is on, low disk, a stale off-box backup stamp, and (in one
scope per box) an unsynchronised host clock.
A systemd `active` state alone is not proof that either sleeve is producing
decisions.

## Venue-confirmed trade accounting

The account-history capture is authenticated but GET-only. Mainnet uses the
separate `BYBIT_ATTEST_*` key by default; `--credential-set execution` selects
the engine key explicitly. The capture window must have ended at the venue and
remain inside Bybit's two-year account-history boundary. The capture checks the
authenticated user and venue time before and after all three histories finish.

```sh
python scripts/research/capture_bybit_account_history.py \
  --realm mainnet \
  --start "$TRADE_START_UTC" \
  --end "$TRADE_END_UTC" \
  --out "$VENUE_CAPTURE"

python scripts/research/reconcile_venue_wal.py \
  --wal /var/lib/liquidity-migration-engine-mainnet/engine.wal \
  --venue-history "$VENUE_CAPTURE" \
  --sleeve long \
  --trade-execution-id "$REGISTERED_EXECUTION_ID" \
  --expected-realm mainnet \
  --expected-user-id "$BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID" \
  --deployment-receipt "$DEPLOYED_ACTIVATION_RECEIPT" \
  --engine-binary "$DEPLOYED_ENGINE_BINARY" \
  --signal-worker-binary "$DEPLOYED_SIGNAL_WORKER_BINARY" \
  --engine-config "$DEPLOYED_ENGINE_CONFIG" \
  --expected-commit "$ROLLOUT_COMMIT" \
  --expected-binary-sha256 "$ROLLOUT_ENGINE_SHA256" \
  --expected-signal-worker-sha256 "$ROLLOUT_SIGNAL_WORKER_SHA256" \
  --expected-config-sha256 "$ROLLOUT_ENGINE_CONFIG_SHA256" \
  --out "$ACCOUNTING_REPORT"
```

The expected identities come from the reviewed rollout record and retained
config bytes, not from the evidence being graded. The receipt is the exact
seven-line `activation.complete` record for the engine, signal worker, launcher,
and control boundary. The supplied engine, signal-worker, and config bytes are
rehashed against independently retained digests. The report says
`venue_confirmed` only when the complete
WAL family, boot config identities, execution and order identities, fill fields,
one-way position path, fees, closed profit and loss, account cash changes, and
every crowd-fee settlement agree. A missing, duplicate, foreign, damaged,
wrong-generation, or out-of-retention row withholds the label.

## Recovery rules

- A stale or unreadable engine heartbeat makes position state unknown. Keep
  entries disabled and inspect the owner before acting on targets or caches.
- A stale signal heartbeat means decision inputs are unknown. Do not stop the
  engine merely because the worker failed; the engine still owns exits.
- A WAL/account mismatch is reconciled from authenticated venue state and WAL
  attribution. Do not edit the WAL or strategy checkpoint by hand.
- A failed deploy leaves the fleet stopped. Repair the named check and rerun
  the exact-commit flow.
- A funded stop or disarm is not reversed by a demo command, resume action, or
  ordinary service restart.

See [`notifications.md`](notifications.md) for alerts and
[`engine.md`](engine.md) for the durable runtime contract.
