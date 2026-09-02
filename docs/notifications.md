# Notifications and controls

Telegram carries three separate surfaces:

| Surface | Source | Destination | Authority |
| --- | --- | --- | --- |
| trade updates | engine heartbeat and attributed trade log | main chat | read-only |
| health alerts | fleet liveness checks | alert chat, falling back to main | read-only |
| control panel | Telegram poller through the root helper | main chat | durable entry controls only |

The Rust engines and signal workers receive no Telegram variables. Observer
units load the token and chat IDs, then explicitly unset every venue credential
and `REAL_MONEY`.

## Trade updates

`liquidity-migration-trade-notify.timer` runs every five minutes. Its oneshot
service reads both realms:

| Realm label | Position source | Exit source |
| --- | --- | --- |
| `DEMO` | `/var/lib/liquidity-migration-engine/heartbeat.json` | `/var/lib/liquidity-migration-engine/trades.jsonl` |
| `RM` | `/var/lib/liquidity-migration-engine-mainnet/heartbeat.json` | `/var/lib/liquidity-migration-engine-mainnet/trades.jsonl` |

An entry message is generated only when a fresh, exact-realm engine heartbeat
contains a new attributed LONG, CARRY, or Exodus position. Notional is filled
quantity times average entry price. Desired reducer state and migration target
books are not position evidence.

An exit message comes only from a complete engine trade-log line. A priced
round trip reports sleeve, symbol, side, entry and exit, hold time, net profit
or loss after venue fees, return on position notional, maker share when known,
and arrival shortfall when known. Funding is not included because the engine
fill stream does not report the venue's wallet funding settlements.

`maker_canary` rows remain in the engine log but are hidden from phone messages
and daily totals. They are execution-canary evidence, not directional trading
updates.

The first observation of a trade file baselines at its current valid byte
offset and does not replay history. A partial final line waits for the next
run. A malformed complete line pins the offset and makes the run fail so it
cannot be skipped. Notification state advances only after all messages send.

An unreadable, stale, future-dated, wrong-realm, malformed, or unattributed
heartbeat retains the prior position snapshot. It cannot invent an entry or
exit. The separate liveness service reports the unhealthy source.

Once per UTC day, the notifier summarizes the completed prior day from both
trade logs. Demo and real-money sleeves stay separate in every subtotal.

## Liveness alerts

Three liveness timers run: demo, mainnet, and host. Alerts use
`TELEGRAM_ALERT_CHAT_ID`; if it is absent, the main chat is used so a fault is
not silently dropped.

The check derives its expected units and heartbeat artifacts from
[`deploy/fleet_manifest.tsv`](../deploy/fleet_manifest.tsv). A realm scope
pages on:

- an inactive fleet unit in its realm the manifest says must be running;
- a stale or unreadable engine or worker heartbeat;
- an engine whose heartbeat says it cannot open positions; and
- an engine whose rolling-loss trip is on, with the 24-hour net and the limit.

The host scope is independent of the fleet — it keeps running through deploys
and funded stops — and pages on:

- an inactive independent unit (the two recorders, the upload and backup
  timers);
- a recorder status file that is stale, says storage is blocked, shows new
  dropped frames, or reports no market frame for two minutes — each recorder
  separately, the Binance one's alerts suffixed with its state directory;
- a market-tape upload receipt older than three hours, or a Drive with less
  than 200 GB free;
- a backup receipt older than eight hours;
- low disk under `/var/lib`; and
- an unsynchronised host clock.

Each scope pings its own dead-man's-switch URL when healthy; the host scope's
is optional and must differ from the demo scope's, or a dead demo fleet would
be masked by a healthy recorder.

One systemd process being `active` does not suppress a stale heartbeat or a
latched engine.

Alerts are keyed and persisted. A new condition sends immediately, a continuing
condition re-alerts after its cooldown, and a cleared condition sends one
resolved note.

An optional external heartbeat URL is pinged on a healthy run so an external
dead-man's-switch catches a box death the on-box watchdog cannot.

## Control panel

`liquidity-migration-telegram-controls.service` long-polls the Telegram bot.
It understands `/controls`, `/panel`, `/start`, and `/status`. Updates queued
while the service was down are discarded before polling begins; a stale press
cannot execute later.

The panel exposes:

- fleet status;
- pause demo;
- resume demo; and
- pause real money while the funded owner is active.

There is no close button and no funded-resume button. Flatten is a deliberate
operator command through [`scripts/ops.sh`](../scripts/ops.sh).

A press is accepted only from the configured chat. In a private chat, the
sender ID must equal that chat ID unless `TELEGRAM_CONTROL_USER_IDS` names an
allow-list. In a group, an allow-list is required. The unprivileged bot can run
only an exact action from the sudo policy; it cannot pass paths, units,
environment variables, or extra arguments.

The root-owned helper submits immutable controls to the engine as the realm
runtime user.

### Pause

Pause sends `entries_enabled=false` to LONG, CARRY, and Exodus and waits for a
fresh engine heartbeat to report all three false. The engine and signal worker
stay active. Open positions, exits, covers, stop repair, observations, and
checkpoints continue.

Demo pause also saves the exact pre-pause LONG/CARRY owner switches, writes
their resolved off state, and keeps that state across reboot and deployment.

Funded pause changes no arming credential. It is available only when the funded
owner is already active.

### Resume

Demo resume requires a live demo owner. It restores the saved LONG/CARRY switches, submits the matching
entry permissions, enables Exodus when its committed config permits it, and
waits for heartbeat acknowledgement.

The helper has an explicit `resume-mainnet` recovery action, but the phone
panel does not expose it. That action requires an already running funded
owner. It does not read or write `REAL_MONEY`, so it cannot arm a disarmed
account.

## Fleet status

The status response is machine-derived. It reports:

- demo and mainnet owner unit state;
- demo and mainnet signal-worker state;
- effective LONG, CARRY, and Exodus entry permissions in each engine
  heartbeat; and
- the demo LONG/CARRY configured owner switches.

The parser requires one owner, one signal worker, and all three exact entry
rows per realm. Missing, duplicate, unknown, or contradictory rows make status
fail instead of presenting a partial fleet as healthy.

## Configuration

Core variables:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
TELEGRAM_ALERT_CHAT_ID
TELEGRAM_CONTROL_USER_IDS
```

`TELEGRAM_ALERT_CHAT_ID` is optional and falls back to the main chat.
`TELEGRAM_CONTROL_USER_IDS` is a comma-separated set of numeric sender IDs.
The systemd units own state paths; callers cannot redirect them through an
environment variable.

Messages are escaped once and sent as one monospace HTML block. Telegram's
4096-character limit is respected by batching below 3500 characters. A 429 is
retried once only when the requested wait is within the bounded retry cap.

## Diagnosis

Use the corresponding unit journal before changing state:

```sh
scripts/ops.sh logs trade-notify.service 200
scripts/ops.sh logs demo-liveness.service 200
scripts/ops.sh logs mainnet-liveness.service 200
scripts/ops.sh logs host-liveness.service 200
scripts/ops.sh logs telegram-controls.service 200
```

For missing trade messages, inspect the notifier state offset, the exact realm
heartbeat, and the trade-log tail. For an alert, follow its key to the named
artifact. For a refused control, inspect the helper error; do not bypass the
release, account, or heartbeat check with direct systemd actions.
