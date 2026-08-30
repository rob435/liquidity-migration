# VPS systemd topology

The installed topology separates strategy target production from account
execution. Every guarded service enters through
`scripts/run_authorized_runtime.sh`, which replaces itself with the
commit-owned workload for that unit and entrypoint.

Operator commands, deploy modes, profiles and failure handling are in
[`../../docs/operations.md`](../../docs/operations.md); this file is only the
unit shapes.

## Services

Twenty-two unit files: fifteen services and seven timers (two liveness, the LLM ledger, the trade notifier, the forward uploader, the nightly backup, the weekly chaos drill).

| Unit | Role |
| --- | --- |
| `liquidity-migration-engine.service` | The Rust execution engine — sole Bybit **demo** mutator, on the fleet's demo account (555899665, `bybit-demo.env`); holds that account's single-writer lease — see below |
| `liquidity-migration-engine-mainnet.service` | The Rust engine on the **funded** account — runs only while `REAL_MONEY` is armed |
| `liquidity-migration-bybit-long-demo.service` | LONG target producer |
| `liquidity-migration-bybit-carry-demo.service` | CARRY target producer |
| `liquidity-migration-bybit-{carry,long}-mainnet.service` | Real-money target producers; both start when `REAL_MONEY` is armed, sized by the installed risk profile |
| `liquidity-migration-demo-liveness.service` | Account/strategy watchdog and notification surface |
| `liquidity-migration-mainnet-liveness.service` | Mainnet account/strategy watchdog and notification surface |
| `liquidity-migration-telegram-controls.service` | Owner control buttons (pause/resume — there is no close button) — the sole `getUpdates` consumer |
| `liquidity-migration-llm-ledger.service` | LLM driver judgments on movers and trigger events, and the judged candidates file the demo LONG sleeve enters through — run by its hourly timer |
| `liquidity-migration-trade-notify.service` | Sends every sleeve's entries and its exits with what they made to the owner's DM — run by its 5-minute timer |
| `liquidity-migration-forward-capture.service` | Records public Bybit L50 books, trades, derivative tickers and liquidations into verified compressed segments; no account credentials or order path |
| `liquidity-migration-forward-upload.service` | Copies only completed compressed segments to Google Drive, checks each new batch, and writes a Drive-side checksum list — run by its hourly timer |
| `liquidity-migration-backup.service` | Nightly off-box copy of the WALs and trade files (the state git cannot rebuild) — run by its daily timer; a note and a clean exit until `/etc/liquidity-migration/backup.env` names a destination |
| `liquidity-migration-chaos-drill.service` | Weekly crash-recovery rehearsal: kills the **demo** engine and reports on the alerts line whether it came back clean, latched, or not at all — run by its Sunday timer; never touches mainnet |

The liveness services are invoked by their matching timers, and the engines own
the accounts. Target producers and auxiliary services have private API, mainnet,
`REAL_MONEY`, and unnecessary Telegram variables explicitly removed.

The forward recorder writes under
`/var/lib/liquidity-migration/forward-market`. It keeps the local receive time
beside the exchange times, preserves depth sequence and flags regression,
rotates at 64 MB, verifies
each `zstd` file and its checksum before deleting the raw segment, then retains
at most 30 days or 60 GB while leaving at least 25 GB free. Its manifest records
both compression and deletion. A queue overrun rebuilds the socket so the next
book epoch begins with a fresh snapshot.

The forward uploader reads that directory but selects only immutable `.zst`
segments. It never sends `.partial` files, venue credentials, account WALs, or
environment files. A local ledger advances only after `rclone check` passes,
and each successful batch leaves a SHA-256 list under `_batches/` in Drive.
Its root-only OAuth configuration is
`/etc/liquidity-migration/rclone.conf`, beside the other host credentials that
move with a VPS migration.

## Dependency edges

No unit can take the fleet down with it. Every unit's only lifecycle edges
are `Wants=`/`After=` on `network-online.target` — nothing `Requires=`
anything else in the fleet.

- **Producers** (demo and mainnet alike) publish target books to disk; the
  engine reads the books and owns the account. No producer binds to the engine:
  a dead engine leaves the producers running and publishing.
- **Neither liveness unit** has an ordering, requirement, binding, part-of,
  requisite, uphold, or wants edge to the units it watches — a stopped or
  failed unit is what it alerts on. The mainnet observer loads the root-only
  `telegram-mainnet.env` projection; funded API keys and `REAL_MONEY` never
  enter the observer process.
- **The control panel** (`telegram-controls`) likewise has no edge to the
  fleet it controls: it must keep serving buttons while the units it pauses
  or resumes are stopped. It holds Telegram credentials only — the API-key
  pairs and `REAL_MONEY` are unset — and acts through `systemctl` and the
  sleeve override + resolve library.

## The engine units

What the engine does with an account is [`../../docs/engine.md`](../../docs/engine.md);
`liquidity-migration-engine.service` is the odd unit here, in three ways.

- **It owns the fleet's demo account.** It loads `bybit-demo.env` — demo
  account 555899665, the live demo book — and holds that account's
  single-writer kernel lease. Nothing else writes to the account: anything
  else taking the lease stops the engine from starting rather than letting
  two writers wedge each other.
- **The engine is mandatory.** Activation requires the exact locked release,
  `/etc/liquidity-migration/engine.env`, its config, a fresh heartbeat, and the
  expected account/venue/realm binding. Deploy installs a missing non-secret
  demo environment and atomically adds absent identity fields to a legacy one;
  an explicit mismatch is never overwritten.
- **Its build is part of the deploy gate.** `cargo build --release --locked`
  runs against the exact target commit during prefetch, before the fleet is
  stopped. Cargo first fetches the locked graph into a clean isolated cache;
  compilation then runs `--offline` with a private network. Stopped installation
  rechecks the immutable source, candidate owner, path, and prefetched digest
  before copying it. Any toolchain, fetch, compile, install, restart, digest, or
  commit-marker failure aborts activation.

`liquidity-migration-engine-mainnet.service` has the same shape on the funded
account: gated by `/etc/liquidity-migration/engine-mainnet.env` plus the
binary, started through `start_mainnet_fleet` when `REAL_MONEY=true` in
`/etc/liquidity-migration/bybit-mainnet.env` — the single arming switch, and
the whole of what decides whether the funded engine trades. See the Real-money
section of [`../../docs/operations.md`](../../docs/operations.md).

Persisting any funded surface makes rollout the only path allowed to change or
activate the installed generation. Direct install, activate, and staged modes
refuse, as do `ops.sh start` and `restart` for funded units. Stop and disarm stay
available for fail-safe action.

Those two remote fail-safe paths never import or execute the deployed checkout.
They quarantine and verify the exact funded-unit allowlist first; stop does not
read credentials, while disarm uses an isolated root-owned system interpreter
and embedded strict parser for the stable, atomic `REAL_MONEY=false` rewrite.

Every managed runtime unit starts through the fixed, root-owned trusted
launcher. During rollout that launcher accepts an ephemeral root-owned
`/run/liquidity-migration/activation.permit` with exactly ten ordered fields:
the engine, launcher, control-helper, sudoers, and Telegram-bot release hashes;
then `boot_id`, rollout `owner_pid`, `owner_start_ticks`, and
`not_after_epoch`. A root transient watchdog verifies the PID/start-ticks and
boot binding, pins the one-link permit inode, and refreshes the six-second lease
once per second under an exclusive lock. Launchers read under a shared lock.
The watchdog verifies pathname identity and link count around every write, so
unlink, replacement, or hard-linking revokes without permit recreation. Each
launcher supervises its workload and polls authority every two seconds, so
rollout or watchdog death, PID reuse, expiry, replacement, or digest drift
terminates an incomplete activation even though service users retain
`ProtectProc=invisible`.

At watchdog startup, the permit's device/inode is recorded before content
validation. The watchdog then takes a non-creating read pin, compares that pin
with the recorded identity and current pathname, and revalidates the content
under an exclusive lock before renewal. Thus an unlink or even a valid-looking
replacement in either startup gap is rejected rather than adopted.

After the entire intended topology is enabled, active, and verified, rollout
syncs it and atomically writes the persistent root-owned
`/opt/liquidity-migration-engine/bin/activation.complete` receipt. Its exact six
ordered fields are `commit`, `sha256`, `launcher_sha256`,
`control_helper_sha256`, `controls_sudoers_sha256`, and
`telegram_bot_sha256`. The receipt is synced and validated before the watchdog
and permit are retired. Launchers check receipt, permit, then receipt again, so
a validator straddling the handoff cannot miss both authorities. The receipt
authorizes a verified complete generation after reboot without depending on the
ephemeral `/run` directory; a power loss before that commit instead leaves
enabled units unable to cross the launcher. Tmpfiles recreate the empty
runtime/lock boundary only, never either authority file.

The engines are installed by the exact systemd manifest and run under distinct
unprivileged identities. Their root-only credential files are loaded by PID 1;
producer and observer processes receive only non-secret projections.

Every unit that runs repository Python able to reach Polars — the four target
producers and both liveness observers — retains `ProtectProc=invisible` but
does not set `ProcSubset=pid`. Polars reads `/proc/meminfo` when sizing its
cgroup-aware memory manager; hiding non-process `/proc` files makes native
Parquet work fail, which kills a producer before it publishes a target book and
kills the watchdog that would report it. Other users' process metadata remains
hidden by `ProtectProc`. The two compiled engines read no Parquet and keep
`ProcSubset=pid`.

Before deploy trusts the checkout, and again before checkout code runs, the
deploy preflight and trusted launcher reject group/world-writable or
non-root-owned critical checkout ancestors. They also recursively require Git
metadata to be root-owned, non-writable by group/other, and composed only of
regular files and directories; deploy uses umask `0022` for new metadata. The
runtime identities cannot rewrite the source or metadata that the release
marker authenticates.

Optional manual inventory controls use transient systemd services under the
matching engine identity. PID 1 loads the engine environment plus the realm
credential. For mainnet that credential is the operator-owned root:root
mode-0600 `/etc/liquidity-migration/bybit-mainnet-attestor.env`, containing
exactly the four read-only inventory/UID assignments. The transient unit
explicitly removes the execution key and `REAL_MONEY`; persistent services and
deployment never load the attestor file. Every inventory command requires two
complete scans with stable scope.

`scripts/ops.sh attest-flat --environment demo|mainnet` uses this transient
service boundary. Mainnet receives only the read-only attestor key.

## Watchdog timers

| Timer | First fire | Then |
| --- | --- | --- |
| `demo-liveness.timer` | `OnActiveSec=1min` | `OnUnitActiveSec=3min` |
| `mainnet-liveness.timer` | `OnActiveSec=1min` | `OnUnitActiveSec=3min` |
| `llm-ledger.timer` | `OnCalendar=*-*-* *:05:00` | `Persistent=true` |
| `trade-notify.timer` | `OnCalendar=*-*-* *:0/5:30` | `Persistent=true` |

The demo observer fires a minute after the timer arms; cold-start noise is
handled by the watchdog's own startup grace (`--max-cycle-age-min 10`). Both
run with `--cooldown-min 60`, so a repeated condition pages hourly rather than
every pass. Both alert on a service that is enabled but not active.

Strategy cycle `ts_ms` is the causal scheduling input, not a completion
timestamp. Once cycle output, target capture, and the decision outcome are
durable, each producer atomically publishes a completion projection bound to
systemd's current `INVOCATION_ID`; the watchdog reads that for age and WS-store
size and binds it back to the causal cycle row, so a prior service generation
cannot mask a hung restart. Before the first projection, the current generation
gets the same bounded ten-minute grace as the cycle SLA. The observer suppresses
only the nonterminal queue-head L2 subscription transition (latched at 30s; the
terminal timeout still pages). Missing, stale, reconciliation, and capital
health failures are never suppressed.

Instrument rules are not a watchdog or deploy input. Each Rust engine asks its
selected venue adapter for current rules at boot and refuses to start if that
read fails or omits a configured symbol.
