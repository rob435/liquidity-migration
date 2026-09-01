# Systemd fleet

[`../fleet_manifest.tsv`](../fleet_manifest.tsv) is the canonical unit
inventory. The files in this directory implement it exactly; tests reject a
current manifest row without a file, an unregistered file, or an invalid
dependency.

## Trading topology

Each realm has two long-running Rust processes:

| Realm | Public-signal worker | Account owner |
| --- | --- | --- |
| demo | `liquidity-migration-signal-worker-demo.service` | `liquidity-migration-engine.service` |
| mainnet | `liquidity-migration-signal-worker-mainnet.service` | `liquidity-migration-engine-mainnet.service` |

The worker has public-data inputs only. It writes immutable observations to
`/var/lib/liquidity-migration/signals/<realm>` and keeps its checkpoint and
heartbeat in its own `StateDirectory`. Its unit removes every known private
credential, real-money switch, and Telegram secret from the environment.

The engine is the sole account writer. It reads the realm signal spool, owns
the private venue credential and account lease, and writes the WAL, heartbeat,
and trade log in its own `StateDirectory`. It also reads durable operator
commands from `/var/lib/liquidity-migration/controls/<realm>`.

The engine `Wants` and starts after the worker. Worker failure does not stop the
engine: exits, account reconciliation, and already durable observations must
remain available. Activation starts the worker first, requires a `ready`
heartbeat bound to the exact input and engine-config hashes, then starts and
verifies the account owner.

The mainnet pair starts only when the owner has armed `REAL_MONEY=true` in the
funded credential file and every funded preflight check passes. A systemd unit
name or config file cannot arm money.

## Observers and jobs

The remaining current units do not decide directional exposure:

| Unit family | Role |
| --- | --- |
| `demo-liveness` and `mainnet-liveness` service and timer | Page on an inactive fleet unit, a stale heartbeat, a latched engine, or a rolling-loss trip in their realm |
| `trade-notify.service` and timer | Report actual engine-attributed positions and closed-trade P&L |
| `telegram-controls.service` | Receive owner commands; the helper submits Rust runtime controls |
| `chaos-drill.service` and timer | Exercise demo recovery |
| `llm-ledger.service` and timer | Research-only public-data judgments; no strategy input |

## Independent units

The manifest marks four unit families `independent`. A deploy never stops
them, a funded stop or disarm never touches them, and they start at boot, so
they run whether or not the trading fleet is up:

| Unit family | Role |
| --- | --- |
| `forward-capture.service` | The Bybit market recorder: books, trades, tickers, funding, liquidations, and daily instrument snapshots ([`docs/data.md`](../../docs/data.md) §Market tape) |
| `market-tape-upload.service` and timer | Every hour, pack each finished hour of the tape into one archive and upload it to Google Drive |
| `backup.service` and timer | Four times a day, snapshot the engines' logs and state and mirror them to Google Drive with history |
| `host-liveness.service` and timer | Page on the recorder, the upload receipt, the backup receipt, disk, and the host clock |

Deploy restarts the recorder only when its unit file, script, symbol list, or
Python dependencies changed, and then waits for a status file the new process
wrote; otherwise it is left running. Timers are restarted so a changed
schedule applies.

## Identities and permissions

Trading and observation identities are separate:

- `liquidity-signal-worker` runs both credential-free signal workers.
- `liquidity-engine-demo` owns the demo engine state and control spool.
- `liquidity-engine-mainnet` owns the funded engine state and control spool.
- observer, controls, capture, builder, and research identities receive only
  the paths required by their units.

The shared `liquidity-migration` group grants narrow read/traverse access. It
does not grant private credentials or account mutation. Engine units have
write access only to their state, signal spool, control spool, and account
lease directory. Signal workers have write access only to their state and
signal spool.

Each unit file carries its own committed command line. Unit environment files
select reviewed inputs; callers cannot append an alternate command line.

## Lifecycle

Deploy stops every fleet unit (never an independent one), installs the exact
commit and the Rust release, renders native configs, completes stopped state
takeover, then starts the signal worker, the account owner, and the downstream
units in manifest order, waiting for a fresh heartbeat at each step. A realm
whose worker or owner publishes no fresh heartbeat is rolled back: the last
commit whose deploy finished is deployed again, and the run still fails so
the failure is visible.

Pause and flatten do not stop signal workers. They use durable entry-permission
and flatten requests in the engine control spool, so settlement observations
and exits continue.
