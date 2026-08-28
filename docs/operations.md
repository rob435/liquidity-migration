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
| `loss-reset --environment demo\|mainnet --note TEXT` | Prove that realm stopped and flat; inspect by default, clear durably only with `--execute` |
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
sync, and verify the fixed funded-unit allowlist. Stop never opens the funded
credential. Disarm then resolves and verifies the root-owned system Python,
runs it with an empty environment and isolated/no-site flags, and uses an
embedded strict parser to stably read and atomically replace the mode-`0600`
credential with `REAL_MONEY=false`. If that rewrite fails after quarantine,
the funded units remain persistently stopped and disabled.

`engine venues` and `engine strategies` are credential-free discovery commands
for the compiled venue realms, evidence gates, and strategy plugs.

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
Release qualification also requires the configured Ubuntu workflow's Python
checks, locked Rust tests, bounded optimized account-state soak, build, and
binary smoke test to pass on the exact pushed commit; local Windows
cross-compilation is not a substitute for executing those Linux binaries.

Deploy preflight and the fixed trusted launcher both prove the checkout's
immediate parent, checkout root, runtime-dispatcher ancestry, Telegram-bot
ancestry, and `.git` directory are canonical root-owned directories with no
group or other write bit. They recursively reject non-root-owned,
group/world-writable, linked, or special entries in `.git`. The remote deploy
also fixes its umask at `0022` before Git creates new metadata. A compromised
workload identity therefore cannot rewrite either the runtime source boundary
or the Git metadata used to bind it to the release marker.

Before target prefetch or any stop, a rollout verifies the outgoing installed
engine against its checkout-bound release marker and SHA-256, copies that
root-owned executable into a private runtime directory, and verifies the source
and copy against the same digest. This immutable outgoing snapshot is the first
trusted verifier. The incoming checkout and build candidate never attest to
their own safety.

The activation watchdog records the permit device/inode before validating its
contents, opens that exact object read-only (never with a pathname
`O_CREAT`), proves the pinned identity is unchanged, and only then reopens the
descriptor for locked renewal. It revalidates content under the inode lock
before the first and every later write. A direct unlink or same-content
replacement therefore revokes activation instead of being recreated or adopted
by a startup race. Launchers validate the durable receipt, then the locked
permit, then the receipt again so the atomic completion handoff has no
receipt/permit observation gap.

The rollout uses that outgoing snapshot before any unit stops and again after
all downstream units and both account owners stop. After the target is built
and installed while quiescent, the installed-generation boundary requires two
independent verifiers: the unchanged outgoing snapshot and the newly installed
target bound to its installed release marker and digest. Each verifier performs
two complete inventory scans with stable scope. A refusal by either blocks
activation.

An outgoing release that predates `attest-flat` fails before prefetch. The
rollout never substitutes the incoming candidate. Crossing that compatibility
boundary requires a separately signed and reviewed out-of-band attestor
bootstrap; this repository provides no automatic bypass. Provision its operator
trust root independently as the regular root-owned mode-`0600` file
`/etc/liquidity-migration/rollout-attestor-operator-public.pem`. Never place or
copy that public key into `/etc/liquidity-migration/attestor-bootstrap`; that
root-owned mode-`0700` directory contains exactly `attestor`, `manifest`, and
`manifest.sig`. The signed manifest contains exactly `commit`, `sha256`,
`purpose`, and `not_after_utc` in that order. Rollout verifies the signature and
expiry against the independently provisioned trust root before it snapshots or
runs the bootstrap attestor.

The inventory probe type has no order, cancel, amend, stop, leverage, or other
venue-mutation method. For mainnet, systemd loads the mainnet engine environment
plus the dedicated read-only attestor file, explicitly removes the write-key
variables and `REAL_MONEY`, and runs under the unprivileged mainnet engine user.
Neither the deploy shell nor a mainnet attestation receives the execution key.
Before the first proof, rollout validates and privately snapshots the exact
attestor file and binds its digest, so every rollout phase uses the same
credential material.

Demo is always attested. Any persisted funded surface makes mainnet attestation
mandatory and requires both the mainnet engine environment and the attestor
file. Funded and `--require-flat` rollouts abort with status 3 on any non-flat
or incomplete read. An unarmed demo-only rollout reports the same failure and
continues unless `--require-flat` is set.

Bybit discovers every linear settlement coin advertised by the venue while
retaining USDT and USDC, then strictly paginates linear, inverse, and option
positions; unified-wallet non-cash assets and every borrow liability; and
linear, inverse, option, and spot open orders. Mainnet also scans spread open
orders, both RFQ quote roles and inquiries, and active venue-native TWAP,
chase, iceberg, and POV strategies. Its cross-account asset overview retains
non-cash holdings and liabilities from every reported product account and
blocks every reported TradingBot or CopyTrading category even at zero equity.
Demo exposes none of those extended surfaces. Unknown and delisted rows remain
blockers by name. The verifier also
binds the authenticated venue, realm, and expected account ID, requires a
non-empty stable scope, rejects either local sample when it is older than 30
seconds or more than 5 seconds in the future, and rejects a double scan that
takes more than 60 seconds. Other venue adapters currently refuse this
capability.

Bybit exposes these surfaces through several reads, not one transactional
snapshot. The two complete scans make some races visible but do not make the
result atomic. Bybit also has no account-wide list for every bot family. The
funded UID must therefore be dedicated to this engine, with no hand trading,
venue bots, copy trading, or other trading API keys;
`BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID` must equal the authenticated UID as the
reviewed operator acknowledgement of that constraint. Do not place orders or
move assets while a rollout or manual attestation is running. Success describes
the collected samples, not activity racing them and not a mathematical proof
that unenumerable bot state is absent.

`scripts/ops.sh attest-flat --environment demo|mainnet` runs the installed
adapter's same read-only check outside a rollout. Demo uses its demo credential.
Mainnet uses `/etc/liquidity-migration/bybit-mainnet-attestor.env` and explicitly
removes the execution key and arming switch from the transient service. The
wrapper drops to the realm's unprivileged engine user and never copies secrets
into the deploy shell or argv. The underlying `engine attest-flat` reads
`ENGINE_CONFIG_FILE`, then falls back to `engine.toml` when no `--config` is
given. Success means the adapter returned two fresh, empty credential-wide
samples; it does not predict activity after them.

### Activation commit protocol

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
- `/etc/liquidity-migration/bybit-mainnet-attestor.env` is an operator-installed
  regular `root:root` mode-`0600` file containing exactly the four non-empty
  attestor assignments from its template;
- the attestor key is physically separate from the execution key, globally
  read-only, UTA-bound, and allowlisted only to the production host;
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

Arming is a host-side operator act. A repository commit cannot arm funded
trading.

Bybit startup independently verifies one-way position mode for every configured
symbol and aborts on ambiguity. It does not change mode, verify margin mode, or
prevent a later manual mode switch, so the operator check remains required.

Funded Bybit account identity also enforces the key shape reported by the venue.
It requires a creation time on or after 2026-08-27 22:30 UTC, UTA membership, a
write-capable key, ContractTrade Order and Position permissions, and no Wallet
Withdraw permission. `BYBIT_REAL_API_KEY_IP` must name the one production host
IP, and the venue must report exactly that IP alone; the exact host `/32` or
`/128` form is also accepted. Missing, wildcard, all-network, additional, or
mismatched entries abort before account identity is accepted. This forces
replacement of the exposed older key but cannot create, install, or revoke a
key for the owner.

Funded identity separately requires
`BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID` to equal the authenticated account ID.
This is load-bearing because Bybit cannot machine-enumerate every account bot
family. Setting it is an acknowledgement of a dedicated UID, not permission to
share one and not proof that outside activity has stopped.

Mainnet inventory controls authenticate with a second key, never the execution
key. Copy [`deploy/bybit-mainnet-attestor.env.template`](../deploy/bybit-mainnet-attestor.env.template)
to `/etc/liquidity-migration/bybit-mainnet-attestor.env` as a regular
`root:root` mode-`0600` file. Apart from comments and blank lines, it must have
exactly one non-empty assignment for each of:

- `BYBIT_ATTEST_API_KEY`;
- `BYBIT_ATTEST_API_SECRET`;
- `BYBIT_ATTEST_API_KEY_IP`;
- `BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID`.

The venue must report this key as globally read-only, UTA-bound, created on or
after 2026-08-27 22:30 UTC, and allowlisted to exactly the declared host IP. It
needs the ContractTrade Order and Position query scopes plus Wallet
AccountTransfer query scope so every inventory endpoint can be read; Wallet
Withdraw must be absent. The global read-only flag makes those permission names
query authority, not order or transfer authority. The persistent engine service
never loads this file. Only transient mainnet `attest-flat`, stopped-engine
`loss-reset`, and rollout proofs receive it.

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

## Daily loss halt

`account_risk.max_daily_loss_usdt` is `10.0` in the funded profile and `null`
in demo. At a UTC boundary the engine compares its latest pre-midnight equity
evidence with the first fresh valid post-midnight account view and uses the
higher value as the opening anchor. Equity at or below opening minus the cap
refuses entries as `LossGuardTripped`; genuine reduce-only exits still flow.
The measure is account-wide equity, not sleeve P&L or a high-water mark, so
fees, funding, unrealized P&L, and outside activity reflected by the venue all
affect it.

The opening anchor and any trip are made durable in the WAL. Boundary equity
evidence is also checkpointed once per minute and immediately on every rise,
so a restart around midnight cannot discard an observed cross-boundary loss.
Order and opening-amend assessments advance the UTC risk clock themselves, so
they cannot race ahead under yesterday's anchor while waiting for the next
account poll. If the process was offline, the
older/higher evidence can conservatively halt too early; it cannot grant extra
budget. A non-tripped anchor rolls on the next UTC day. A trip stays latched across equity recovery,
day changes, and restart until an operator performs this workflow:

1. Stop the engine that owns the account and leave producers stopped.
2. Run `scripts/ops.sh loss-reset --environment demo|mainnet --note "REASON"`
   with one concrete environment. The wrapper refuses while that realm's engine
   or either producer is active, loads the private files through systemd, and
   runs as the realm's unprivileged engine user. This dry run claims the WAL,
   requires a fresh credential-wide flat attestation bound to the configured
   realm and expected account ID, and writes nothing. On mainnet the venue read
   uses only the dedicated read-only attestor key; the execution key and
   `REAL_MONEY` are absent.
3. Investigate the trip and independently confirm the account should resume.
4. Repeat the same wrapper command with `--execute`. The command appends the
   operator note and canonical cleared risk anchor, then crosses one durability
   barrier.
5. Start the engine and producers through the normal activation path. The next
   new-risk assessment establishes a new UTC opening-equity anchor.

The note must be non-empty and at most 512 bytes. Never clear the WAL anchor by
editing files or by restarting the process.

## Funded key rotation

Key rotation changes venue and host state and is performed by the account
owner. The engine validates the installed key against Bybit's identity reply;
it cannot create the replacement or revoke the prior key.

1. Run `scripts/ops.sh deploy disarm-mainnet`. This atomically removes the
   arming switch and stops and disables funded units; it does not flatten.
2. In the venue account, create a UTA key on or after 2026-08-27 22:30 UTC. It
   must be write-capable, grant ContractTrade Order and Position, omit Wallet
   Withdraw, and allowlist only the production host IP. Remove or revoke every
   other trading key and venue bot for this UID, and stop hand and copy trading
   on it.
3. Replace the key and secret in the root-owned funded credential file on the
   host, and set `BYBIT_REAL_API_KEY_IP` to that one IP. Keep the file a regular
   `root:root` file with mode `0600`; never put the key or secret in git, shell
   history, logs, or chat. Set
   `BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID` to the dedicated funded UID only
   after the exclusivity condition is true.
4. Create a separate globally read-only attestor key with the query scopes
   above, install its exact four-value root-owned file, and do not put either
   key in the other's environment.
5. Revoke the prior execution key at the venue.
6. Run `scripts/ops.sh real-money preflight`, then
   `scripts/ops.sh attest-flat --environment mainnet`, and independently verify the
   realm, account ID, venue-reported key shape, position mode, margin mode, open
   orders, and positions. The startup must accept the new execution key and the
   attestation must accept the separate query key.
7. Arm and activate the funded fleet only through a reviewed rollout after every
   check passes.

`STATE.md` records whether rotation is still owed. Do not infer completion from
a green build or deploy.

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

Systemd topology, users, environment files, and credential projections are
listed in [`deploy/systemd/README.md`](../deploy/systemd/README.md). Engine
recovery and risk contracts are in [`engine.md`](engine.md).
