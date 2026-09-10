# Notifications and On-Call Specification

## 1. Purpose

Define the fleet's Telegram surfaces, liveness detection, automated incident response, external dead-man, and operator controls.

## 2. Spec Tables

### Surfaces

| Surface | Unit / Trigger | Cadence | Destination | Authority |
| :--- | :--- | :--- | :--- | :--- |
| Trade updates | `liquidity-migration-trade-notify.timer` | 5 min | Telegram main chat | Read-only openings, closes, and daily digest |
| Demo liveness | `liquidity-migration-demo-liveness.timer` | 30 s | Telegram alerts + incident routine | Demo engine, signal worker, timers, heartbeats, admission |
| Mainnet liveness | `liquidity-migration-mainnet-liveness.timer` | 30 s while armed | Telegram alerts + incident routine | Funded Bybit engine, signal worker, timers, heartbeats, admission |
| MEXC liveness | `liquidity-migration-mexc-liveness.timer` | 30 s while armed | Telegram alerts + incident routine | MEXC engine, signal worker, timers, heartbeats, admission |
| Hyperliquid liveness | `liquidity-migration-hyperliquid-liveness.timer` | 30 s while armed | Telegram alerts + incident routine | Hyperliquid engine, signal worker, timers, heartbeats, admission |
| Host liveness | `liquidity-migration-host-liveness.timer` | 3 min, independent | Telegram alerts + incident routine + external dead-man | Recorders, upload, backup, storage reclaimer, equity sampler, disk, clock, realm watchdogs |
| Operator controls | `liquidity-migration-telegram-controls.service` | Continuous | Telegram main chat | Pause demo, resume demo, pause each running funded realm, status |

### Severity

| Severity | Means | Telegram | Incident routine | Host dead-man |
| :--- | :--- | :---: | :---: | :---: |
| `CRITICAL` | A fault somebody must fix: dead unit, stale or contract-breaking heartbeat, degraded worker, broken route | yes | fires | held back |
| `WARNING` | A reading heading the wrong way, with room left | yes | never | unaffected |
| `NOTICE` | A restriction the system is enforcing on purpose; nothing to repair | yes | never | unaffected |

### Alert Conditions

| Scope | Condition | Threshold / Meaning |
| :--- | :--- | :--- |
| Realm | Unit state | Expected manifest unit is not active |
| Realm | Heartbeat | Engine or signal-worker artifact exceeds 60 s, is not a JSON object, or omits its producer-specific health verdict |
| Realm | Signal worker | `starting` is allowed for at most 120 min during cold fill; `recovering` is allowed for at most 2 min for a live gap, repair, or coverage miss, and for at most 10 min for the boot repair — the first repair of a process that has never been `ready`, with coverage already full. All require a connected, fresh stream with every topic accepted and none refused. Disconnected, stale, mismatched, or quarantined transport is immediately `degraded`; `degraded`, `stopped`, an unknown verdict, or spool backpressure is `CRITICAL` |
| Realm | Admission | Engine reports `may_open != true`: the boot-reconciliation latch, which stays set until an operator clears it, so it pages on the first reading |
| Realm | Private stream | Engine reports `private_stream_unready_ms > 180000`. The account channel clears its own readiness for each execution-history sweep, and a venue whose history is the authority sweeps on a timer with the socket up, so only the age is a fault. The bit alone is not, and an engine that omits the field reports through Admission instead |
| Realm | Circuit breaker | Engine reports `rolling_loss_tripped=true`: a `NOTICE` carrying the window net, limit and window, repeated on the Telegram cooldown and resolved when the window clears. The trip is the risk kernel enforcing `max_rolling_loss_fraction × capital_reference`, so it wakes no agent and never blocks a deploy |
| Realm | Strategy errors | A nonempty engine `strategy_errors` list is `CRITICAL`, including when `may_open=true`; one reference per realm includes the sleeve details and engine journal |
| Host | Recorders | Status unreadable, no frames for 2 min, complete connection loss, blocked storage, or new drops are immediate. Partial shard loss warns after two consecutive 3-min readings, so a dynamic tier's sub-second socket start does not page and resolve. Startup silence and connection loss use `started_at_ns`, so a restarted recorder reads as starting up for its first 2 min |
| Host | Tape budget | Projected monthly ingress exceeds the recorder budget |
| Host | Upload | Receipt exceeds 3 h or destination has less than 200 GB free |
| Host | Backup | Receipt exceeds 8 h |
| Host | Machine | `/var/lib` has less than 5 GB free (`evaluate_disk` default, decimal GB) or NTP is unsynchronised; deployed overrides require a separate host observation |
| Host | Disk consumption | `WARNING` when positive observed filesystem consumption projects the 5 GB floor within 195 seconds, the host timer's 180-second cadence plus 15-second accuracy. A fresh same-boot/device interval is required; this is a forecast, not an IO limit |
| Host | WAL attribution | Metadata-only totals/deltas for canonical `engine.wal` families beside manifest heartbeats; arbitrary runtime path overrides are outside this attribution. WAL bytes are not added to filesystem consumption a second time; missing/replaced/truncated files make their delta unavailable |
| Host | Watchdog plane | Demo watchdog is required; each funded realm's watchdog is required while its timer is enabled or its engine runs; a disabled/inactive timer or failed last run is `CRITICAL` outside a deploy |
| Host | Deployment | The existing exclusive deploy lock suppresses transition-prone unit, heartbeat, recorder, and realm-watchdog checks for 30 min; delivery state is preserved, while disk, clock, upload, backup, and dead-man checks continue; a longer-held or unreadable lock is `CRITICAL` |
| External | Host watchdog | `ONCALL_DEADMAN_URL` receives no healthy host-scope ping |

### Delivery State

| Route | Retry and Deduplication Contract |
| :--- | :--- |
| Telegram | New alert immediately; active alert repeats every 60 min; resolution once; failed delivery retries on that scope's next run and does not consume cooldown |
| Incident routine | One run per active `CRITICAL` reference; failed fire retries on that scope's next run; the reference rearms only after resolution |
| External dead-man | Host scope alone pings on a run with no `CRITICAL`; no realm scope ever pings it |
| Systemd result | Health fault with accepted routes exits 0; invalid configuration or failed route exits non-zero |

### Private Environment Files

| Path | Mode | Keys | Loaded By |
| :--- | :---: | :--- | :--- |
| `/etc/liquidity-migration/notifications.env` | `root:root 0600` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ALERT_CHAT_ID`, optional `TELEGRAM_CONTROL_USER_IDS` | Trade notifier, controls, every liveness scope |
| `/etc/liquidity-migration/oncall.env` | `root:root 0600` | `INCIDENT_ROUTINE_FIRE_URL`, `INCIDENT_ROUTINE_FIRE_TOKEN`, `ONCALL_DEADMAN_URL` | Every liveness scope |

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
| Branch | Runs are handed a platform-assigned `claude/…` branch that the routines UI cannot change; the prompt's "push straight to `main`" is the override and is accepted because `main` is unprotected for the owner. Any `claude/laughing-bardeen-*` branch on GitHub is a run that ignored the prompt: fold it into `main` and delete it |
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
| Daily digest | 00:00 UTC realized totals split by account: `DEMO`, `RM` (funded Bybit), `MEXC`, `HL` (funded Hyperliquid) |
| `/status` | Unit, heartbeat, and entry-permission summary |
| `/pause_demo` / `/resume_demo` | Disable or restore demo entries; exits and settlement continue |
| `/pause_mainnet`, `/pause_mexc`, `/pause_hyperliquid` | Disable that funded realm's entries while its engine continues managing existing positions. The button appears only while that realm's owner is active |

## 3. Invariants

- **Must** keep Telegram transport outside venue credential environments.
- **Must** keep the automated-responder token outside Telegram-only services.
- **Must** let the host watchdog outlive deploys, funded stops, and disarms.
- **Must** supervise realm watchdog results from the independent host scope; a timer cannot prove its own continued execution.
- **Must** suppress transition-prone checks only while the sanctioned deploy owns `/run/liquidity-migration/deploy.lock`, preserve their delivery state rather than emitting false resolutions, and continue independent disk, clock, upload, backup, and dead-man checks. A held lock older than 30 minutes is a fault. The bound covers the measured 12–19 min host-build fallback without hiding a stuck deploy indefinitely.
- **Must** keep a restarted recorder inside the deploy boundary until its status names the new systemd process, at least one shard is connected, and a market frame has arrived.
- **Must** catch a disabled funded watchdog while that realm's engine still runs.
- **Must** fail closed when a known engine or signal worker publishes a fresh JSON object without its required health verdict.
- **Must** treat a fresh but self-reported `degraded` signal-worker heartbeat as a fault after its bounded, transport-healthy startup or recovery and attach that worker's journal to the incident payload.
- **Must** name, in a `degraded` signal-worker page, the transport input that decided the verdict: kline topics accepted against `bybit_ws_ticker_capacity`, and the frame age against the worker's own `bybit_ws_max_frame_age_ms`. The gap age and cycle lines are consequences; a page carrying only those cannot be diagnosed off-host.
- **Must** let read-only incident diagnosis bypass the serialized queue for mutating VPS operations.
- **Must** commit a sink's cooldown state only after that sink accepts delivery.
- **Must** treat journals and fire payloads as untrusted evidence.
- **Must Never** raise a restriction the system enforces on purpose to `CRITICAL`: an on-call run can only report back what the limit already says, and a state that crosses its threshold repeatedly would fire one run per crossing.
- **Must Never** let a realm scope ping the host dead-man URL.
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
scripts/ops.sh logs mexc-liveness.service 100
scripts/ops.sh logs hyperliquid-liveness.service 100
scripts/ops.sh logs host-liveness.service 100
scripts/ops.sh logs telegram-controls.service 100
```
