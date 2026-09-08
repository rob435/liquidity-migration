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
| **Flatten** | `scripts/ops.sh flatten --environment <demo\|mainnet\|mexc\|hyperliquid> [--execute]` | Mutating | Orders reducers to close attributed exposure. Read-only without `--execute`. |
| **Attest Flat** | `scripts/ops.sh attest-flat --environment <demo\|mainnet\|mexc\|hyperliquid>` | Read-only | Two-scan venue proof that the account holds zero open positions. Bybit, MEXC and Hyperliquid implement the GET-only credential-wide probe; MEXC's scan covers the futures account (assets, positions, open orders, position stops), Hyperliquid's covers perpetual positions, the cross-margin account value, working orders including reduce-only triggers, and spot token balances. |
| **Preflight** | `scripts/ops.sh real-money preflight` | Read-only | Validates all funded Bybit credentials, IP bindings, and profile dials. |
| **MEXC preflight** | `scripts/ops.sh real-money preflight-mexc` | Read-only | Validates the MEXC credential file, its arming switch, and the mexc worker source. |
| **Hyperliquid preflight** | `scripts/ops.sh real-money preflight-hyperliquid` | Read-only | Validates the Hyperliquid credential file (address shape, API wallet key shape, no other venue's keys), its arming switch, and the hyperliquid worker source. |
| **Verify Identity** | `scripts/ops.sh verify-account-identity --environment <demo\|mainnet\|mexc\|hyperliquid>` | Read-only | Authenticates the realm's GET-only probe and binds it to `EXPECTED_ENGINE_ACCOUNT_USER_ID`; a mismatch prints the id the credentials answered as. |
| **Canary Order** | `scripts/ops.sh canary-order --environment <demo\|mexc\|hyperliquid> --symbol SYMBOL --expected-user-id ID [--execute]` | Mutating with `--execute` | One bounded live order lifecycle through the realm's own credential file: one venue-minimum post-only order 0.5% under the bid, cancelled, the account proved clean twice. The engine accepts it on the Bybit demo and on `live-canary` realms only; `hyperliquid_mainnet` is the one `live-canary` realm today, and `mexc_mainnet` is `live-proven`, so it is refused there. |
| **Deploy** | `scripts/ops.sh deploy [mode]` | Mutating | Executes exact-commit deployment (`deploy`, `rollback`, `verify`, `stop-mainnet`, `disarm-mainnet`, `stop-mexc`, `disarm-mexc`, `stop-hyperliquid`, `disarm-hyperliquid`). |

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

### Observed production-day reconstruction

| Input / claim | Current scope and exact limitation |
| --- | --- |
| Candidate interval | 2026-09-06 00:00:00 UTC ≤ execution time < 2026-09-07 00:00:00 UTC; both realms, USDT linear only |
| Original copied WALs | `/tmp/r3-current-wal-20260907T005622Z/{demo,mainnet}/raw/engine.wal`; 53 complete segments per family; all frames and original SHA256s checked. The research reader accepts segment versions 1–7 and current identity/order/fill tags; this does not change the ordinary Rust runtime's v1/v7 reader contract |
| Private captures | `/tmp/connected-work-20260907/{demo,mainnet}-history.jsonl`; authenticated paginated executions, closed P&L and USDT transactions for the complete interval; 38/77 trade fills and 28/29 funding executions respectively |
| Observed accounting | Every trade matches WAL execution ID, request, symbol, side, quantity, price, fee and timestamp plus its transaction quantity/price/fee. Funding execution fees match signed transaction funding; cash balances form one exact chain, including equal-millisecond rows |
| Demo cash legs | Fees `5.02740506`, funding credit `4.17601351`, cash flow `160.40318`, net change `159.55178845` USDT. Transaction-implied initial `1612.99704261`, final transaction `1772.54883106` USDT |
| Mainnet cash legs | Fees `1.56448939`, funding credit `0.32668403`, cash flow `11.80147000`, net change `10.56366464` USDT. Transaction-implied initial `127.95896334`, final transaction `138.52262798` USDT |
| Missing boundary account fields | No independent account snapshot with balances and per-symbol positions exactly at either midnight is identified in this bundle. `reconciled` WAL rows contain findings/may-open, not account quantities or balances. The retained venue snapshot is at the later 00:56–00:57 capture; transaction endpoints are inferred cash, not independent boundary observations |
| Ownership/order lifecycle | Request-attributed signed fill deltas are available. They do not reconstruct all internal offsets, forced-fill allocations, initial holdings, cancellations/amendments and final sleeve positions. Full lifecycle equality remains unestablished |
| Order-history snapshots | `/tmp/connected-work-20260907/history-orders-{demo,mainnet}-20260906.json` contains 112/53 orders created during the day, captured at 21:12:30/33 UTC Sep 7. All 109/49 engine requests match cumulative fill quantity and USDT fee; 108/49 also match original request terms and terminal state. Seven remaining rows are venue-created deactivated stops without client IDs. Pagination is complete; chronology is not. [Bybit documents](https://bybit-exchange.github.io/docs/v5/order/order-list) only 24-hour retention for fully cancelled/rejected/deactivated orders; this capture returns some older rows, which does not establish exhaustive older coverage |
| Preserved lifecycle difference | Under historical `cece1d9f`, demo `eng-1788648652000-47` requests a 61,960-unit XCNUSDT reduce-only buy. The venue snapshot reports adjusted quantity 30,980 and `Filled`; the WAL records exactly 30,980 filled, fee `0.07081409`, then cancels the original remainder. These terminal labels refer to different quantities. `/tmp/connected-work-20260907/{demo,mainnet}-order-history-comparison.json` retains the difference; no full lifecycle equality is claimed |
| Signals | The WAL contains 19,962 demo / 21,483 mainnet accepted and consumed day observations, seven/six lifecycle records, and one mainnet signal gap. The current disk spools contain readiness files and a socket. No claim that signals are absent; managed lifecycle timeline replay remains unsupported by `SignalReplayFeed` |
| Public archives | All 24 named Bybit hourly tar objects exist under `LiquidityMigration/market-tape/bybit-linear/2026/09/06/`. Object presence does not establish complete symbol/channel delivery, instrument snapshots or gap-free book chains; their full payloads are not qualified against every traded interval |
| Runtime/config timeline | Five demo / four mainnet boots span multiple commits. Copied realm configs and WAL hashes exist; replaying all decisions as a single current binary is not the historical production program. Exact clock/state/version transition replay remains unqualified |
| Result files | `/tmp/connected-work-20260907/{demo,mainnet}-observed-day.json`, `*-wal-scan.json`, `*-wal-input-inventory.json`, `observed-day-summary-final.log`; originals and prior failed diagnostics remain intact |
| Full-day outcome | No complete independent production reproduction is established. Observed cash/fill matching does not validate hypothetical public-data fills or a strategy's profitability |

The existing reconciliation report includes `observed_window`. Its `complete_production_reproduction` remains false and its missing requirements remain explicit. Missing cash fields, discontinuities, funding mismatches and unmatched executions are reported individually; missing values never become zero. `accounting_only=True` streams/CRC-checks every frame while retaining only accounting records and original sequence numbers.

### Fleet Manifest & Systemd Unit Inventory

| Systemd Unit | Realm | User / Group | Activation Policy | Role |
| :--- | :--- | :--- | :--- | :--- |
| `liquidity-migration-engine.service` | Demo | `liquidity-engine-demo:liquidity-migration` | `multi-user.target` | Execution engine on demo account. |
| `liquidity-migration-engine-mainnet.service` | Mainnet | `liquidity-engine-mainnet:liquidity-migration`| `manual` (requires `REAL_MONEY`) | Execution engine on funded Bybit account. |
| `liquidity-migration-engine-mexc.service` | MEXC | `liquidity-engine-mexc:liquidity-migration` | `manual` (requires `REAL_MONEY` in `mexc-mainnet.env`) | Execution engine on the MEXC USDT-perp account. |
| `liquidity-migration-engine-hyperliquid.service` | Hyperliquid | `liquidity-engine-hyperliquid:liquidity-migration` | `manual` (requires `REAL_MONEY` in `hyperliquid-mainnet.env`) | Execution engine on the funded Hyperliquid account. |
| `liquidity-migration-signal-worker-demo.service` | Demo | `liquidity-signal-worker:liquidity-migration`| `multi-user.target` | Public feature ingestion & IPC. |
| `liquidity-migration-signal-worker-mainnet.service`| Mainnet | `liquidity-signal-worker:liquidity-migration`| `multi-user.target` | Public feature ingestion & IPC. |
| `liquidity-migration-signal-worker-mexc.service` | MEXC | `liquidity-signal-worker:liquidity-migration`| `manual` (with its realm) | Public feature ingestion & IPC; the features are Bybit mainnet's. |
| `liquidity-migration-signal-worker-hyperliquid.service` | Hyperliquid | `liquidity-signal-worker:liquidity-migration`| `manual` (with its realm) | Public feature ingestion & IPC; the features are Bybit mainnet's. |
| `liquidity-migration-mexc-liveness.timer` | MEXC | `liquidity-observer:liquidity-migration` | Timer (every 30 s while armed) | MEXC engine and worker watchdog. |
| `liquidity-migration-hyperliquid-liveness.timer` | Hyperliquid | `liquidity-observer:liquidity-migration` | Timer (every 30 s while armed) | Hyperliquid engine and worker watchdog. |
| `liquidity-migration-forward-capture.service` | Global | `liquidity-capture:liquidity-migration` | `independent` (boot) | Continuous Bybit tick & L2 capture. |
| `liquidity-migration-forward-capture-binance.service`| Global | `liquidity-capture:liquidity-migration` | `independent` (boot) | Continuous Binance tick & L2 capture. |
| `liquidity-migration-telegram-controls.service` | Global | `liquidity-controls:liquidity-controls` | `multi-user.target` | Interactive Telegram operator bot. |
| `liquidity-migration-trade-notify.timer` | Global | `liquidity-observer:liquidity-migration` | Timer (every 1m) | Fills and closed-trade alert dispatcher. |
| `liquidity-migration-market-tape-upload.timer` | Global | `root:root` | Timer (hourly at :10) | Ships finished tape archives to Google Drive, then deletes shipped hours older than `--keep-hours 24` from both tape roots. |
| `liquidity-migration-backup.timer` | Global | `root:root` | Timer (every 15 min) | Ships engine state & WAL to Google Drive. |

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
| Dispatch `diagnose`, `disarm-mainnet`, `disarm-mexc`, `disarm-hyperliquid` | No build | Reads incident state or persistently disarms one funded realm |

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
| Readiness | Require a fresh heartbeat and the same active main PID/restart counter throughout the 12-second settle window before recording the realm fingerprint. An enforced rolling-loss entry restriction remains active and is reported as a `NOTICE`; it does not block process replacement or the five-minute demo soak. Other heartbeat and resource failures still block handover. |
| Failed handover or fleet rollback | A predecessor must have identical Rust, dependency, toolchain and build inputs to the current checkout and recorded deployed generation. Incompatible or unavailable inputs leave the installed candidate and durable state in place for forward repair. The worker has no read-only state compatibility command, so a changed-runtime rollback is not inferred safe. |
| Default demo rollback/drill | `scripts/runtime/chaos_drill.sh rollback\|drill` selects the recorded previous generation under the deployment lock and requires identical runtime inputs. `restore` selects the completed current generation without requiring a predecessor |
| Selected demo pair | `rollback\|drill --qualified-pair EXPECTED_CURRENT_SHA PREDECESSOR_SHA` selects a retained archive independently of `previous-commit`. The caller must qualify the specific runtime/state compatibility before using this input. The helper requires the completed deployment to equal the supplied current SHA under the lock; a failed predecessor startup restores that current release. `restore` rejects pair arguments |
| Pair qualification scope | Full-SHA pair selection permits its reviewed source difference; it does not suppress archive, WAL or worker-state errors. A copied-WAL read establishes record readability only. Loaded images, fresh process/account readiness and successful restoration require an actual demo drill |
| Durable state | Rollback never restores old WAL or worker files over newer state; required record refusal remains explicit. |
| Legacy Python snapshots | Deployment refuses missing or invalid canonical native state when the WAL is nonempty or any required legacy source file exists. Recovery uses a retained compatible release with the complete source bundle and matching account/configuration; the current release has no Python state importer. |
| Retained WAL recovery | Ordinary readers accept segment v1/v7; `wal-convert-v5` privately reads v5 into a separate complete family. Original families remain recoverable through the compatible `8c92c964` release pinned in [STATE.md](../STATE.md). Preserve original source segments, unresolved callback sources and compatible binaries until their replay/rollback requirement is retired. Copied conversion does not authorize host conversion or pruning. [Retained input assembly and qualification](https://github.com/rob435/liquidity-migration/blob/29366d3a2013701a0956a2a471a7c916bf6980e2/docs/tier1-round-handoff.md#L80-L86) records the exact prefix/tail boundaries; a quarantined suffix alone is not a complete family. |

### Retained legacy dependencies

| Call path | Why it remains required |
| --- | --- |
| `engine/engine-core/src/engine/order_lineage.rs` → WAL archive reader → `OrderLineageRestored` | Late fills reactivate original scalar requests and cumulative fill lineage from retained segments |
| `engine/engine-core/src/engine/boot_recovery.rs` → `legacy_quantity::plan` / `Replay` → attribution/reconciliation | Actual retained inventory still needs provenance-driven grid adoption; native exact residuals cannot use legacy dust rules |
| `callback_recovery/host.rs`, `state.rs`, `snapshot.rs` → WAL queued/prepared pages | Converted fixtures still retrieve four demo / five mainnet queued and prepared callbacks; zero source-frontier fixture coverage does not authorize source-reader deletion |
| Native stop maintenance / legacy stop intent | A held position lacking fresh catalog metadata still needs its durable same-side protective-stop repair |
| `backtest/venue.rs` book mode and `sim/` | Existing binary64 fill compatibility remains tested; new trade/bar modes use derived exact grid quantities with an explicitly supplied USDT settlement asset |
| `wal-convert-v5` and retained `8c92c964` release | Quarantined v5 originals remain recoverable only through the retained compatible reader/converter. Current ordinary readers still refuse them |
| Three terminal demo requests | `eng-1788685989000-{8,9,10}` lack exact terms; both wall time and complete execution history must be strictly beyond 2026-09-13 13:47:40.615 UTC. Calendar passage cannot remove other retained-state dependencies |
| Current retained-state read | At 2026-09-07 21:04:59 UTC, live demo segment `000063` still contains all three cancelled, zero-filled, scalar requests. Its execution-history frontier is `1788801307401` ms; neither strict expiry condition is met. `/tmp/connected-work-20260907/live-retention-boundary.json` retains the read |
| Current-source copied boots | All 14 demo / 13 mainnet converted v7 bases boot; original v5 bases remain refused. Nine queued/nine prepared callbacks and both known filled archive queries remain readable. Full-prefix/rotated reboot preserves exact ownership, quantities, accounting, lots and protection for six captured native positions per realm; the separate legacy-quantity original/cleared rehearsal also passes. Transport, collateral and fresh quote receipt times are mocked; real callback-source coverage is zero |

Copied-family requalification verifies all 27 base transformations, every frame CRC, original hashes and unchanged non-base bytes. The current-source boot checks add no independent venue/account truth or new compatible-image paired comparison. No live WAL conversion, pruning, release removal, capital change or mainnet arming change is part of this research work. Such a live migration still needs explicit owner authorization after the retained dependencies are resolved.

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
Persistently stops one funded realm and disables its arming switch:
```bash
scripts/ops.sh deploy disarm-mainnet
scripts/ops.sh deploy disarm-mexc
scripts/ops.sh deploy disarm-hyperliquid
```

| Mode | Sets `REAL_MONEY=false` in | Stops and disables |
| :--- | :--- | :--- |
| `disarm-mainnet` | `/etc/liquidity-migration/bybit-mainnet.env` | every `mainnet` realm unit |
| `disarm-mexc` | `/etc/liquidity-migration/mexc-mainnet.env` | every `mexc` realm unit |
| `disarm-hyperliquid` | `/etc/liquidity-migration/hyperliquid-mainnet.env` | every `hyperliquid` realm unit |

`stop-mainnet`, `stop-mexc` and `stop-hyperliquid` stop the same units without
touching the switch. No mode flattens exposure.

---

### Real-Money Configuration Dials

Configured in `/etc/liquidity-migration/bybit-mainnet.env` (`0600`, root-owned):

| Environment Dial | Default | Constraint | Meaning |
| :--- | :--- | :--- | :--- |
| `REAL_MONEY` | `false` | Required `true` | Master arming switch for the funded engine. |
| `RM_CARRY_STOP_LOSS_FRACTION` | `0.10` | `0 < fraction < 1/5` | Declared CARRY stop ceiling; the engine may tighten it for leverage or known liquidation price. |
| `RM_ROLLING_LOSS_FRACTION` | `0.10` | Positive ratio | Maximum fraction of reference lost in closed 24h PnL plus current account open losses before entry admission trips. |

These dials serve every realm: deploy renders one operational profile from this
file and installs the identical bytes in each realm's signal-worker source
directory. `mexc-mainnet.env` and `hyperliquid-mainnet.env` hold no dials and a
dial written in either is read by nothing.

---

### MEXC Realm

| Property | Value |
| :--- | :--- |
| Fleet realm | `mexc` |
| Engine venue name | `mexc_mainnet` (`venue` in `deploy/engine.mexc.toml.template`) |
| Heartbeat realm / lease realm | `mexc_mainnet`; heartbeat venue is `mexc` |
| Practice realm | none — MEXC publishes no futures testnet, so every order is real |
| Credential file | `/etc/liquidity-migration/mexc-mainnet.env`, root-owned `0600`, written by hand |
| Unit environment | `/etc/liquidity-migration/engine-mexc.env`, root-owned `0600`, written by hand |
| Rendered config | `/etc/liquidity-migration/engine-mexc.toml`, rendered by deploy |
| Sleeves | LONG entries on; CARRY and EXODUS entries rendered off. CARRY scores Bybit funding, MEXC funding differs per symbol. No maker, no probe |
| Public data | Bybit mainnet, exactly as the other realms (`configs/signal-worker.mexc.json`, `public_market_realm` `mainnet`) |
| Source readiness | `mexc_mainnet` is `live-proven` (canary lifecycle 2026-09-08 20:16 UTC): `engine run` takes the realm, `engine canary-order` refuses it. `engine venues` prints the current value |

**Must** obtain `EXPECTED_ENGINE_ACCOUNT_USER_ID` from an authenticated venue
reply; MEXC exposes no numeric account id, and the engine derives `key-` plus
the first eight bytes of `sha256(api key)` in hex.
**Must** know that `REAL_MONEY=true` in `mexc-mainnet.env` starts the realm on
the next deploy: deploy reads `engine venues` from the installed binary, and a
`live-proven` realm with an armed switch is provisioned and handed over like
mainnet. `verify` prints the value as `mexc readiness=...`.
**Must Never** set the switch without explicit owner instruction.

Arming, in order:

```sh
# 1. On MEXC: create a key with futures order placement (KYC-gated), no
#    withdrawal, IP-allowlisted to this VPS.
# 2. On the host, by hand:
install -o root -g root -m 0600 deploy/mexc-mainnet.env.template \
  /etc/liquidity-migration/mexc-mainnet.env
install -o root -g root -m 0600 deploy/engine.mexc.env.template \
  /etc/liquidity-migration/engine-mexc.env
# fill in MEXC_REAL_API_KEY and MEXC_REAL_API_SECRET; leave REAL_MONEY=false
# until the identity is bound.

# 3. Deploy once with the switch off: deploy installs the units and leaves
#    them stopped. Then read the account id the gateway binds: the template's
#    placeholder `key-` mismatches on purpose and the message prints the id
#    the credentials answered as. Write it into engine-mexc.env and rerun
#    until it passes, then prove the account clean.
gh workflow run vps-deploy.yml --ref main -f mode=deploy
scripts/ops.sh verify-account-identity --environment mexc
scripts/ops.sh attest-flat --environment mexc

# 4. Arm REAL_MONEY=true in mexc-mainnet.env, then deploy; that deploy
#    renders engine-mexc.toml, projects the worker env and starts the realm.
scripts/ops.sh real-money preflight-mexc
gh workflow run vps-deploy.yml --ref main -f mode=deploy
```

`verify-account-identity`, `attest-flat` and `canary-order` run on the host
under `systemd-run` as the realm's user with its credential file loaded; the
read-only modes drop `REAL_MONEY`, the canary keeps it.

---

### Hyperliquid Realm

| Property | Value |
| :--- | :--- |
| Fleet realm | `hyperliquid` |
| Engine venue name | `hyperliquid_mainnet` (`venue` in `deploy/engine.hyperliquid.toml.template`) |
| Heartbeat realm / lease realm | `hyperliquid_mainnet`; heartbeat venue is `hyperliquid` |
| Lease file | `/run/lock/liquidity-migration/hyperliquid-hyperliquid_mainnet-user-0x<40 hex>.lock` |
| Practice realm | `hyperliquid_testnet` exists on the venue at `testnet-canary` and is not a fleet realm. Every order this realm can place is real |
| Credential file | `/etc/liquidity-migration/hyperliquid-mainnet.env`, root-owned `0600`, written by hand, and the only arming file |
| Credential keys | `HYPERLIQUID_REAL_ACCOUNT_ADDRESS` (the master account, `0x` + 40 hex in either case; the engine lower-cases it), `HYPERLIQUID_REAL_API_WALLET_KEY` (an API wallet — the venue calls it an agent — the account approved, `0x` + 64 hex; it trades and cannot withdraw), `REAL_MONEY`, optional Telegram trio |
| Unit environment | `/etc/liquidity-migration/engine-hyperliquid.env`, root-owned `0600`, written by hand |
| Rendered config | `/etc/liquidity-migration/engine-hyperliquid.toml`, rendered by deploy |
| Sleeves | LONG entries on; CARRY and EXODUS entries rendered off. Both score Bybit's eight-hourly funding rate and Hyperliquid funds hourly. No maker, no probe |
| Public data | Bybit mainnet, exactly as the other realms (`configs/signal-worker.hyperliquid.json`, `public_market_realm` `mainnet`). The engine prices against Hyperliquid's own `bbo` / `activeAssetCtx` socket |
| Source readiness | `hyperliquid_mainnet` is `live-canary`: `engine canary-order` runs, `engine run` refuses. `engine venues` prints the current value |

| Venue fact | Where it changes a decision |
| :--- | :--- |
| Funding settles and is quoted hourly | A carry number taken from Bybit's eight-hourly rate is out by a factor of eight, so CARRY and EXODUS entries are rendered off |
| A stop is a separate reduce-only trigger order, not a field on the position | It appears in the working-order list of every scan, and a position with no such order reads as unprotected |
| Minimum order notional 10 USD | An order under it is refused at admission (`engine/engine-core/src/engine/intent_admission.rs`), not sent |
| Limit orders only | A market intent goes to the venue as an IOC limit through the book |
| Symbols the venue does not list | Refused at admission; the worker's universe is Bybit mainnet's |
| Base fees on the funded account today | 4.5 bp taker, 1.5 bp maker (`userFees`: `userCrossRate 0.00045`, `userAddRate 0.00015`) |

**Must** set `EXPECTED_ENGINE_ACCOUNT_USER_ID` to the MASTER account address,
lower-case `0x` plus 40 hex — never the API wallet's own address.
`/info {"type": "userRole", "user": <agent address>}` names the master the
agent signs for. At boot the gateway reads the account's `extraAgents` and
refuses before trading if the signing key is not listed.
**Must** hold the account's USDC in Perps, not Spot. A spot balance and a
negative account value show up as `wallet_asset` rows: they do not block the
canary's derivative-flat precheck, but they do block `attest-flat`.
**Must** arm `REAL_MONEY=true` in `hyperliquid-mainnet.env` before the canary:
the gateway refuses to build unarmed. Deploy provisions the realm whenever the
switch is armed and starts its units only when the installed `engine venues`
reports `live-proven`; `verify` prints `hyperliquid armed|off` and
`hyperliquid readiness=...`.
**Must Never** set the switch without explicit owner instruction.

Arming, in order:

```sh
# 1. On Hyperliquid, in the venue's own interface: approve an API wallet
#    (agent) for the master account, and move the account's USDC from Spot to
#    Perps. Nothing on this host can do either.
# 2. On the host, by hand:
install -o root -g root -m 0600 deploy/hyperliquid-mainnet.env.template \
  /etc/liquidity-migration/hyperliquid-mainnet.env
install -o root -g root -m 0600 deploy/engine.hyperliquid.env.template \
  /etc/liquidity-migration/engine-hyperliquid.env
# fill in HYPERLIQUID_REAL_ACCOUNT_ADDRESS (the master),
# HYPERLIQUID_REAL_API_WALLET_KEY and REAL_MONEY=true; the realm stays stopped
# until the source is promoted, whatever this says.

# 3. Deploy once: with the switch armed, deploy renders
#    engine-hyperliquid.toml and projects the worker env, and leaves every
#    hyperliquid unit stopped while the realm is live-canary.
scripts/ops.sh real-money preflight-hyperliquid
gh workflow run vps-deploy.yml --ref main -f mode=deploy

# 4. Read the account id the gateway binds. The template's placeholder `0x`
#    mismatches on purpose; the message prints the id the credentials answered
#    as. Write it into engine-hyperliquid.env and rerun until it passes.
scripts/ops.sh verify-account-identity --environment hyperliquid

# 5. The live evidence step: one 10-USD post-only BTCUSDT order, its cancel,
#    and two clean account scans, through the realm's credential file.
scripts/ops.sh canary-order --environment hyperliquid --symbol BTCUSDT \
  --expected-user-id 0x<40 hex> --execute

# 6. Record the canary receipt in CHANGELOG.md, move hyperliquid_mainnet to
#    live-proven in engine/engine-public/src/registry.rs, push, and deploy
#    again; that deploy starts the realm.
```

Both engine subcommands run on the host under `systemd-run` as
`liquidity-engine-hyperliquid` with `hyperliquid-mainnet.env` and
`engine-hyperliquid.env` loaded; the identity check drops `REAL_MONEY`, the
canary keeps it.

---

### Off-Box Google Drive Backups

Configured via `/etc/liquidity-migration/rclone.conf`:

| Data Payload | Schedule | Destination on Google Drive | Retention |
| :--- | :--- | :--- | :--- |
| **Engine State & WAL** | Every 15 min (`backup.timer`; completed-copy age alerts after 30 min) | `LiquidityMigration/engine-state/latest/` | 60 days in `history/` |
| **Market Tape Hours** | Hourly at :10 (`upload.timer`)| `LiquidityMigration/market-tape/<tape>/YYYY/MM/DD/` | Permanent archive; the host keeps a 24 h sliding window of shipped hours ([market_tape/README.md](../market_tape/README.md) §Local Sliding Window) |

| Local backup stage | Contract |
| --- | --- |
| Sealed WAL segments | After successful remote checksum verification, byte-identical staged copies of numbered segments below the current maximum become hard links to the immutable source on the same filesystem |
| Growing WAL / other state | Remain independent copies; rsync uses replacement files, never `--inplace`; a later append cannot change the active segment's staged snapshot |
| Physical disk usage | Linking releases duplicate blocks without pruning the live WAL or cloud history; `du` on the stage alone still counts shared blocks |
| Stage on its own mount | `link` needs one mount, not one matching `st_dev`: a stage the kernel refuses a link into keeps both copies, counts `unlinkable_roots=` in the run's last line, and leaves the backup successful |
| Implementation | `scripts/runtime/backup_state.sh`, `scripts/runtime/link_sealed_backup_wals.py`; the existing backup lock covers staging, verification and linking |
| Mount namespace | The script creates `backup/` on the same mount as source WALs; systemd manages only `receipts/` through `StateDirectory`, because a separate backup bind mount prevents hard links even when device IDs match |
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

Must never delete later-generation rows, rewrite accepted hashes, or edit a live cursor to clear a gap. Inspect logs read-only before selecting a recovery action (`<realm>` is `demo`, `mainnet`, `mexc`, or `hyperliquid`):

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
