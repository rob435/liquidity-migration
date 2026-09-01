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
scripts/ops.sh deploy MODE [ARGS...]
```

`start`, `stop`, `restart`, `flatten --execute`, profile rendering, and deploy
modes mutate the host. A unit name without the
`liquidity-migration-` prefix is qualified automatically.

Direct start, stop, or restart of funded units is not the funded rollout
contract. Use the exact-commit operational rollout for a funded generation.

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

## Release boundary

Systemd does not execute a mutable checkout command directly. The trusted
launcher verifies a root-owned release marker that binds:

- the exact git commit;
- engine and signal-worker binary hashes;
- the authorized runtime launcher;
- the Telegram control helper and sudo policy; and
- the Telegram bot boundary.

The short-lived activation permit binds that release to the rollout process.
The persistent activation receipt allows ordinary service restarts only for
the installed generation. A changed checkout, binary, launcher, helper, or
receipt is refused.

The engine binary and signal worker are installed outside the checkout under
`/opt/liquidity-migration-engine/bin`. Runtime config and credentials remain
root-owned under `/etc/liquidity-migration`. State remains under `/var/lib` and
is not recreated by a code checkout.

## Deployment modes

The deploy entry point accepts:

- `verify`: read-only topology and artifact checks;
- `install`: fetch, build, install, and prepare a stopped generation;
- `activate`: activate and verify an installed generation;
- `staged --profile operational`: prefetch, install, activate, and verify in
  one remote session;
- `rollout --profile operational`: exact-commit funded generation change with
  topology snapshot, ordered stop, install, activation, and verification;
- `stop-mainnet`: stop the funded realm; and
- `disarm-mainnet`: remove funded runtime authorization through its explicit
  owner boundary.

For funded generation changes, use a full commit SHA:

```sh
EXPECTED_COMMIT=<40-lowercase-hex> \
  scripts/ops.sh deploy rollout --profile operational
```

Rollout prefetches and compiles before it stops the incumbent. It then stops
the validated ordered union of the installed and candidate manifests,
downstream units before account owners, proves the managed fleet quiescent,
installs the exact commit, activates owners before dependants, and verifies the
resulting topology. This transition inventory covers retired producer units
even though they are absent from the candidate manifest. A failure before
checkout mutation can restore the prior topology; a failure after authority
changes leaves the managed fleet stopped for explicit recovery.

Install builds the Rust release before generating strategy config. Rust
renders the exact native directional blocks and, for mainnet, the maker block.
The installed TOML is atomically replaced and checked against a second render.

## Native state takeover

Install handles each realm while its owner is stopped:

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
`EXPECTED_ENGINE_ACCOUNT_USER_ID`. Demo uses its demo account credential;
mainnet prefers the optional globally read-only attestor and otherwise uses the
existing execution credential inside the Rust inventory type, which exposes no
order or account-mutation method. An exact retry is a no-op. A partial bundle,
another account, changed source bytes, or a conflicting checkpoint stops
installation.

Before it snapshots or stops any unit, an armed rollout validates the selected
credential as a single-link `root:root` mode-`0600` file, runs the exact
candidate's read-only identity command, and matches the authenticated Bybit UID
to the engine's expected account. Missing or mismatched credentials therefore
leave the incumbent fleet untouched.

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
their resolved off state. Resume restores those switches, submits the matching
durable permissions, and requires the current activation receipt. Exodus
resumes with the realm when the committed config permits entries.

Mainnet resume cannot arm an account. It requires the funded engine already
running under the separately owner-managed `REAL_MONEY=true` credential and a
matching activation receipt.

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
document only when `--execute` is explicit.

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

Status verifies the exact commit, clean checkout, release marker, binary
digests, launcher and receipt, exact systemd inventory, config render,
WAL/native state, spools, runtime identities, heartbeats, timers, and active
realm policy. Do not replace a failed check with an interpretation of an old
doc or an old rollout receipt.

Liveness separately checks current heartbeats, account and signal freshness,
independent LONG and CARRY cycle completion, spool publication, WebSocket and
REST fallback state, systemd memory pressure, WAL growth and storage, feed
readiness, controls, timers, host clock, latches, and native reducer errors.
Symbol entry blockers remain trading state and do not page. A systemd `active`
state alone is not proof that either sleeve is producing decisions.

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
- A failed rollout after authority changes stays stopped. Repair the named
  check and rerun the exact-commit flow.
- A funded stop or disarm is not reversed by a demo command, resume action, or
  ordinary service restart.

See [`notifications.md`](notifications.md) for alerts and
[`engine.md`](engine.md) for the durable runtime contract.
