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
| `liquidity-migration-market-tape-upload.timer` | Global | `root:root` | Timer (hourly at :10) | Ships finished tape archives to Google Drive. |
| `liquidity-migration-backup.timer` | Global | `root:root` | Timer (every 6h) | Ships engine state & WAL to Google Drive. |

* **Independent Units**: `forward-capture`, `forward-capture-binance`, `market-tape-upload`, `backup`, and `host-liveness` are never stopped by fleet deploys or safety stops.

---

## 4. Deployment & Rollback Protocol

Deployments run via SSH using `scripts/deploy_vps_live.sh`:

```bash
EXPECTED_COMMIT=<40-hex-commit> scripts/ops.sh deploy
```

### Deployment Flow & Decoupled Handover
1. **Fetch & Verify**: Verifies target commit is on `origin/main`.
2. **Artifact Delivery**: Detects CI precompiled binary archive or builds locally via throttled cargo (`nice -n 10 --jobs 2`).
3. **Demo Handover**: Stops Demo $\to$ Installs release $\to$ Restores state $\to$ Starts Demo $\to$ Verifies fresh heartbeat within 180s.
   *(Mainnet continues actively trading during Demo upgrade).*
4. **Mainnet Atomic Swap**:
   - Pre-renders config and verifies attestor while Mainnet is live.
   - Executes atomic swap: `stop_realm_units mainnet` $\to$ `import state` $\to$ `start_realm mainnet`.
5. **Auto-Rollback**: If either realm fails to publish a fresh heartbeat within 180s, the script rolls back to `/opt/liquidity-migration-engine/deployed-commit`.

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
| **Market Tape Hours** | Hourly at :10 (`upload.timer`)| `LiquidityMigration/market-tape/<tape>/YYYY/MM/DD/` | Permanent archive |
* **Security Invariant**: Backup scripts explicitly reject `*.env` files to prevent credentials from ever leaving the host.

---

## 8. Incident Recovery Matrix

| Symptom | Probable Cause | Immediate Action |
| :--- | :--- | :--- |
| **Engine Heartbeat Stale ($> 30\text{s}$)** | Process crashed or deadlock | Check `scripts/ops.sh logs engine-mainnet 100`. Inspect WAL lock. |
| **Signal Worker Stale** | WebSocket disconnect or gap | Inspect `logs signal-worker-mainnet`. Engine continues exits independently. |
| **Rolling Loss Tripped** | 24h loss ceiling breached | Entries halted automatically. Exits permitted. Inspect `heartbeat.json`. |
| **Capture Dropping Frames** | CPU/disk saturation | Check `journalctl -u liquidity-migration-forward-capture`. Budget shedding will activate. |
| **Stranger Position Latched** | Unattributed fill on venue | Engine halts new entries. Run `attest-flat` and audit account on exchange. |
| **Engine exits with `signal source … has sequence gap: expected N, got N+1`** and loops under `Restart=always` | A spool row was deleted by something other than the engine (the worker only ever adds rows). Restarting does not help: the cursor is durable and the row is gone. If row N is still on disk, the spool reader has regressed (fixed 2026-09-03: a read the core dropped mid-flight lost its row); file it. | Start a new worker generation (recipe below). The engine treats `<source>.g<new>` as a fresh source starting at sequence 1 and keeps the old cursor. |
| **Engine logs `WARN invalid signal frame size; dropping the stream`** | A frame wider than 16 MiB (the worker sends none since 2026-09-03) or a lost frame boundary (fixed 2026-09-03, resumable frame state). Not fatal: the row is on disk and the spool poll delivers it. | None if the row was delivered. If it repeats every hour, a row is wider than the frame cap: shrink the payload. |
| **Worker exits with `spool class preflight underestimated an emitted observation batch`** | A `WireEvent` arm is missing from `projected_spool_files` (`engine/signal-worker/src/worker.rs`) for an event that emits a spool row. Every restart replays the same input and exits again. | Add the arm; the fix is a deploy. Nothing on the host needs cleaning. |
| **Worker logs `instrument lane: …` every hour** | One venue row failed a check and the whole snapshot was refused; the worker's instrument table stops refreshing (`instruments` in `checkpoint.json` stays stale or empty). | Read the exact message. Fix the check to the venue's real shape (see 2026-09-03 in CHANGELOG); never let one row cost the table. |
| **Any `CRITICAL` on the funded realm** | — | The watchdog pages the on-call agent ([docs/notifications.md](notifications.md) §On-call agent). The owner reads the run's PR. |

### New signal-worker generation

Use when the engine reports a sequence gap for a source. The worker keeps its universe and input history; only the output sequence restarts, under a new `g<generation>` in the source id. The worker republishes its current state on the next tick.

```bash
# on the host, as root; <realm> is demo or mainnet
systemctl stop liquidity-migration-signal-worker-<realm>
python3 - <<'EOF'
import json, os
path = "/var/lib/liquidity-migration-signal-worker-<realm>/checkpoint.json"
state = json.load(open(path))
state["source_generation"] = ""
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(state, fh, separators=(",", ":"))
    fh.flush(); os.fsync(fh.fileno())
os.rename(tmp, path)
EOF
chown liquidity-signal-worker:liquidity-migration /var/lib/liquidity-migration-signal-worker-<realm>/checkpoint.json
systemctl start liquidity-migration-signal-worker-<realm>
# Rows of the dead generation that sit above the engine's cursor are orphans:
# the engine reads them as the gap and exits. Remove them before it starts.
grep -l "\"source\":\"[^\"]*<old generation>" /var/lib/liquidity-migration/signals/<realm>/*.json | xargs -r rm -f
systemctl restart liquidity-migration-engine<-mainnet or empty>
journalctl -u liquidity-migration-engine<-mainnet or empty> -n 20 --no-pager
```

Rows of the old generation below the cursor are harmless: the engine ignores and retires them.
