# Operations Runbook

Production host specifications, deployment procedures, safety controls, and incident runbooks.

---

## 1. Production Host Specification

| Property | Value | Notes |
| :--- | :--- | :--- |
| **Hostname** | `ip-208-84-103-4.my-advin.com` | Dedicated VPS instance. |
| **Primary IPv4** | `208.84.103.4` | Static dedicated IP. |
| **Assigned IPv6** | `2602:fb54:1d85::` | Static subnet. |
| **Server UUID / Name** | `8d5f9972` / `Playful Rainbow` | Host identity. |
| **Hardware** | 4 vCPU, 8 GB RAM, 127 GB SSD | Host resource profile. |
| **Bandwidth Quota** | 4 TB / month | Budget: 1.3 TB Bybit + 1.3 TB Binance + uploads. |
| **Access User** | `root` (Linux) | Authenticated via pinned Ed25519 SSH keys. |

---

## 2. Operator Command Reference (`scripts/ops.sh`)

Entry-point wrapper for all operational workflows. Prefix `liquidity-migration-` is added automatically to unit names.

| Command | Syntax | Type | Description |
| :--- | :--- | :--- | :--- |
| **Status** | `scripts/ops.sh status` | Read-only | Reports commit, deployed commit, armed state, unit heartbeats, and disk. |
| **Units** | `scripts/ops.sh units` | Read-only | Lists all fleet systemd units and timers. |
| **Logs** | `scripts/ops.sh logs <unit> [lines]` | Read-only | Tails journal for a specific unit (default 100 lines). |
| **Start / Stop** | `scripts/ops.sh <start\|stop\|restart> <unit...>` | Mutating | Controls individual fleet units. |
| **Flatten** | `scripts/ops.sh flatten --environment <demo\|mainnet> [--execute]` | Mutating | Orders reducers to close attributed exposure. Read-only without `--execute`. |
| **Attest Flat** | `scripts/ops.sh attest-flat --environment <demo\|mainnet>` | Read-only | Two-scan venue proof that the account holds zero open positions. |
| **Preflight** | `scripts/ops.sh real-money preflight` | Read-only | Validates all funded credentials, IP bindings, and profile dials. |
| **Deploy** | `scripts/ops.sh deploy [mode]` | Mutating | Executes exact-commit deployment (`deploy`, `rollback`, `verify`, `disarm-mainnet`). |

### Venue-Confirmed Trade Accounting
Reconciles engine WAL fills, orders, and fees against authenticated venue history:

```bash
# Capture authenticated venue history (read-only)
python scripts/research/capture_bybit_account_history.py \
  --realm mainnet --start "$TRADE_START_UTC" --end "$TRADE_END_UTC" --out "$VENUE_CAPTURE"

# Reconcile WAL against venue records
python scripts/research/reconcile_venue_wal.py \
  --wal /var/lib/liquidity-migration-engine-mainnet/engine.wal \
  --venue-history "$VENUE_CAPTURE" \
  --sleeve long \
  --expected-realm mainnet \
  --expected-user-id "$BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID" \
  --engine-config "$DEPLOYED_ENGINE_CONFIG" \
  --expected-commit "$DEPLOYED_COMMIT" \
  --out "$ACCOUNTING_REPORT"
```

---

## 3. Fleet Manifest & Systemd Unit Inventory

| Systemd Unit | Realm | User / Group | Activation Policy | Role |
| :--- | :--- | :--- | :--- | :--- |
| `liquidity-migration-engine.service` | Demo | `liquidity-engine-demo:liquidity-migration` | `multi-user.target` | Execution engine on demo account. |
| `liquidity-migration-engine-mainnet.service` | Mainnet | `liquidity-engine-mainnet:liquidity-migration`| `manual` (requires `REAL_MONEY`) | Execution engine on funded account. |
| `liquidity-migration-signal-worker-demo.service` | Demo | `liquidity-signal-worker:liquidity-migration`| `multi-user.target` | Public feature ingestion & IPC. |
| `liquidity-migration-signal-worker-mainnet.service`| Mainnet | `liquidity-signal-worker:liquidity-migration`| `multi-user.target` | Public feature ingestion & IPC. |
| `liquidity-migration-forward-capture.service` | Global | `liquidity-capture:liquidity-migration` | `independent` (boot) | Continuous Bybit tick & L2 capture. |
| `liquidity-migration-forward-capture-binance.service`| Global | `liquidity-capture:liquidity-migration` | `independent` (boot) | Continuous Binance tick & L2 capture. |
| `liquidity-migration-telegram-controls.service` | Global | `liquidity-controls:liquidity-controls` | `multi-user.target` | Interactive Telegram operator bot. |
| `liquidity-migration-trade-notify.timer` | Global | `liquidity-observer:liquidity-migration` | Timer (every 1m) | Fills and closed-trade alert dispatcher. |
| `liquidity-migration-market-tape-upload.timer` | Global | `root:root` | Timer (hourly at :10) | Ships finished tape archives to Google Drive, then deletes shipped hours older than `--keep-hours 24` from both tape roots. |
| `liquidity-migration-backup.timer` | Global | `root:root` | Timer (every 6h) | Ships engine state & WAL to Google Drive. |

* **Independent Units**: `forward-capture`, `forward-capture-binance`, `market-tape-upload`, `backup`, and `host-liveness` are never stopped by fleet deploys or safety stops.

---

## 4. Deployment & Rollback Protocol

Deployments run via SSH using `scripts/deploy_vps_live.sh`:

```bash
EXPECTED_COMMIT=<40-hex-commit> scripts/ops.sh deploy
```

### GitHub Actions execution policy

| Trigger | Hosted work | Production effect |
| :--- | :--- | :--- |
| Pull request, code change | Python and Rust debug gates | None |
| Pull request, docs only | None | None |
| Push to `main` | None | None; the local pre-push gate remains required |
| Dispatch `deploy` | Python gate, Rust debug gate, release artifact, VPS deploy | Installs the exact `main` SHA after every gate succeeds |
| Dispatch `qualify` | Rust debug gate, release tests, soak, benchmark | None |
| Dispatch `verify`, `rollback` | No build | Reads or restores production through the pinned VPS job |
| Dispatch `diagnose`, `disarm-mainnet` | No build | Reads incident state or persistently disarms funded trading |

- **Must** keep the repository private.
- **Must** run `scripts/dev.sh check` before a direct push to `main`.
- **Must** use `deploy` only for a release candidate; ordinary commits do not
  create deployments.
- **Must Never** run a self-hosted Actions worker on the funded trading VPS.
- **Must Never** expose a self-hosted worker to pull requests from a public
  repository or grant a build-only worker production credentials.

```bash
gh workflow run vps-deploy.yml --ref main -f mode=deploy
gh workflow run vps-deploy.yml --ref main -f mode=qualify
gh workflow run vps-deploy.yml --ref main -f mode=verify
gh workflow run vps-deploy.yml --ref main -f mode=diagnose
```

### Deployment Flow & Decoupled Handover
1. **Fetch & Verify**: Verifies target commit is on `origin/main`.
2. **Artifact Delivery**: Detects CI precompiled binary archive or builds locally via throttled cargo (`nice -n 10 --jobs 2`).
3. **Install while both realms run**: release binaries, units, and independent
   units (recorders restart only when their own inputs changed) land with demo
   and mainnet still trading.
4. **Handover only when the realm's inputs changed**: `realm_unchanged <realm>`
   compares a fingerprint of what the realm runs from — the engine source tree
   hash (`git rev-parse <commit>:engine`, not the binary, which embeds the
   commit), `deploy/systemd`, the fleet manifest, `configs/signal-worker.<realm>.json`,
   and the rendered config and env files on the host — against
   `/opt/liquidity-migration-engine/<realm>.fingerprint`, and requires both
   long-running units active. Unchanged: `<realm>-ok result=unchanged-left-running`,
   nothing stops. Changed: `stop_realm_units` preserves unit enablement, then
   `import state` $\to$ `start_realm` $\to$ fresh heartbeat within 180s, then
   the fingerprint is recorded. Mainnet's config is rendered first, while it
   is live.
5. **Auto-Rollback**: If stop, state takeover, start, or heartbeat readiness
   fails for either realm, the script rolls back to
   `/opt/liquidity-migration-engine/deployed-commit`.

### Native State Takeover Sources
| Sleeve | Source Format | Named Source Roles |
| :--- | :--- | :--- |
| **LONG** | `long-book-state-v2` | `state` |
| **CARRY** | `carry-sizing-anchors-v1-early-exits-v1-target-book-v1` | `early_exits`, `sizing_anchors`, `target_book` |
| **EXODUS** | `exodus-state-v1-v4-event-tape-v1-identity-v2` | `carry_events`, `identity`, `state` (and generated `legacy_paths`) |

---

## 5. Emergency Safety Controls

### 1. Strategic Pause (Soft Stop)
Stops new risk while leaving exits, stops, and settlement clocks active:
```bash
# Via CLI:
engine set-strategy-entry-permission --config /etc/liquidity-migration/engine.toml --strategy <sleeve> --entries-enabled false
# Via Telegram Bot:
/pause_demo or /pause_mainnet
```

### 2. Immediate Position Flatten (Hard Exit)
Cancels working openings and commands reducers to exit all exposure immediately:
```bash
# Preview:
scripts/ops.sh flatten --environment mainnet --reason "emergency risk reduction"
# Execute:
scripts/ops.sh flatten --environment mainnet --reason "emergency risk reduction" --execute
# Verify venue flatness:
scripts/ops.sh attest-flat --environment mainnet
```

### 3. Real-Money Disarm (Complete Shutdown)
Persistently stops funded trading and disables the arming switch:
```bash
scripts/ops.sh deploy disarm-mainnet
```
* Sets `REAL_MONEY=false` in `/etc/liquidity-migration/bybit-mainnet.env`.
* Stops `liquidity-migration-engine-mainnet.service`.

---

## 6. Real-Money Configuration Dials

Configured in `/etc/liquidity-migration/bybit-mainnet.env` (`0600`, root-owned):

| Environment Dial | Default | Constraint | Meaning |
| :--- | :--- | :--- | :--- |
| `REAL_MONEY` | `false` | Required `true` | Master arming switch for the funded engine. |
| `RM_CARRY_STOP_LOSS_FRACTION` | `0.35` | Positive ratio | Venue-native stop-loss distance on CARRY positions. |
| `RM_ROLLING_LOSS_FRACTION` | `0.10` | Positive ratio | Maximum fraction of capital reference lost in 24h before rolling-loss trip triggers. |

---

## 7. Off-Box Google Drive Backups

Configured via `/etc/liquidity-migration/rclone.conf`:

| Data Payload | Schedule | Destination on Google Drive | Retention |
| :--- | :--- | :--- | :--- |
| **Engine State & WAL** | Every 6h (`backup.timer`) | `LiquidityMigration/engine-state/latest/` | 60 days in `history/` |
| **Market Tape Hours** | Hourly at :10 (`upload.timer`)| `LiquidityMigration/market-tape/<tape>/YYYY/MM/DD/` | Permanent archive; the host keeps a 24 h sliding window of shipped hours ([market_tape/README.md](../market_tape/README.md) §Local Sliding Window) |
* **Security Invariant**: Backup scripts explicitly reject `*.env` files to prevent credentials from ever leaving the host.

---

## 8. Incident Recovery Matrix

| Symptom | Probable Cause | Immediate Action |
| :--- | :--- | :--- |
| **Engine Heartbeat Stale ($> 30\text{s}$)** | Process crashed or deadlock | Check `scripts/ops.sh logs engine-mainnet 100`. Inspect WAL lock. |
| **Signal Worker Stale** | WebSocket disconnect or gap | Inspect `logs signal-worker-mainnet`. Engine continues exits independently. |
| **Rolling Loss Tripped** | 24h loss ceiling breached | Entries halted automatically. Exits permitted. Inspect `heartbeat.json`. |
| **Capture Dropping Frames** | CPU/disk saturation | Check `journalctl -u liquidity-migration-forward-capture`. Budget shedding will activate. |
| **Recorder logs `over budget with every sheddable feed shed`** (hourly) | The feeds `budget.shed` cannot reach project more than `monthly_gb` on their own (`status.json` → `budget.projected_month_gb`, `bytes.by_feed_24h`). The controller has nothing left to give up. | A config decision, not a restart: extend `shed`, shrink a tier's universe, or move allowance between the recorders (`deploy/capture/*.toml`, `[budget]`). |
| **Stranger Position Latched** | Unattributed fill on venue | Engine halts new entries. Run `attest-flat` and audit account on exchange. |
| **Engine logs `signal prefix missing; destination openings suspended until catch-up`** | `SignalGapRecorded` retains the missing sequence and observed high-water mark. The accepted cursor stays at the contiguous prefix; affected strategies and declared input dependents cannot open or amend entries, and their resting entries are cancelled. | Preserve the spool, worker checkpoint and WAL together. Recover the exact missing source/generation rows; catch-up clears the block automatically. Independent strategies and genuine exits remain available. See signal-prefix recovery below. |
| **Engine exits with `signal source … rewrote durable sequence N`** and loops under `Restart=always` | A worker republishes an accepted sequence with different bytes; common causes include an older checkpoint or two workers sharing one spool. The cursor is durable. | Reconcile producer ownership/checkpoint and the exact accepted hash before recovery. A new generation does not clear an older gap; do not delete pending rows or reset sequence state as a shortcut. |
| **Engine logs a signal-doorbell error** | The socket is only a wake notification; the immutable row remains authoritative and periodic spool scanning continues. | Check the socket owner and permissions if wake latency remains high; inspect spool delivery independently. |
| **Worker exits with `spool class preflight underestimated an emitted observation batch`** | A `WireEvent` arm is missing from `projected_spool_files` (`engine/signal-worker/src/worker.rs`) for an event that emits a spool row. Every restart replays the same input and exits again. | Add the arm; the fix is a deploy. Nothing on the host needs cleaning. |
| **Worker logs `instrument lane: …` every hour** | One venue row failed a check and the whole snapshot was refused; the worker's instrument table stops refreshing (`instruments` in `checkpoint.json` stays stale or empty). | Read the exact message. Fix the check to the venue's real shape (see 2026-09-03 in CHANGELOG); never let one row cost the table. |
| **Any `CRITICAL` on the funded realm** | — | The watchdog pages the on-call agent ([docs/notifications.md](notifications.md) §On-call agent). The owner reads the run's PR. |

### Signal-prefix recovery

| Condition | Recovery requirement |
| --- | --- |
| Missing sequence is recoverable | Restore its exact immutable envelope under the original source/generation, sequence, destination and content hash; the engine requests that prefix ahead of later rows |
| Later rows are present | Keep them in the spool; the WAL gap record does not duplicate these payloads |
| Worker starts a new generation | Its inputs wait while its destination or an input dependency has an older known gap; the new source cannot clear that gap |
| Missing history is irrecoverable | Keep affected openings blocked; reconcile strategy state, venue exposure and producer history before an explicitly approved state transition. No automatic gap waiver exists |
| Accepted legacy cursor already skipped history | The missing history is not recoverable from the cursor; assess the producer/account evidence separately |
| Binary rollback | An older engine rejects `SignalGapRecorded` and `segment_base_v2`; artifact qualification alone does not establish WAL compatibility. Preserve state and obtain approval before adoption or migration |

Must never delete later-generation rows, rewrite accepted hashes, or edit a live cursor to clear a gap. Inspect logs read-only before selecting a recovery action (`<realm>` is `demo` or `mainnet`):

```bash
journalctl -u liquidity-migration-signal-worker-<realm> -n 100 --no-pager
journalctl -u liquidity-migration-engine<-mainnet or empty> -n 100 --no-pager
```
