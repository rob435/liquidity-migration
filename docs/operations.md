# Operations

The Rust engine is the only account owner. Python producer units have no venue
credentials and can only publish absolute target books. The supported fleet
surface is `scripts/ops.sh`; it runs credential-bearing engine controls through
transient systemd services under the matching private environment. Direct
binary or Python module invocation is for tests and development unless a
low-level recovery section explicitly requires it.

## Command surface

| Command | Effect |
| --- | --- |
| `status [ARGS...]` | Read-only VPS and release verification |
| `units` | List fleet units and timers |
| `logs UNIT [LINES]` | Read one unit's journal |
| `start\|stop\|restart UNIT...` | Change explicitly named demo units; funded units may be stopped, but direct start/restart is refused |
| `equity [ARGS...]` | Render descriptive research curves |
| `research-refresh [ARGS...]` | Plan or run the append-first research workflow |
| `real-money preflight` | Read-only arming report; never prints credential values |
| `real-money render-profile` | Render the non-secret operational risk profile; writes only with `--execute` |
| `flatten --environment demo\|mainnet` | Preview known-position reduction; `--execute` stops producers and publishes zero targets |
| `attest-flat --environment demo\|mainnet` | Run the installed adapter's credential-wide, read-only two-scan flatness check |
| `deploy MODE [ARGS...]` | Demo-only hosts may install, activate, or stage; a host with any funded configuration may change or activate a generation only through rollout |

Mutating commands require explicit targets. Preview is the default where the
command supports it. `REAL_MONEY=true` exists only in the root-owned funded
credential file and remains the sole funded arming switch.

Any persisted funded surface closes the direct-generation paths, even while
disarmed: the write credential, read-only attestor credential, mainnet engine
environment or config, producer environment or source, or mainnet Telegram
projection. `deploy install`, `activate`, and `staged` then refuse before
mutation; `ops.sh start` and `restart` refuse funded units. `deploy rollout` is
the only generation-changing activation path. Fail-safe stop,
`disarm-mainnet`, and read-only verification remain available.

The remote `stop-mainnet` and `disarm-mainnet` implementations do not execute
the deployed checkout or its virtual environment. Both first stop, disable,
sync, and verify the funded units selected by the canonical fleet manifest. Stop never opens the funded
credential. Disarm then resolves and verifies the root-owned system Python,
runs it with an empty environment and isolated/no-site flags, and uses an
embedded strict parser to stably read and atomically replace the mode-`0600`
credential with `REAL_MONEY=false`. If that rewrite fails after quarantine,
the funded units remain persistently stopped and disabled.

`engine venues` and `engine strategies` are credential-free discovery commands
for the compiled venue realms, evidence gates, and strategy plugs.

## Fleet boundary

[`deploy/fleet_manifest.tsv`](../deploy/fleet_manifest.tsv) is the one fleet
inventory. It declares every unit's state, realm, lifecycle and stop order,
activation rule, operator policy, dependencies, health check, timer contract,
and runtime input/output artifacts. `ops.sh`, rollout, runtime dispatch, and
tests derive their managed sets from it; adding a unit only to a shell list or
systemd directory is invalid. Activation starts rows in reverse stop order.
Timer services marked `job-now` run once during activation without being
enabled; their timers remain the durable schedule.

Each realm has one engine process, one Rust state directory, one exact venue
account ID, and one account lease. The engine owns private REST/WS, orders,
fills, positions, stops, reconciliation, risk, and WAL recovery. LONG, CARRY,
and Exodus each have a distinct target-book source and Rust sleeve name.

Producer units require:

- their engine-visible target-book path;
- LONG's durable requested-book state path where applicable;
- CARRY's durable pre-settlement event output or Exodus's matching tape input;
- the engine heartbeat path;
- the exact expected venue account user ID;
- a non-secret operational profile and candidate universe where configured.

They explicitly reject venue keys and `REAL_MONEY`. Liveness observers receive
only a root-generated Telegram projection, never funded venue credentials.
Producer source environments remain root-only mode `0600`. Their installed
profile and candidate-universe projections are root-owned, runtime-group
readable mode `0640`; the producer can verify those stable files but cannot
rewrite the reviewed inputs.

## Host network

Bybit is served through CloudFront, and CloudFront chooses which edge answers
from the resolver the DNS query arrives on. The box has no working IPv6
egress, so a query sent over IPv6 is placed on another continent and comes
back naming an edge about 206 ms away. The same resolver asked over IPv4
returns the Singapore edge, about 2 ms away. The box sits in Johor, minutes
from that edge.

The box therefore resolves over IPv4 only and prefers IPv4 addresses:

- `/etc/netplan/50-cloud-init.yaml` lists IPv4 nameservers for `eth0` and no
  IPv6 ones. Netplan merges the `nameservers` list across files rather than
  letting a later file replace it, so an IPv6 resolver has to be absent here;
  adding an IPv4-only file beside it leaves the IPv6 entries in place.
- `/etc/cloud/cloud.cfg.d/99-disable-network-config.cfg` stops cloud-init
  writing that file again, which would restore them.
- `/etc/netplan/99-dns-ipv4-only.yaml` adds the IPv4 fallback resolvers.
- `/etc/gai.conf` gives IPv4-mapped addresses precedence.

`netplan generate` writes the merged result to `/run/systemd/network/`, so
`grep -h ^DNS= /run/systemd/network/*.network` is what the box will actually
use after a reboot — check that, not the netplan files.

A process keeps whatever edge it resolved when it connected, so anything
started before those files were in place holds the far edge until it is
restarted.

`venue_clock_offset_ms` in the engine heartbeat is how this is read back. It
is how far a quote's venue stamp sits from this box's clock at the moment the
quote is read off the socket, so it carries the one-way path: single or low
tens of milliseconds is the near edge, two hundred is the far one.

## Release verification

Install and rollout bind the checkout commit, release marker, binary SHA-256,
engine heartbeat version, realm, and exact venue account ID. Cargo builds use
the pinned toolchain and `--locked`. Rollout compiles the exact target during
prefetch while the incumbent fleet stays live. A locked network fetch populates
the new isolated Cargo cache, then compilation runs offline in a private
network. Stopped installation only revalidates and copies that commit- and
digest-bound candidate. The target branch and exact-version Python wheels are
also cached before quiescence. A deterministic manifest binds the downloaded
wheel bytes to this rollout, and stopped installation uses `--no-index`. CI
pins third-party action revisions.
The atomically installed virtual environment is root-owned mode `0755` and is
smoke-tested through the CLI import as each unprivileged Python service user;
the two producer launchers never fall back to an unpinned system interpreter.
While the fleet is stopped, installation also reassigns the engine and all six
producer state trees in place through descriptor-relative traversal. Existing
empty LONG v1 state is upgraded to v2 without dropping cooldown history; a v1
record with a holding is not guessed because its v2 request clocks do not
exist. The research-only LLM ledger owns its state directory and has no path
into any producer or engine target-book directory.
Release qualification also requires the configured Ubuntu workflow's Python
checks, locked Rust tests, bounded optimized account-state soak, build, and
binary smoke test to pass on the exact pushed commit; local Windows
cross-compilation is not a substitute for executing those Linux binaries.

On a funded host, install atomically replaces
`/etc/liquidity-migration/engine-mainnet.toml` with the exact committed mainnet
template after the fleet is stopped and the target checkout is selected. The
file therefore cannot retain an old sleeve list while running a new binary.

The engine WAL's ordered strategy names are durable identity. Appending a
sleeve preserves every existing numeric ID, but the first successful boot then
writes the longer name list. Recovery from that point needs a binary and config
that accept the same prefix; a two-sleeve generation cannot replay a WAL that
names `carry`, `long`, and `exodus`. Keep a qualified three-sleeve release
available for recovery.

Deploy preflight and the fixed trusted launcher both prove the checkout's
immediate parent, checkout root, runtime-dispatcher ancestry, Telegram-bot
ancestry, and `.git` directory are canonical root-owned directories with no
group or other write bit. They recursively reject non-root-owned,
group/world-writable, linked, or special entries in `.git`. The remote deploy
also fixes its umask at `0022` before Git creates new metadata. A compromised
workload identity therefore cannot rewrite either the runtime source boundary
or the Git metadata used to bind it to the release marker.

### Manual account inventory

`scripts/ops.sh attest-flat --environment demo|mainnet` is an optional,
read-only account inventory command. Rollout and activation do not call it and
do not require an attestor credential. Demo uses its demo credential. Mainnet
uses `/etc/liquidity-migration/bybit-mainnet-attestor.env`; the transient
service removes the execution key and `REAL_MONEY`, drops to the mainnet engine
user, and does not copy credentials into the deploy shell or command line.

The Bybit adapter runs two complete scans. It discovers the venue's linear
settlement coins; paginates linear, inverse, and option positions; reads
unified-wallet non-cash assets and borrow liabilities; and reads linear,
inverse, option, and spot open orders. Mainnet also checks spread orders, RFQ
roles and inquiries, active venue-native algorithmic orders, and cross-account
asset categories. Unknown and delisted rows remain visible blockers.

A successful command means those two collected samples were fresh, stable in
scope, bound to the expected account ID, and empty. Bybit does not expose one
transactional account snapshot or every possible bot family, so the result
does not cover activity racing the scans. The separate account ID acknowledgement
(`BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID`) records that the UID is dedicated to
this engine. Other venue adapters refuse the account-wide inventory capability.


### Activation commit protocol

Rollout snapshots which managed units are active, persistently enabled, and
runtime-enabled before stopping them. Unsupported linked/transient enablement
states are refused before any unit stops. If a phase fails before checkout
mutation, cleanup restores the snapshot: a markerless incumbent starts directly;
a marked incumbent receives a temporary authority bound to its unchanged
release, starts only the observed units, then commits that restored generation.
Once checkout mutation starts, rollout does not guess across code or WAL
formats; a failed candidate leaves the managed fleet stopped for an explicit
compatible recovery.

Artifact installation and service activation are separate commits. Before the
first candidate unit starts, rollout removes the prior completion authority and
atomically creates root-owned mode-`0644`
`/run/liquidity-migration/activation.permit`. That temporary permit contains
exactly these ten ordered assignments:

1. `commit`;
2. `sha256`;
3. `launcher_sha256`;
4. `control_helper_sha256`;
5. `controls_sudoers_sha256`;
6. `telegram_bot_sha256`;
7. `boot_id`;
8. `owner_pid`;
9. `owner_start_ticks`;
10. `not_after_epoch`.

A root transient watchdog binds the permit to the rollout process's PID and
Linux start ticks, verifies the boot ID, opens that one-link inode once, and
renews its six-second lease once per second under an exclusive advisory lock.
Launchers take the matching shared lock before reading, so a reader sees one
complete lease. The watchdog rechecks the pathname-to-inode identity and link
count before and after every renewal; deleting, replacing, or hard-linking the
permit makes it stop without recreating the pathname. Supported cleanup still
stops the watchdog before unlinking. The guarded services retain
`ProtectProc=invisible`; only that watchdog reads the rollout process identity
from `/proc`. The installed, digest-bound launcher accepts either the temporary
permit or the durable receipt, supervises its child, and rechecks authority
every two seconds. A dead or reused rollout PID, a dead watchdog, an expired or
changed permit, or a hash mismatch therefore stops the workload instead of
leaving a partially activated topology running.

Only after every intended unit is enabled, running, and verified does rollout
flush that topology and atomically install root-owned mode-`0644`
`/opt/liquidity-migration-engine/bin/activation.complete`. The receipt contains
exactly the first six release-identity assignments above, in the same order.
It is synced and validated before the watchdog stops and the temporary permit
is removed. Each launcher checks receipt, permit, then receipt again, so a read
that straddles the two atomic changes still sees the committed authority. The
receipt is persistent and sufficient on its own: a normal boot
does not require `/run/liquidity-migration` or a leftover permit. A crash or
power loss before the receipt commit leaves no durable authority, so enabled
candidate units refuse startup and any lease-started children stop when the
lease expires; after the commit, the receipt describes the already verified
complete topology. Runtime tmpfiles recreate only the empty runtime directory
and lock files after boot, never activation authority.

## Flatten semantics

`flatten --execute` is a de-risking tool, not a reset or flat attestation. It:

1. requires a running Rust engine and readable heartbeat;
2. stops producers;
3. publishes explicit zero targets for observed configured-symbol positions;
4. waits for those observed positions to disappear;
5. leaves producers stopped and producer state unchanged;
6. exits 6 with `global_flat=unproven` even after known positions close.

Run `scripts/ops.sh attest-flat --environment demo|mainnet` with the concrete
realm after flattening. Unknown or delisted exposure may remain when that check
is unavailable or fails; do not restart or replace state on a configured-symbol
result alone.

## Real money

Before arming, verify all of the following:

- the funded API key is contract-trading only, withdrawal-disabled, and IP
  allowlisted;
- the funded UID is dedicated to this engine, with no hand trading, venue bots,
  copy trading, or other trading API keys, and
  `BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID` names it;
- the funded engine env contains the exact venue account user ID;
- the Rust toolchain and release binary match the pinned release;
- the operational profile is the exact render of reviewed dials;
- target, WAL, heartbeat, and data roots are real, disjoint local paths with
  appropriate ownership;
- Telegram delivery and the off-box dead-man signal are provisioned;
- the venue account is independently verified against the intended realm,
  position mode, margin mode, open orders, and positions.

The separate read-only inventory credential is needed only when the operator
uses manual `attest-flat`; arming and deployment do not require that file.

Arming is a host-side operator act. A repository commit cannot arm funded
trading.

Bybit startup independently verifies one-way position mode for every configured
symbol and aborts on ambiguity. It does not change mode, verify margin mode, or
prevent a later manual mode switch, so the operator check remains required.

Funded Bybit account identity also enforces the key shape reported by the venue.
It requires UTA membership, a write-capable key, ContractTrade Order and
Position permissions, and no Wallet
Withdraw permission. `BYBIT_REAL_API_KEY_IP` must name the primary production
host. `BYBIT_REAL_API_KEY_BACKUP_IP` may name one distinct backup host. The
venue must report exactly the declared one- or two-host set; exact host `/32`
and `/128` forms are also accepted. Missing, wildcard, all-network, undeclared,
duplicate, or mismatched entries abort before account identity is accepted.
Key creation time does not affect admission.

Funded identity separately requires
`BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID` to equal the authenticated account ID.
This is load-bearing because Bybit cannot machine-enumerate every account bot
family. Setting it is an acknowledgement of a dedicated UID, not permission to
share one and not proof that outside activity has stopped.

Optional mainnet inventory controls authenticate with a second key, never the
execution key. Copy [`deploy/bybit-mainnet-attestor.env.template`](../deploy/bybit-mainnet-attestor.env.template)
to `/etc/liquidity-migration/bybit-mainnet-attestor.env` as a regular
`root:root` mode-`0600` file. Apart from comments and blank lines, it must have
exactly one non-empty assignment for each of:

- `BYBIT_ATTEST_API_KEY`;
- `BYBIT_ATTEST_API_SECRET`;
- `BYBIT_ATTEST_API_KEY_IP`;
- `BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID`.

The venue must report this key as globally read-only, UTA-bound, and allowlisted
to exactly the declared host IP. It
needs the ContractTrade Order and Position query scopes plus Wallet
AccountTransfer query scope so every inventory endpoint can be read; Wallet
Withdraw must be absent. The global read-only flag makes those permission names
query authority, not order or transfer authority. The persistent engine service
never loads this file. Only transient mainnet `attest-flat` receives it.

## Venue qualification

A venue name in the registry proves integration, not production behavior.
`engine venues` shows the source-controlled gate for every exact realm.
`engine run` refuses `production-blocked` and `read-only` entries before it
opens a WAL, reads a credential, or opens a socket. Hyperliquid and Lighter
testnets are `testnet-canary`; their mainnets and MEXC mainnet remain
`production-blocked`. A reviewed source change may promote only the exact realm
whose complete live lifecycle evidence was retained.

Before calling an adapter production-proven, run the smallest permitted order
through its practice realm and keep the artifacts for:

1. credential realm and account identity, current instrument rules, and a
   complete account inventory;
2. public-feed cold start, forced sequence break, stale-book refusal, and
   reconnect recovery;
3. place, acknowledge, partial fill, full fill, cancel, amend where supported,
   leverage where required, and venue-native protection;
4. execution-history recovery across an intentional private-stream gap;
5. restart reconciliation with no duplicate send and no lost in-flight order;
6. reduction to flat followed by a fresh credential-wide flat attestation.

Hyperliquid and Lighter have practice realms. MEXC does not; its first canary is
real money and needs a separate owner decision. Variational has no account or
order API. Current evidence by venue is in [`engine.md`](engine.md) §The venues.

## Funded key rotation

Key rotation changes venue and host state and is performed by the account
owner. The engine validates the installed key against Bybit's identity reply;
it cannot create the replacement or revoke the prior key.

1. Run `scripts/ops.sh deploy disarm-mainnet`. This atomically removes the
   arming switch and stops and disables funded units; it does not flatten.
2. In the venue account, create a UTA key. It must be write-capable, grant
   ContractTrade Order and Position, omit Wallet
   Withdraw, and allowlist only the declared production host IPs. Remove or
   revoke every other trading key and venue bot for this UID, and stop hand and
   copy trading on it.
3. Replace the key and secret in the root-owned funded credential file on the
   host. Set `BYBIT_REAL_API_KEY_IP` to the primary host. Set the optional
   `BYBIT_REAL_API_KEY_BACKUP_IP` only when a second host is deliberately kept
   on the venue allowlist. Keep the file a regular
   `root:root` file with mode `0600`; never put the key or secret in git, shell
   history, logs, or chat. Set
   `BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID` to the dedicated funded UID only
   after the exclusivity condition is true.
4. Revoke the prior execution key at the venue.
5. Run `scripts/ops.sh real-money preflight` and independently verify the realm,
   account ID, venue-reported key shape, position mode, margin mode, open orders,
   and positions. If manual account-wide inventory is required, install the
   separate read-only inventory key and run
   `scripts/ops.sh attest-flat --environment mainnet`.
6. Arm and activate the funded fleet through a reviewed rollout.

`STATE.md` records whether rotation is still owed. Do not infer completion from
a green build or deploy.

## Venue-confirmed LONG accounting

The account-history capture is authenticated but GET-only. Mainnet uses the
separate `BYBIT_ATTEST_*` key by default; `--credential-set execution` selects
the rotated engine key explicitly. The capture window must have ended at the
venue and must remain inside Bybit's two-year history boundary.
The capture queries authenticated user identity and venue time again after all
three histories finish, then applies the retention boundary to that final time.

```bash
python scripts/research/capture_bybit_account_history.py \
  --realm mainnet \
  --start "$LONG_TRADE_START_UTC" \
  --end "$LONG_TRADE_END_UTC" \
  --out "$LONG_VENUE_CAPTURE"

python scripts/research/reconcile_venue_wal.py \
  --wal /var/lib/liquidity-migration-engine-mainnet/engine.wal \
  --venue-history "$LONG_VENUE_CAPTURE" \
  --sleeve long \
  --trade-execution-id "$LONG_REGISTERED_EXECUTION_ID" \
  --expected-realm mainnet \
  --expected-user-id "$BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID" \
  --deployment-receipt "$DEPLOYED_ACTIVATION_RECEIPT" \
  --engine-binary "$DEPLOYED_ENGINE_BINARY" \
  --engine-config "$DEPLOYED_ENGINE_CONFIG" \
  --expected-commit "$ROLLOUT_COMMIT" \
  --expected-binary-sha256 "$ROLLOUT_ENGINE_SHA256" \
  --expected-config-sha256 "$ROLLOUT_ENGINE_CONFIG_SHA256" \
  --out "$LONG_ACCOUNTING_REPORT"
```

The three expected identities come from the reviewed rollout record and exact
config bytes retained independently of the evidence being graded. Do not copy
an expected value back out of `activation.complete` merely to make the check
pass. The receipt is the exact six-line durable activation receipt; the binary
and config inputs are byte-for-byte captures from that deployed generation.

The report says `venue_confirmed` only when the complete WAL family, every
execution identity, both order identities, exact fill fields, the one-way
position path, trading fees, closed P&L, account cash changes, and every crowd
fee (funding) settlement agree. Each fill names its nearest preceding WAL Boot,
whose exact config SHA-256 must match the retained config and expected digest.
The activation receipt commit and binary digest must match the independent
expected values, and the supplied binary and config bytes are rehashed. A
missing, duplicate, foreign, truncated, wrong-generation, or out-of-retention
row withholds the label. `--trade-execution-id` selects the unique closed trade
that contains that immutable execution ID while retaining full-family WAL
integrity checks. A recovered fill is attributed to the Boot that recorded it;
the Boot does not by itself prove which historical binary sent the order.
Producer parity and tape grading are separate evidence and still use the
registered trade rather than a proxy.

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
- Any hand trade, venue bot, copy-trading authority, or other trading key on the
  funded UID: disarm and stop, remove the competing authority, inspect venue
  truth and the WAL, and clear any resulting reconciliation latch only after
  the discrepancy is understood.

Fleet identity, lifecycle, timers, operator commands, dependencies, health
checks, and artifact seams are defined by
[`deploy/fleet_manifest.tsv`](../deploy/fleet_manifest.tsv). Engine
recovery and risk contracts are in [`engine.md`](engine.md).
