# Operations Runbook

## Purpose

Define host configuration, deployment behavior and incident recovery commands.

## Spec Tables

### Production Host Specification

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

### Operator Command Reference (`scripts/ops.sh`)

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

### Fleet Manifest & Systemd Unit Inventory

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

| Independent families | Deploy behavior |
| --- | --- |
| `forward-capture`, `forward-capture-binance`, `market-tape-upload`, `backup`, `equity-recorder`, `host-liveness` | Remain running through realm handover and disarm; a recorder restarts when its own unit, configuration or runtime inputs change |

---

### Deployment & Rollback Protocol

Deployments run via SSH using `scripts/deploy_vps_live.sh`:

```bash
EXPECTED_COMMIT=<40-hex-commit> scripts/ops.sh deploy
```

### GitHub Actions execution policy

| Trigger | Hosted work | Production effect |
| :--- | :--- | :--- |
| Pull request, code change | Python and Rust debug gates | None |
| Pull request, docs only | None | None |
| Push to `main` | Python and Rust debug gates | None; the local pre-push gate remains required |
| Dispatch `deploy` | Python gate, Rust debug gate, release artifact, VPS deploy | Installs the exact `main` SHA after every gate succeeds |
| Dispatch `qualify` | Rust debug gate, release tests, soak, benchmark | None |
| Dispatch `verify`, `rollback` | No build | Reads or restores production through the pinned VPS job |
| Dispatch `diagnose`, `disarm-mainnet` | No build | Reads incident state or persistently disarms funded trading |

- **Must** keep account state, credentials and private operational evidence outside the public repository.
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

| Phase | Implemented behavior |
| --- | --- |
| Exact source | Fetch and verify the requested commit belongs to `origin/main`. A backward deployment uses the same compatibility decision as rollback before moving the checkout. |
| Artifact delivery | Verify and install the CI-built archive for that commit; missing artifacts fail before stopping the fleet. The funded host does not compile releases. |
| Installation | Binaries and units land while both realms run. Independent recorders restart only when their own inputs change. |
| Equity recorder handover | Before checkout can remove the Python entrypoint, a recorder-only systemd override selects the verified companion in `releases/<commit>/`. A blocking oneshot start completes any existing invocation; an idle observer takes one deployment-time sample. Failed checkout or soak retains that executable and override. Successful global installation removes the override; funded runtime binaries still wait for the demo soak |
| Realm handover | Compare the engine source tree, systemd units, fleet manifest, worker config and rendered realm inputs with the retained fingerprint. Unchanged active realms keep running; changed realms stop, apply any explicit legacy retirement plan, verify canonical native state or initialize an empty WAL with no legacy source files, apply any explicit reconciliation note, then restart. |
| Readiness | Require a fresh heartbeat and the same active main PID/restart counter throughout the 12-second settle window before recording the realm fingerprint. |
| Failed handover or fleet rollback | A predecessor must have identical Rust, dependency, toolchain and build inputs to the current checkout and recorded deployed generation. Incompatible or unavailable inputs leave the installed candidate and durable state in place for forward repair. The worker has no read-only state compatibility command, so a changed-runtime rollback is not inferred safe. |
| Default demo rollback/drill | `scripts/runtime/chaos_drill.sh rollback\|drill` selects the recorded previous generation under the deployment lock and requires identical runtime inputs. `restore` selects the completed current generation without requiring a predecessor |
| Selected demo pair | `rollback\|drill --qualified-pair EXPECTED_CURRENT_SHA PREDECESSOR_SHA` selects a retained archive independently of `previous-commit`. The caller must qualify the specific runtime/state compatibility before using this input. The helper requires the completed deployment to equal the supplied current SHA under the lock; a failed predecessor startup restores that current release. `restore` rejects pair arguments |
| Pair qualification scope | Full-SHA pair selection permits its reviewed source difference; it does not suppress archive, WAL or worker-state errors. A copied-WAL read establishes record readability only. Loaded images, fresh process/account readiness and successful restoration require an actual demo drill |
| Durable state | Rollback never restores old WAL or worker files over newer state; required record refusal remains explicit. |
| Legacy Python snapshots | Deployment refuses missing or invalid canonical native state when the WAL is nonempty or any required legacy source file exists. Recovery uses a retained compatible release with the complete source bundle and matching account/configuration; the current release has no Python state importer. |
| Retained WAL recovery | Ordinary readers accept segment v1/v7; `wal-convert-v5` privately reads v5 into a separate complete family. Original families remain recoverable through the compatible `8c92c964` release pinned in [STATE.md](../STATE.md). Preserve original source segments, unresolved callback sources and compatible binaries until their replay/rollback requirement is retired. Copied conversion does not authorize host conversion or pruning. [Retained input assembly and qualification](https://github.com/rob435/liquidity-migration/blob/29366d3a2013701a0956a2a471a7c916bf6980e2/docs/tier1-round-handoff.md#L80-L86) records the exact prefix/tail boundaries; a quarantined suffix alone is not a complete family. |

### Native State Initialization

| Existing source checked before empty initialization | Path |
| --- | --- |
| LONG state | `/var/lib/liquidity-migration/targets/long-${realm}-state.json` |
| CARRY sizing anchors | `${carry_root}/.cache/carry_sizing_anchors.json` |
| CARRY target book | `/var/lib/liquidity-migration/targets/carry-${realm}.json` |
| EXODUS identity | `${exodus_root}/exodus_state_identity.json` |
| EXODUS state | `${exodus_root}/exodus_state.json` |
| Root selection | `scripts/vps/deploy_remote.sh::ensure_native_strategy_state` selects the configured CARRY and EXODUS roots for each realm |
| Fresh initialization | All five paths absent and WAL empty; verified canonical state preserves legacy files in place |

---

### Emergency Safety Controls

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

### Real-Money Configuration Dials

Configured in `/etc/liquidity-migration/bybit-mainnet.env` (`0600`, root-owned):

| Environment Dial | Default | Constraint | Meaning |
| :--- | :--- | :--- | :--- |
| `REAL_MONEY` | `false` | Required `true` | Master arming switch for the funded engine. |
| `RM_CARRY_STOP_LOSS_FRACTION` | `0.35` | Positive ratio | Venue-native stop-loss distance on CARRY positions. |
| `RM_ROLLING_LOSS_FRACTION` | `0.10` | Positive ratio | Maximum fraction of capital reference lost in 24h before rolling-loss trip triggers. |

---

### Off-Box Google Drive Backups

Configured via `/etc/liquidity-migration/rclone.conf`:

| Data Payload | Schedule | Destination on Google Drive | Retention |
| :--- | :--- | :--- | :--- |
| **Engine State & WAL** | Every 6h (`backup.timer`) | `LiquidityMigration/engine-state/latest/` | 60 days in `history/` |
| **Market Tape Hours** | Hourly at :10 (`upload.timer`)| `LiquidityMigration/market-tape/<tape>/YYYY/MM/DD/` | Permanent archive; the host keeps a 24 h sliding window of shipped hours ([market_tape/README.md](../market_tape/README.md) §Local Sliding Window) |
* **Security Invariant**: Backup scripts explicitly reject `*.env` files to prevent credentials from ever leaving the host.

---

### Incident Recovery Matrix

| Symptom | Probable Cause | Immediate Action |
| :--- | :--- | :--- |
| **Engine Heartbeat Stale (>60 s)** | Process crashed or deadlock | Check `scripts/ops.sh logs engine-mainnet 100`. Inspect WAL lock. |
| **Signal Worker Stale** | WebSocket disconnect or gap | Inspect `logs signal-worker-mainnet`. Engine continues exits independently. |
| **Engine `strategy_errors` is nonempty** | A sleeve reports a callback, checkpoint or source fault, even if other entries remain permitted | Inspect the named sleeve errors and engine journal; the existing realm incident route pages independently of `may_open`. |
| **Engine or worker reaches `start-limit-hit`** | Five starts within 300 seconds exhaust the unit's restart budget | Fix the cause, then reset and start the affected unit using [systemd recovery commands](../deploy/systemd/README.md). Exhaustion does not flatten holdings or automatically retry when the window expires. |
| **Host `disk-growth` warning** | Recent filesystem consumption projects the existing 5 GB free-space floor before the next normal liveness observation | Inspect the reported canonical WAL delta, tape usage and local backup stage. Retained WAL supports late-order recovery; preserve its family. Rotation is not a storage quota. |
| **Rolling Loss Tripped** | 24h loss ceiling breached | Entries halted automatically. Exits permitted. Inspect `heartbeat.json`. |
| **Capture Dropping Frames** | CPU/disk saturation | Check `journalctl -u liquidity-migration-forward-capture`. Budget shedding will activate. |
| **Recorder logs `over budget with every sheddable feed shed`** (hourly) | The feeds `budget.shed` cannot reach project more than `monthly_gb` on their own (`status.json` → `budget.projected_month_gb`, `bytes.by_feed_24h`). The controller has nothing left to give up. | A config decision, not a restart: extend `shed`, shrink a tier's universe, or move allowance between the recorders (`deploy/capture/*.toml`, `[budget]`). |
| **Stranger Position Latched** | Unattributed fill on venue | Engine halts new entries. Run `attest-flat` and audit account on exchange. |
| **Engine logs `signal prefix missing; destination openings suspended until catch-up`** | `SignalGapRecorded` retains the missing sequence and observed high-water mark. The accepted cursor stays at the contiguous prefix; affected strategies and declared input dependents cannot open or amend entries, and their resting entries are cancelled. | Preserve the spool, worker checkpoint and WAL together. Recover the exact missing source/generation rows; catch-up clears the block automatically. Independent strategies and genuine exits remain available. See signal-prefix recovery below. |
| **Engine exits with `signal source … rewrote durable sequence N`** and loops under `Restart=always` | A worker republishes an accepted sequence with different bytes; common causes include an older checkpoint or two workers sharing one spool. The cursor is durable. | Reconcile producer ownership/checkpoint and the exact accepted hash before recovery. A new generation does not clear an older gap; do not delete pending rows or reset sequence state as a shortcut. |
| **Engine logs a signal-doorbell error** | The socket is only a wake notification; the immutable row remains authoritative and periodic spool scanning continues. | Check the socket owner and permissions if wake latency remains high; inspect spool delivery independently. |
| **Worker exits with `spool class preflight underestimated an emitted observation batch`** | A `WireEvent` arm is missing from `projected_spool_files` (`engine/signal-worker/src/worker.rs`) for an event that emits a spool row. Every restart replays the same input and exits again. | Add the arm; the fix is a deploy. Nothing on the host needs cleaning. |
| **Worker logs `instrument lane: …` every hour** | One venue row failed a check and the whole snapshot was refused; the worker's instrument table stops refreshing (`instruments` in `checkpoint.json` stays stale or empty). | Read the exact message. Fix the check to the venue's real shape ; never let one row cost the table. |
| **Any `CRITICAL` on the funded realm** | — | The watchdog pages the on-call agent ([docs/notifications.md](notifications.md) §On-call agent). The owner reads the on-call result. |

### Signal-prefix recovery

| Condition | Recovery requirement |
| --- | --- |
| Missing sequence is recoverable | Restore its exact immutable envelope under the original source/generation, sequence, destination and content hash; the engine requests that prefix ahead of later rows |
| Later rows are present | Keep them in the spool; the WAL gap record does not duplicate these payloads |
| Worker starts a new generation | Its inputs wait while its destination or an input dependency has an older known gap; the new source cannot clear that gap |
| Missing history is irrecoverable | Reconcile strategy state, venue exposure and producer history. For a permanently stopped legacy source, the offline retirement command records the final published ceiling and disposition of its unprocessed suffix without advancing its accepted cursor. Managed sources cannot use this transition |
| Accepted legacy cursor already skipped history | The missing history is not recoverable from the cursor; assess the producer/account evidence separately |
| Binary rollback | Required WAL records and `segment_base_v7` make incompatible readers refuse; deployment permits predecessor recovery only when its runtime inputs match. Preserve all durable state and repair forward otherwise |

Must never delete later-generation rows, rewrite accepted hashes, or edit a live cursor to clear a gap. Inspect logs read-only before selecting a recovery action (`<realm>` is `demo` or `mainnet`):

```bash
journalctl -u liquidity-migration-signal-worker-<realm> -n 100 --no-pager
journalctl -u liquidity-migration-engine<-mainnet or empty> -n 100 --no-pager
```

### Resolve verified historical physical residue

| Field | Contract |
| --- | --- |
| Pending note | `/etc/liquidity-migration/reconcile-clear.<realm>.note`, root owned, mode `0600`; exact historical execution evidence path and hash |
| Handover | After native-state verification, before startup, the release binary runs `reconcile-clear --execute` under the existing realm runtime identity and credential environment |
| Ownership | Native quantities remain exact. Every currently owned net position must match authenticated native exposure after eligible legacy grid resolution; another missing close requires history recovery |
| Completion | A successful clear renames the note to `.note.applied`. Failure preserves the pending note and prevents startup. A pending note prevents unchanged-realm skipping; identical interrupted WAL append retries write nothing |
| Protection | Canonical sleeve stops survive restatement. Boot confirms native protection from sleeve inventory and exact metadata; unmet protection remains pending for repair or reduction |

```sh
# Run through the existing realm credential environment with the engine stopped.
engine reconcile-clear --config /etc/liquidity-migration/engine.toml \
  --note 'verified historical execution; private evidence path and SHA256'
engine reconcile-clear --config /etc/liquidity-migration/engine.toml \
  --note 'verified historical execution; private evidence path and SHA256' --execute
```

### Retire a permanently stopped legacy signal source

| Item | Contract |
| --- | --- |
| Plan path | `/etc/liquidity-migration/legacy-signal-retirements.<realm>.json`, root owned, group `liquidity-migration`, mode `0640` |
| JSON schema | Array of `{ "source": "exact legacy namespace", "published_through": 7, "reason": "publication evidence and unprocessed suffix disposition" }` |
| Execution | Existing engine WAL lock; engine and worker stopped. The full batch validates before any append. Identical retries append nothing; changed outcomes fail |
| Durable result | `LegacySignalSourceRetired` preserves destination and accepted cursor, records the published ceiling and reason, rejects later arrivals, and permits lifecycle completion. Rotation retains the result in `segment_base_v7` |
| Refusals | Managed or unknown source, pending accepted observation, inconsistent publication ceiling, changed retry, running WAL writer, or torn WAL tail |
| Evidence | Preserve original worker checkpoints, WAL family and recovered payloads. Distinguish irrecoverable payloads from retained rows deliberately left unapplied |

- Must never invent a missing envelope, consumption record or accepted cursor.
- Must never retire a producer that can still publish under the source being retired.
- Must preserve protective stops, reductions and reconciled ownership during the realm handover.

```bash
# Use the stopped realm's runtime user and config. Omit --execute to inspect the result.
engine retire-legacy-signal-sources \
  --config /etc/liquidity-migration/engine-mainnet.toml \
  --plan /etc/liquidity-migration/legacy-signal-retirements.mainnet.json
engine retire-legacy-signal-sources \
  --config /etc/liquidity-migration/engine-mainnet.toml \
  --plan /etc/liquidity-migration/legacy-signal-retirements.mainnet.json --execute
```

## Invariants

- Must preserve current WAL, signal prefixes and native protection during recovery.
- Must validate the exact release artifact before installation and verify each realm after handover.
- Must keep account credentials and private evidence outside the public checkout.
- Must treat incompatible-reader rollback as forward repair; never replace newer durable state with a backup merely to boot an old binary.

## Operational Recipes

```sh
scripts/ops.sh status
scripts/ops.sh --help
scripts/dev.sh check
# Select the full intended source SHA before dispatching deployment.
gh workflow run vps-deploy.yml --ref main -f mode=deploy
```

Run a specifically qualified demo pair as root on the host; set both full SHAs from the completed compatibility review. Systemd reads the existing drill environment files.

```sh
: "${TASK_CURRENT_SHA:?Set the reviewed completed deployment SHA}"
: "${TASK_PREDECESSOR_SHA:?Set the reviewed retained predecessor SHA}"
systemd-run --quiet --wait --pipe --collect --service-type=oneshot \
  --unit="liquidity-migration-demo-drill-manual-$$" \
  --property=WorkingDirectory=/opt/liquidity-migration \
  --property=EnvironmentFile=/etc/liquidity-migration/engine.env \
  --property=EnvironmentFile=/etc/liquidity-migration/notifications.env \
  --property=EnvironmentFile=/etc/liquidity-migration/oncall.env \
  --property='Environment=TELEGRAM_ENABLED=1 PYTHONDONTWRITEBYTECODE=1' \
  --property='UnsetEnvironment=BYBIT_DEMO_API_KEY BYBIT_DEMO_API_SECRET BYBIT_REAL_API_KEY BYBIT_REAL_API_SECRET BYBIT_REAL_API_KEY_IP BYBIT_REAL_API_KEY_BACKUP_IP BYBIT_ATTEST_API_KEY BYBIT_ATTEST_API_SECRET BYBIT_ATTEST_API_KEY_IP BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID REAL_MONEY' \
  --property=NoNewPrivileges=true --property=PrivateTmp=true \
  --property=MemoryMax=256M --property=TimeoutStartSec=900 \
  /opt/liquidity-migration/scripts/runtime/chaos_drill.sh drill \
  --qualified-pair "$TASK_CURRENT_SHA" "$TASK_PREDECESSOR_SHA"
```
