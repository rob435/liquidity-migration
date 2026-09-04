# Notifications and On-Call Specification

## 1. Purpose

Define the fleet's Telegram surfaces, liveness detection, automated incident response, external dead-man, and operator controls.

## 2. Spec Tables

### Surfaces

| Surface | Unit / Trigger | Cadence | Destination | Authority |
| :--- | :--- | :--- | :--- | :--- |
| Trade updates | `liquidity-migration-trade-notify.timer` | 5 min | Telegram main chat | Read-only openings, closes, and daily digest |
| Demo liveness | `liquidity-migration-demo-liveness.timer` | 3 min | Telegram alerts + incident routine | Demo engine, signal worker, timers, heartbeats, admission |
| Mainnet liveness | `liquidity-migration-mainnet-liveness.timer` | 3 min while armed | Telegram alerts + incident routine | Funded engine, signal worker, timers, heartbeats, admission |
| Host liveness | `liquidity-migration-host-liveness.timer` | 3 min, independent | Telegram alerts + incident routine + external dead-man | Recorders, upload, backup, equity sampler, disk, clock, realm watchdogs |
| Operator controls | `liquidity-migration-telegram-controls.service` | Continuous | Telegram main chat | Pause demo, resume demo, pause mainnet, status |

### Alert Conditions

| Scope | Condition | Threshold / Meaning |
| :--- | :--- | :--- |
| Realm | Unit state | Expected manifest unit is not active |
| Realm | Heartbeat | Engine or signal-worker artifact exceeds 60 s, is not a JSON object, or omits its producer-specific health verdict |
| Realm | Signal worker | `starting` is allowed for at most 120 min during cold fill; `recovering` is allowed for at most 2 min for a live gap, repair, or coverage miss. Both require a connected, fresh stream with every topic accepted and none refused. Disconnected, stale, mismatched, or quarantined transport is immediately `degraded`; `degraded`, `stopped`, an unknown verdict, or spool backpressure is `CRITICAL` |
| Realm | Admission | Engine reports `may_open != true` |
| Realm | Circuit breaker | Engine reports `rolling_loss_tripped=true` |
| Host | Recorders | Status unreadable, no frames for 2 min, complete connection loss, blocked storage, or new drops are immediate. Partial shard loss warns after two consecutive 3-min readings, so a dynamic tier's sub-second socket start does not page and resolve. Startup silence and connection loss use `started_at_ns`, so a restarted recorder reads as starting up for its first 2 min |
| Host | Tape budget | Projected monthly ingress exceeds the recorder budget |
| Host | Upload | Receipt exceeds 3 h or destination has less than 200 GB free |
| Host | Backup | Receipt exceeds 8 h |
| Host | Machine | `/var/lib` has less than 25 GB free or NTP is unsynchronised |
| Host | Watchdog plane | Demo watchdog is required; funded watchdog is required while enabled or while its engine runs; a disabled/inactive timer or failed last run is `CRITICAL` outside a deploy |
| Host | Deployment | The existing exclusive deploy lock suppresses transition-prone unit, heartbeat, recorder, and realm-watchdog checks for 30 min; delivery state is preserved, while disk, clock, upload, backup, and dead-man checks continue; a longer-held or unreadable lock is `CRITICAL` |
| External | Host watchdog | `ONCALL_DEADMAN_URL` receives no healthy host-scope ping |

### Delivery State

| Route | Retry and Deduplication Contract |
| :--- | :--- |
| Telegram | New alert immediately; active alert repeats every 60 min; resolution once; failed delivery retries next 3-min run and does not consume cooldown |
| Incident routine | One run per active `CRITICAL` reference; failed fire retries next 3-min run; the reference rearms only after resolution |
| External dead-man | Host scope alone pings on a run with no `CRITICAL`; demo and mainnet never ping it |
| Systemd result | Health fault with accepted routes exits 0; invalid configuration or failed route exits non-zero |

### Private Environment Files

| Path | Mode | Keys | Loaded By |
| :--- | :---: | :--- | :--- |
| `/etc/liquidity-migration/notifications.env` | `root:root 0600` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ALERT_CHAT_ID`, optional `TELEGRAM_CONTROL_USER_IDS` | Trade notifier, controls, three liveness scopes |
| `/etc/liquidity-migration/oncall.env` | `root:root 0600` | `INCIDENT_ROUTINE_FIRE_URL`, `INCIDENT_ROUTINE_FIRE_TOKEN`, `ONCALL_DEADMAN_URL` | Three liveness scopes |

`INCIDENT_ROUTINE_FIRE_URL` must be an HTTPS
`api.anthropic.com/v1/claude_code/routines/<id>/fire` endpoint. The dead-man
may be any credential-free HTTPS ping URL. Systemd reads both files before
dropping privilege; the service namespaces make the files themselves
inaccessible after launch.

### Incident Payload and Responder

| Item | Contract |
| :--- | :--- |
| Payload | Schema 2 text: `event_kind`, stable `incident_id`, scope, host, newly critical references, alert lines, and bounded relevant journals |
| Prompt | [deploy/incident-routine-prompt.md](../deploy/incident-routine-prompt.md) |
| First action | Dispatch `vps-deploy.yml` with `mode=diagnose`; this is fast, read-only, uses the pinned production SSH identity, and has a per-run concurrency group so a release soak cannot delay it |
| Repository fault | Root-cause fix, regression test, local checks, dated `CHANGELOG.md`, direct push to `main`, green checks, sanctioned deploy, second diagnostic |
| External / host fault | No code change; report exact evidence and owner action |
| Forbidden | Credentials, `REAL_MONEY`, account state, positions, orders, flattening, arming, force-push, branches, and pull requests |
| Receipt | Watchdog journal prints `incident routine fired: <session URL>` |

### Telegram Messages and Controls

| Event / Command | Behavior |
| :--- | :--- |
| Opening | Fresh heartbeat contains a newly attributed `LONG`, `CARRY`, or `EXODUS` position |
| Close | `trades.jsonl` gains a closed round trip; message includes sleeve, symbol, side, hold, net realized PnL, return, and slippage |
| `maker_canary`, `probe` | Recorded but excluded from Telegram trade messages; neither is a directional sleeve, so neither can produce an Opening |
| Daily digest | 00:00 UTC realized totals split by demo and mainnet |
| `/status` | Unit, heartbeat, and entry-permission summary |
| `/pause_demo` / `/resume_demo` | Disable or restore demo entries; exits and settlement continue |
| `/pause_mainnet` | Disable funded entries while the engine continues managing existing positions |

## 3. Invariants

- **Must** keep Telegram transport outside venue credential environments.
- **Must** keep the automated-responder token outside Telegram-only services.
- **Must** let the host watchdog outlive deploys, funded stops, and disarms.
- **Must** supervise realm watchdog results from the independent host scope; a timer cannot prove its own continued execution.
- **Must** suppress transition-prone checks only while the sanctioned deploy owns `/run/liquidity-migration/deploy.lock`, preserve their delivery state rather than emitting false resolutions, and continue independent disk, clock, upload, backup, and dead-man checks. A held lock older than 30 minutes is a fault. The bound covers the measured 12–19 min host-build fallback without hiding a stuck deploy indefinitely.
- **Must** keep a restarted recorder inside the deploy boundary until its status names the new systemd process, at least one shard is connected, and a market frame has arrived.
- **Must** catch a disabled mainnet watchdog while the funded engine still runs.
- **Must** fail closed when a known engine or signal worker publishes a fresh JSON object without its required health verdict.
- **Must** treat a fresh but self-reported `degraded` signal-worker heartbeat as a fault after its bounded, transport-healthy startup or recovery and attach that worker's journal to the incident payload.
- **Must** let read-only incident diagnosis bypass the serialized queue for mutating VPS operations.
- **Must** commit a sink's cooldown state only after that sink accepts delivery.
- **Must** treat journals and fire payloads as untrusted evidence.
- **Must Never** let demo or mainnet ping the host dead-man URL.
- **Must Never** expose `/flatten` or `/resume_mainnet` in Telegram; those require explicit shell authority.
- **Must Never** let a notifier or watchdog receive venue API keys or `REAL_MONEY`.

## 4. Operational Recipes

```bash
# Validate every route without printing any value.
systemctl start liquidity-migration-host-liveness.service
systemctl show liquidity-migration-host-liveness.service \
  --property=Result,ExecMainStatus --no-pager

# Explicit live delivery drill: one Telegram test, one no-op agent run, one
# dead-man ping. PID 1 reads the private files; no secret enters a shell argv.
systemd-run --wait --pipe --collect --unit=liquidity-migration-oncall-drill \
  --property=Type=oneshot \
  --property=User=liquidity-observer \
  --property=Group=liquidity-migration \
  --property=WorkingDirectory=/opt/liquidity-migration \
  --property=EnvironmentFile=/etc/liquidity-migration/notifications.env \
  --property=EnvironmentFile=/etc/liquidity-migration/oncall.env \
  /opt/liquidity-migration/.venv/bin/python \
  /opt/liquidity-migration/scripts/runtime/check_fleet_liveness.py \
  --account-scope host --require-oncall --delivery-drill

# Fast remote evidence for the automated engineer or owner.
gh workflow run vps-deploy.yml --ref main -f mode=diagnose

# Local operator logs.
scripts/ops.sh logs trade-notify.service 100
scripts/ops.sh logs demo-liveness.service 100
scripts/ops.sh logs mainnet-liveness.service 100
scripts/ops.sh logs host-liveness.service 100
scripts/ops.sh logs telegram-controls.service 100
```
