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
| Realm | Heartbeat | Engine or signal-worker artifact exceeds 60 s, is not JSON, or is not a JSON object |
| Realm | Signal worker | A bounded `starting` state is allowed; `degraded`, `stopped`, an unknown verdict, or spool backpressure is `CRITICAL` |
| Realm | Admission | Engine reports `may_open != true` |
| Realm | Circuit breaker | Engine reports `rolling_loss_tripped=true` |
| Host | Recorders | Status unreadable, no frames for 2 min, connection loss, blocked storage, or new drops |
| Host | Tape budget | Projected monthly ingress exceeds the recorder budget |
| Host | Upload | Receipt exceeds 3 h or destination has less than 200 GB free |
| Host | Backup | Receipt exceeds 8 h |
| Host | Machine | `/var/lib` has less than 25 GB free or NTP is unsynchronised |
| Host | Watchdog plane | An enabled realm watchdog timer is inactive or its last run failed |
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
| `maker_canary` | Recorded but excluded from Telegram trade messages |
| Daily digest | 00:00 UTC realized totals split by demo and mainnet |
| `/status` | Unit, heartbeat, and entry-permission summary |
| `/pause_demo` / `/resume_demo` | Disable or restore demo entries; exits and settlement continue |
| `/pause_mainnet` | Disable funded entries while the engine continues managing existing positions |

## 3. Invariants

- **Must** keep Telegram transport outside venue credential environments.
- **Must** keep the automated-responder token outside Telegram-only services.
- **Must** let the host watchdog outlive deploys, funded stops, and disarms.
- **Must** supervise realm watchdog results from the independent host scope; a timer cannot prove its own continued execution.
- **Must** read a realm watchdog's requirement from its systemd enablement, never from a realm's runtime state; `enable --now` and `disable --now` move both together, so a deploy's teardown is not a fault.
- **Must** treat a fresh but self-reported `degraded` signal-worker heartbeat as a fault and attach that worker's journal to the incident payload.
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
