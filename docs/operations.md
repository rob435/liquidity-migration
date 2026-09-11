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
| **Why** | `scripts/ops.sh why [realm]` | Read-only | Why one engine is not trading, from its own heartbeat: HEALTH (age, `may_open`, private stream, rolling loss, strategy errors, per-strategy entry permission, uptime, commit), EXPOSURE (positions, working entries) and BLOCKERS (`entry_blockers` grouped by reason). Default realm `mainnet`. An absent field prints `unknown`, never zero. |
| **Start / Stop** | `scripts/ops.sh <start\|stop\|restart> <unit...>` | Mutating | Controls individual fleet units. |
| **Flatten** | `scripts/ops.sh flatten --environment <demo\|mainnet\|mexc\|hyperliquid> [--execute]` | Mutating | Orders reducers to close attributed exposure. Read-only without `--execute`. |
| **Attest Flat** | `scripts/ops.sh attest-flat --environment <demo\|mainnet\|mexc\|hyperliquid>` | Read-only | Two-scan venue proof that the account holds zero open positions. Positive USDT/USDC is cash in every account except `TradingBot` and `CopyTrading`; a Bybit holding the venue values under 1 USD is dust (`wallet_dust`, `asset_account_dust:*`), printed as `flat-dust` and not a blocker; MEXC and Hyperliquid balances carry no venue valuation and count in full. Bybit, MEXC and Hyperliquid implement the GET-only credential-wide probe; MEXC's scan covers the futures account (assets, positions, open orders, position stops), Hyperliquid's covers perpetual positions, the cross-margin account value, working orders including reduce-only triggers, and spot token balances. |
| **Preflight** | `scripts/ops.sh real-money preflight` | Read-only | Validates all funded Bybit credentials, IP bindings, and profile dials. |
| **MEXC preflight** | `scripts/ops.sh real-money preflight-mexc` | Read-only | Validates the MEXC credential file, its arming switch, and the mexc worker source. |
| **Hyperliquid preflight** | `scripts/ops.sh real-money preflight-hyperliquid` | Read-only | Validates the Hyperliquid credential file (address shape, API wallet key shape, no other venue's keys), its arming switch, and the hyperliquid worker source. |
| **Verify Identity** | `scripts/ops.sh verify-account-identity --environment <demo\|mainnet\|mexc\|hyperliquid>` | Read-only | Authenticates the realm's GET-only probe and binds it to `EXPECTED_ENGINE_ACCOUNT_USER_ID`; a mismatch prints the id the credentials answered as. |
| **Canary Order** | `scripts/ops.sh canary-order --environment <demo\|mexc\|hyperliquid> --symbol SYMBOL --expected-user-id ID [--execute]` | Mutating with `--execute` | One bounded live order lifecycle through the realm's own credential file: one venue-minimum post-only order 0.5% under the bid, cancelled, the account proved clean twice. The engine accepts it on the Bybit demo and on `live-canary` realms only; `mexc_mainnet` and `hyperliquid_mainnet` are both `live-canary` today, so `demo`, `mexc` and `hyperliquid` are all accepted. `CANARY_REALMS` in `scripts/ops.sh` is the practice realm plus every funded realm on a venue with no practice sibling. A running engine holds the account lease, so take a realm's canary while its posture is `stopped`. |
| **Storage** | `scripts/ops.sh storage [plan]` | Read-only | Prints the reclaimer's receipt and `status.json`. `plan` re-measures the budget and reports the candidates without pruning, uploading or unlinking. |
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

#### Full-day economic reconciliation

`liquidity_migration/research/day_reconciliation.py` reconciles one realm's whole UTC day between two equity-recorder boundary samples and the venue's own transaction log. It is read-only, runs off the host on copies, and emits `gate: pass|fail` with its reasons.

| Input | What it is | Off-host copy |
| --- | --- | --- |
| `--wal` | the copied engine WAL segments; a copied day never starts at segment 1, so what is required is coverage of both boundary readings | `/var/lib/liquidity-migration-engine[-<realm>]/engine.wal*`, the segments whose records span both readings |
| `--capture` | authenticated executions, closed P&L, USDT transaction log and both transfer directions, from the day's midnight through the end boundary sample's venue reading, so capture a few minutes past the next midnight | written by `scripts/research/capture_bybit_account_history.py` |
| `--equity-samples` | directory holding `engine-<realm>-<YYYY-MM>.jsonl` | `/var/lib/liquidity-migration/equity` |
| `--tolerance-usdt` | declared residual tolerance, default `0.01` | — |
| `--max-boundary-distance-s` | declared maximum distance between a boundary sample and midnight, default `120` | — |
| `--out` | new mode-0600 JSON report; the plain text table is written beside it with a `.txt` suffix | — |

Funding settles at 00:00 UTC and the sample lands seconds later, so the rows between midnight and each boundary sample's venue reading sit on one side of the day's sum and the other side of the equity reading. They are listed with their ids, types and amounts, the gate reads the residual net of them, and both the raw and the net residual stay in the report.

| Boundary sample field | Use |
| --- | --- |
| `ts_ms`, `account_age_ms` | selection and both distances: the sample's own clock, and the venue reading inside it at `ts_ms - account_age_ms`, which is also the straddle interval's far edge |
| `state` | only a `live` sample with a numeric `equity_usdt` is selected; earlier non-live rows are counted, never averaged over |
| `equity_usdt` | the boundary money reading; drives the equity residual |
| `wallet_cash_usdt`, `unrealised_pnl_usdt` | the cash-versus-mark split; present, they gate the recorded wallet-cash move against the venue's rows and make an open boundary position reconcilable |
| `positions` | per-symbol signed quantity, gated against the WAL's attributed exposure; a row with a null `strategy` is the owner's hand exposure and is reported, not gated |
| `positions_truncated` | the recorder dropped the list to stay inside its line cap, so per-symbol equality is unestablished at that boundary |
| `position_count`, `sleeve_positions` | the physical and per-sleeve position comparison against the WAL's attributed exposure |
| `position_entry_notional_usdt` | reported against the WAL-derived entry notional; never gated |
| `account_user_id`, `venue`, `mode`, `engine_commit` | identity, bound to the capture manifest's `user_id` |

| Gate check | Passes when | Fails with |
| --- | --- | --- |
| Boundary selection | a `live` sample exists at or after each midnight, and both its own and its venue reading's distance are inside `--max-boundary-distance-s` | a named missing requirement per boundary, never a zero |
| WAL coverage | the first copied segment's first record is stamped at or before the begin boundary reading and the last segment's last record at or after the end one | the segment index and the stamp it carries instead; a family that does not start at segment 1 is a note, not a failure |
| Capture coverage | the manifest is complete under schema 1, covers `[day, day+1)`, and names the requested realm and one account | the manifest's own issue text |
| Account transfers | the manifest proves complete `transfer_in` and `transfer_out` pagination | one requirement naming the unfetched sources with the chain's unexplained jumps and the cash residual a transfer would leave; the per-jump and residual reasons fold into it, because there is one cause |
| Transaction types | every row's `type` is one of `TRADE`, `SETTLEMENT`, `TRANSFER_IN`, `TRANSFER_OUT`, `DEPOSIT`, `WITHDRAW` | the unrecognised type, its row count and its summed `change`; the rows still count toward the total |
| Wallet-cash chain | each row's `cashBalance - change` equals the previous row's `cashBalance`, and `(end - begin) - Σ change` is inside tolerance. Rows sharing one `transactionTime` are ordered by the chain, never by `id`, which is not an order | the jump, its amount, and the transaction it precedes |
| Boundary straddle | the capture covers `[midnight, the boundary sample's venue reading)` at both boundaries | a named missing requirement per boundary, because a cash row in that interval would be unobserved |
| Equity residual | `(end equity - begin equity) - Σ change`, net of the cash rows straddling the two boundary readings, is inside tolerance **and** either boundary wallet cash is recorded or both boundaries read flat | both the raw and the net residual, or the missing unrealised-P&L requirement when a boundary is neither flat nor decomposable |
| Boundary wallet cash | with `wallet_cash_usdt` at both boundaries, its move equals `Σ change` plus the straddle inside tolerance, and each sample keeps `equity = wallet cash + unrealised P&L` | the recorded move against what the rows account for, or the sample that breaks the identity |
| Fills | WAL day fills and captured `Trade` executions are the same set, and the WAL, execution and `TRADE`-row fee sums are equal | each unmatched execution id and each differing fee sum |
| Positions | per boundary, `position_count` equals the WAL's nonzero symbols plus the sample's `unattributed` count, every sleeve's count matches, and — with a sample `positions` list — every symbol's signed quantity matches | one difference row per boundary naming the sleeve, symbol and quantities |
| Attribution | every WAL fill in the day has a sleeve | the count of fills with none |

| Not established by a pass | Why |
| --- | --- |
| an independent wallet-cash snapshot | the chain is the transaction log's own `cashBalance` column, inferred cash; the sample's `wallet_cash_usdt` is `equity - unrealised P&L` off one venue reading, not a second source |
| anything a sample predating the recorder fields cannot say | without `wallet_cash_usdt` and `unrealised_pnl_usdt` a day with an open boundary position cannot split its equity change into cash and mark, and fails; without `positions` symbol equality stays unestablished and the count comparison is what gates. The report names each absent field |
| entry prices for exposure older than the copied WAL | the rotation restatement carries signed quantities, not entry prices, so those positions are listed `unpriced` and the entry-notional comparison covers only the rest |
| short-side closed round trips | round trips are grouped long-first; a short sleeve keeps its fills and its signed exposure |
| profitability or authorization | the report grants no trading or real-money authority |

```bash
DAY=2026-09-08 NEXT=2026-09-09

# 1. On the host, copy the day's evidence out; never move the originals. Copy both
#    month sample files when the day is the last of its month.
sudo sh -c "cd /var/lib/liquidity-migration-engine-mainnet && tar -czf /tmp/wal-$DAY.tgz engine.wal*"
sudo cp /var/lib/liquidity-migration/equity/engine-mainnet-2026-09.jsonl /tmp/
sudo chown "$USER" /tmp/wal-$DAY.tgz /tmp/engine-mainnet-2026-09.jsonl
# then scp both to the research box.

# 2. On the research box, unpack the segments and keep the sample file's exact name.
#    --wal below names the family prefix, which is the path even when only
#    rotation segments (engine.wal.NNNNNN) were copied.
mkdir -p /tmp/day-$DAY/wal /tmp/day-$DAY/equity
tar -C /tmp/day-$DAY/wal -xzf wal-$DAY.tgz
cp engine-mainnet-2026-09.jsonl /tmp/day-$DAY/equity/

# 3. Capture the venue's own account history: the day, plus the minutes past the
#    next midnight that reach the end boundary sample's venue reading.
python scripts/research/capture_bybit_account_history.py \
  --realm mainnet --start "$DAY" --end "${NEXT}T00:05:00+00:00" --out /tmp/day-$DAY/history.jsonl

# 4. Reconcile. Exit 0 is the gate passing; the table prints and is written beside the JSON.
python -m liquidity_migration.research.day_reconciliation \
  --realm mainnet --day "$DAY" \
  --wal /tmp/day-$DAY/wal/engine.wal \
  --capture /tmp/day-$DAY/history.jsonl \
  --equity-samples /tmp/day-$DAY/equity \
  --tolerance-usdt 0.01 \
  --out /tmp/day-$DAY/report.json
```

### Realm table

Every realm the fleet runs is declared in [`deploy/realms.tsv`](../deploy/realms.tsv), and every unit file, env template and manifest row for it is rendered from that one row.

| Column | Meaning | Allowed values |
| :--- | :--- | :--- |
| `realm` | The realm's name; every derived path and unit name is spelled from it | lowercase name, unique |
| `venue` | The credential family and the prose the venue owns | a key of `VENUE_FACTS` in `liquidity_migration/policy/realms.py` |
| `engine_venue` | The name `engine venues` prints, and what `realm_run_ready` gates on | e.g. `bybit_demo`, `mexc_mainnet` |
| `engine_realm` | `EXPECTED_ENGINE_REALM` in the realm's engine env file | e.g. `demo`, `mainnet`, `mexc_mainnet` |
| `kind` | Practice account or the owner's money | `practice` (exactly one row) \| `funded` |
| `posture` | Whether a deploy starts the realm's units or stops and disables them | `running` \| `stopped` |
| `legacy_names` | The engine's unit, env, config, state directory and env template carry no `-<realm>` suffix | `true` (practice only) \| `false` |
| `long_entries`, `carry_entries`, `exodus_entries` | What `render-native-config` permits per sleeve | `true` \| `false` \| `toggles` (defers to `LONG_SLEEVE`/`CARRY_SLEEVE`) |
| `owner_stop`, `worker_stop`, `liveness_timer_stop`, `liveness_service_stop` | The realm's four manifest stop orders; irregular because two hand-written realm extras sit between the clusters | positive integers, unique within their lifecycle phase |

Everything else is derived once, in `liquidity_migration/policy/realms.py`, and rendered to [`deploy/realm_fields.tsv`](../deploy/realm_fields.tsv) (`realm|field|value`, one row per realm per field). `lm_realm_field` in [`deploy/lib_realms.sh`](../deploy/lib_realms.sh) looks a value up in that file and derives nothing:

| Realm | Engine unit | Worker unit | Engine user | State directory | Engine env | Engine config | Credential file |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `demo` | `liquidity-migration-engine.service` | `liquidity-migration-signal-worker-demo.service` | `liquidity-engine-demo` | `/var/lib/liquidity-migration-engine` | `/etc/liquidity-migration/engine.env` | `/etc/liquidity-migration/engine.toml` | `/etc/liquidity-migration/bybit-demo.env` |
| `mainnet` | `liquidity-migration-engine-mainnet.service` | `liquidity-migration-signal-worker-mainnet.service` | `liquidity-engine-mainnet` | `/var/lib/liquidity-migration-engine-mainnet` | `/etc/liquidity-migration/engine-mainnet.env` | `/etc/liquidity-migration/engine-mainnet.toml` | `/etc/liquidity-migration/bybit-mainnet.env` |
| `mexc` | `liquidity-migration-engine-mexc.service` | `liquidity-migration-signal-worker-mexc.service` | `liquidity-engine-mexc` | `/var/lib/liquidity-migration-engine-mexc` | `/etc/liquidity-migration/engine-mexc.env` | `/etc/liquidity-migration/engine-mexc.toml` | `/etc/liquidity-migration/mexc-mainnet.env` |
| `hyperliquid` | `liquidity-migration-engine-hyperliquid.service` | `liquidity-migration-signal-worker-hyperliquid.service` | `liquidity-engine-hyperliquid` | `/var/lib/liquidity-migration-engine-hyperliquid` | `/etc/liquidity-migration/engine-hyperliquid.env` | `/etc/liquidity-migration/engine-hyperliquid.toml` | `/etc/liquidity-migration/hyperliquid-mainnet.env` |

Also derived per realm: the liveness service and timer, the signal spool `/var/lib/liquidity-migration/signals/<realm>`, the control spool `/var/lib/liquidity-migration/controls/<realm>`, the worker source env and its operational profile, the Telegram route `/etc/liquidity-migration/telegram-<realm>.env`, the three data roots `/opt/liquidity-migration/data/bybit-{long,carry,exodus}-<realm>-event`, the `UnsetEnvironment` list of every unit, and the arming verb (`preflight`, else `preflight-<realm>`).

| Generated file | Count |
| :--- | :--- |
| `deploy/systemd/` engine, signal-worker, liveness service and liveness timer | 4 per realm |
| `deploy/engine[.<realm>].env.template`, `deploy/signal-worker-<realm>.env.template` | 2 per realm |
| `deploy/fleet_manifest.tsv` rows between `# BEGIN GENERATED …`/`# END GENERATED …` markers | 4 per realm, in 4 regions |
| `deploy/realm_fields.tsv`, the shell's only source of realm facts | 60 rows per realm, one file |

**Invariants**

- Must never hand-edit a generated file; edit the table or the renderer and re-render.
- `python -m liquidity_migration.policy.realms check` must exit 0; `tests/policy/test_realms.py` fails the gate otherwise.
- Must keep the hand-written manifest rows (shared units, the mainnet `execution-study` pair, the demo `chaos-drill` pair) outside every generated region.
- `lm_realm_field` reads only the generated `deploy/realm_fields.tsv`, so a realm or a field reaches the shell only after `python -m liquidity_migration.policy.realms render`; the parity test compares the lookup with `realms.py` for every field of every realm.
- The practice realm must be `posture=running`: the deploy soaks on it before any funded handover.
- A new realm on a known venue is one table row, plus its `configs/signal-worker-<realm>.json`, its `deploy/engine.<realm>.toml.template`, and the owner's credential file on the host. A new **venue** also needs its credential families and labels in `VENUE_FACTS` (`realms.py`), nowhere else.
- Must never read `posture` as authorization: `REAL_MONEY=true` in the realm's own credential file is still the only arming gate. The engine's own `readiness` refuses `production-blocked` and `read-only`; a `live-canary` realm runs as the owner's forward test and its boot log names the unproven capabilities.

**Recipes**

```bash
# Re-render every generated file from the table, then prove no drift.
python -m liquidity_migration.policy.realms render
python -m liquidity_migration.policy.realms check

# Read one realm's derived names, from bash or from Python.
bash -c '. deploy/lib_realms.sh; lm_realm_field mexc engine_config'
python -c 'from liquidity_migration.policy.realms import realm; print(realm("mexc").engine_config)'

# Stop a funded realm permanently: set its posture, then deploy. The next
# deploy stops and disables its units and leaves them stopped.
#   deploy/realms.tsv: mexc|...|funded|stopped|...
EXPECTED_COMMIT=<40-hex-commit> scripts/ops.sh deploy
```

---

### Fleet Manifest & Systemd Unit Inventory

| Systemd Unit | Realm | User / Group | Activation Policy | Role |
| :--- | :--- | :--- | :--- | :--- |
| `liquidity-migration-engine.service` | Demo | `liquidity-engine-demo:liquidity-migration` | `multi-user.target` | Execution engine on demo account. |
| `liquidity-migration-engine-mainnet.service` | Mainnet | `liquidity-engine-mainnet:liquidity-migration`| `manual` (requires `REAL_MONEY`) | Execution engine on funded Bybit account. |
| `liquidity-migration-engine-mexc.service` | MEXC | `liquidity-engine-mexc:liquidity-migration` | `manual` (requires `REAL_MONEY` in `mexc-mainnet.env`) | Execution engine on the MEXC USDT-perp account. |
| `liquidity-migration-engine-hyperliquid.service` | Hyperliquid | `liquidity-engine-hyperliquid:liquidity-migration` | `manual` (requires `REAL_MONEY` in `hyperliquid-mainnet.env`) | Execution engine on the funded Hyperliquid account. |
| `liquidity-migration-signal-worker-demo.service` | Demo | `liquidity-signal-worker:liquidity-migration`| `multi-user.target` | Public feature ingestion & IPC. |
| `liquidity-migration-signal-worker-mainnet.service`| Mainnet | `liquidity-signal-worker:liquidity-migration`| `multi-user.target` | Public feature ingestion & IPC. |
| `liquidity-migration-signal-worker-mexc.service` | MEXC | `liquidity-signal-worker:liquidity-migration`| `manual` (with its realm) | Public feature ingestion & IPC; the features are built from MEXC's own public data. |
| `liquidity-migration-signal-worker-hyperliquid.service` | Hyperliquid | `liquidity-signal-worker:liquidity-migration`| `manual` (with its realm) | Public feature ingestion & IPC; the features are built from Hyperliquid's own public data. |
| `liquidity-migration-mexc-liveness.timer` | MEXC | `liquidity-observer:liquidity-migration` | Timer (every 30 s while armed) | MEXC engine and worker watchdog. |
| `liquidity-migration-hyperliquid-liveness.timer` | Hyperliquid | `liquidity-observer:liquidity-migration` | Timer (every 30 s while armed) | Hyperliquid engine and worker watchdog. |
| `liquidity-migration-forward-capture.service` | Global | `liquidity-capture:liquidity-migration` | `independent` (boot) | Continuous Bybit tick & L2 capture. |
| `liquidity-migration-forward-capture-binance.service`| Global | `liquidity-capture:liquidity-migration` | `independent` (boot) | Continuous Binance tick & L2 capture. |
| `liquidity-migration-telegram-controls.service` | Global | `liquidity-controls:liquidity-controls` | `multi-user.target` | Interactive Telegram operator bot. |
| `liquidity-migration-trade-notify.timer` | Global | `liquidity-observer:liquidity-migration` | Timer (every 1m) | Fills and closed-trade alert dispatcher. |
| `liquidity-migration-market-tape-upload.timer` | Global | `root:root` | Timer (hourly at :10) | Ships finished tape archives to Google Drive, then deletes shipped hours older than `--keep-hours 6` from both tape roots. |
| `liquidity-migration-backup.timer` | Global | `root:root` | Timer (every 15 min) | Ships engine state & WAL to Google Drive. |
| `liquidity-migration-storage-reclaim.timer` | Global | `root:root` | Timer (hourly at :41) | Reclaims host storage that has been verified off-box: release directories, the apt cache, the quarantine archive, and sealed WAL segments below the engine's retention floor. |

The realm rows above are generated from [the realm table](#realm-table); the shared rows are hand-written.

| Independent families | Deploy behavior |
| --- | --- |
| `forward-capture`, `forward-capture-binance`, `market-tape-upload`, `backup`, `storage-reclaim`, `equity-recorder`, `host-liveness` | Remain running through realm handover and disarm; a recorder restarts when its own unit, configuration or runtime inputs change |

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
| Dispatch `deploy` | Python gate, Rust debug gate, candidate qualification (`release_artifact.py smoke`: a release-profile recovery and functional smoke run on the exact candidate binaries, with `qualification.json`, `qualification.log` and `binaries.sha256` packed into the archive), VPS deploy | Installs the exact `main` SHA after every gate succeeds. Every install path verifies the archive with `release_artifact.py verify --require-candidate`, so a binary qualified from the same source but compiled or featured differently cannot reuse another archive's receipt |
| Dispatch `qualify` | Rust debug gate, release tests, soak, benchmark, paired latency study | None; on-demand latency research qualification only |
| Dispatch `verify`, `rollback` | No build | Reads or restores production through the pinned VPS job |
| Dispatch `diagnose`, `disarm-mainnet`, `disarm-mexc`, `disarm-hyperliquid` | No build | Reads incident state or persistently disarms one funded realm |

#### Candidate qualification receipt

`scripts/release_artifact.py` is the only producer and the only reader of a
deployable archive.

| Property | Value |
| :--- | :--- |
| Produced by | `python3 scripts/release_artifact.py smoke --commit <sha> --output <tarball>`, in the `rust-artifact` job of `.github/workflows/vps-deploy.yml` |
| Workload (`checks`) | `release-recovery-tests`, `account-state-smoke`, `candidate-engine-smoke`, `binary-smoke`: release-profile `cargo test --release --locked --lib --tests` over `engine-core`, `engine-risk`, `engine-wal`, `engine-types`, `engine-public`, `engine-venue`; `account_state_soak --operations 100000 --live-ids 1024 --history-rows 0,1000 --repeats 1`; the packaged `engine bench --events 200 --rate 100 --every 20 --symbols BTCUSDT` |
| Receipts in the archive | `qualification.json`, `qualification.log`, `binaries.sha256` beside `engine`, `engine-tools`, `signal-worker` |
| Manifest | `schema_version` 2, `qualification_kind` (`candidate-smoke` or `full`), `commit`, `profile`, `rustc`, `target`, `platform`, `checks`, `binaries`, `log_sha256`, `wal_compatibility: "not_assessed"` |
| `build_contract` | `cargo_lock_sha256`, `toolchain_sha256`, `feature_policy: "workspace-default-features-from-pinned-source"`, `target`, `profile: "release"`, `compiler_flag_overrides: false`, exact `build_arguments`. Qualification refuses to start under `RUSTFLAGS`, `CARGO_ENCODED_RUSTFLAGS`, `RUSTC_WRAPPER`, `RUSTC_WORKSPACE_WRAPPER` or any set `CARGO_PROFILE_RELEASE_*`, and re-derives and compares the contract after the workload |
| Verified by | `release_artifact.py verify --require-candidate` in the `vps` job, `stage_release_binaries` in `scripts/deploy_vps_live.sh`, and `release_artifact unpack --require-candidate` in `build_engine` of `scripts/vps/deploy_remote.sh` |
| Refused with `--require-candidate` | No `qualification.json`; `schema_version` 1; a missing or malformed `build_contract`; a lockfile, toolchain, target, profile, feature-policy or build-argument difference; incomplete `checks` for the stated `qualification_kind` |
| Still accepted without the flag | `schema_version` 1 and the pre-qualification checksum-only archive, so the `8c92c964` rollback path pinned in [STATE.md](../STATE.md) stays usable |
| Scope | Functional recovery and smoke evidence on the exact candidate bytes. Not latency, profitability, installed-byte or WAL-generation compatibility evidence; `mode=qualify` remains the separate on-demand latency research qualification |

- **Must** keep account state, credentials and private operational evidence outside the public repository.
- **Must** run `scripts/dev.sh check` before a direct push to `main`.
- **Must** use `deploy` only for a release candidate; ordinary commits do not
  create deployments.
- **Must Never** install a release artifact without its candidate qualification
  receipt: every deploy path verifies with `--require-candidate`, and a
  source-qualified binary compiled or featured differently is a different
  candidate.
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
| Current retained-state read | Retained segments contain legacy cancelled scalar requests until both wall time and complete execution history pass the strict expiry condition. |
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

### 4. Execution host loss and replacement

Bring a replacement host onto a funded account from the off-box backup, without
ever having two writers on it.

| Objective | Value | Why |
| :--- | :--- | :--- |
| RPO | 15 min | `backup.timer` copies engine state and WAL every 15 minutes. Up to one interval of appends is missing from the restored family, so a restored engine may not know about an order it placed in that window. That is why venue reconciliation is mandatory and not a formality |
| RTO | 60 min, **proposed** | Manual: provision, restore, fence, read-only checks, arm. `gh workflow run vps-deploy.yml` installs the release; nothing restores state or fences a venue. Never measured — the drill below has not been run. The owner sets the real number |

| Recovery set | Path | Source | Compatibility check |
| :--- | :--- | :--- | :--- |
| Release binaries | `/opt/liquidity-migration-engine/releases/<commit>/` and `bin/` | The deploy workflow, from the exact source commit | SHA256 against the release-image table in [STATE.md](../STATE.md); `scripts/ops.sh deploy verify` |
| Unit environment | `/etc/liquidity-migration/engine-<realm>.env` | Written by hand, per §Realm table | `EXPECTED_ENGINE_ACCOUNT_USER_ID` is re-proved by `verify-account-identity` |
| Rendered engine config | `/etc/liquidity-migration/engine-<realm>.toml` | Rendered by deploy from the realm template | `engine render-native-config --check` fails unless the file holds the exact rendered bytes |
| Resolved sleeves | `/etc/liquidity-migration/sleeves.resolved.env` | Written by deploy (`deploy/lib_sleeves.sh`) | Re-rendered by the next deploy; a stale copy is replaced, never merged |
| Account binding registry | `/etc/liquidity-migration/mexc-account-bindings.json` (MEXC only) | Written by hand | `AccountBinding::load` refuses a credential the registry does not name, before any socket |
| WAL family | `/var/lib/liquidity-migration-engine-<realm>/engine.wal[.NNNNNN]` | `engine-state/latest/` on Drive, 60 days of `history/` | `engine-tools restore-check --wal PATH` |
| Signal spool | `/var/lib/liquidity-migration/signals/<realm>` | `engine-state/latest/` — in `DEFAULT_SOURCES` of `scripts/runtime/backup_state.sh` | `restore-check --spool DIR` counts deliverable rows and the newest sequence |
| Control spool | `/var/lib/liquidity-migration/controls/<realm>` | `engine-state/latest/` — also in `DEFAULT_SOURCES` | `restore-check --controls DIR` counts files |
| Worker state | `/var/lib/liquidity-migration-signal-worker-<realm>` | `engine-state/latest/` | The worker cold-starts when its source contract differs; nothing here checks it |
| Credentials | `/etc/liquidity-migration/*.env` | **Never backed up.** `backup_state.sh` refuses a `*.env` source by name and excludes the pattern from the copy | Re-issued at the venue as part of fencing, then written by hand |

**Every `*.env` is absent from the backup by design.** A replacement host has no
credentials until the owner writes them, which is also what makes credential
rotation a usable fence.

| Property | Implemented behavior |
| :--- | :--- |
| Lease file | `/run/lock/liquidity-migration/<venue>-<realm>-user-<account id>.lock`, e.g. `bybit-mainnet-user-<id>.lock` (`LEASE_DIRECTORY` in `engine-venue/src/lease.rs`) |
| Mechanism | Kernel `flock(LOCK_EX \| LOCK_NB)` on that inode, plus an inode re-proof after the open. Every adapter uses the same directory, name format and sequence |
| Expiry | None. No heartbeat, no timeout: the kernel drops the lock when the holder's last descriptor closes, on clean exit and crash alike |
| Scope | Process ownership **on one host**. `/run` is that host's own tmpfs, so a second host holding the same credentials contends for nothing. The lease is not failover fencing and must not be read as any |
| Venue-side dead-man | None in use. No funded realm arms a venue cancel-all or cancel-on-disconnect. Hyperliquid's `scheduledCancel` appears in `venues/hyperliquid/lookup.rs` and `ws.rs` only as a terminal order status the engine reads; nothing in `engine-venue/src/venues` sends the action that arms it. A venue-side cancel-all or disconnect-cancel would remove protective triggers while leaving positions open, so arming one is a decision, not a safety net |

Fencing is the operator's act, and it is venue-specific. Heartbeat loss and SSH
loss prove nothing: a host that answers neither can still hold a socket to the
venue and still be sending orders.

| Realm | Fence at the venue | What may stay | Provider alternative |
| :--- | :--- | :--- | :--- |
| `mainnet` (Bybit) | Delete the API key behind `BYBIT_REAL_API_KEY` in the Bybit UI | The read-only attestor key (`BYBIT_ATTEST_API_KEY`), which cannot trade | Power the instance off at the provider console |
| `mexc` | Delete the API key behind `MEXC_REAL_API_KEY` in the MEXC UI. The account UID in `mexc-account-bindings.json` does not change; add the new key's `sha256(api key)` to that file | Nothing MEXC-side is read-only today | Power the instance off at the provider console |
| `hyperliquid` | Revoke the API wallet (the venue calls it an agent) behind `HYPERLIQUID_REAL_API_WALLET_KEY` at the venue. The master account key is the owner's and is never on the host | The master account, untouched | Power the instance off at the provider console |
| `demo` (Bybit demo) | Delete the demo key (`BYBIT_DEMO_API_KEY` in `/etc/liquidity-migration/bybit-demo.env`). No money is at risk, but two writers on one demo account corrupt the evidence the realm exists to produce | — | Power the instance off at the provider console |

Either fence alone is sufficient. Power-off stops the host; credential deletion
stops its orders even if the host is alive and unreachable. Proof means the key
is gone from the venue's own key list, or the provider console shows the
instance stopped — not that a ping failed.

| Readiness gate, before arming | Command or check | Pass condition |
| :--- | :--- | :--- |
| Restored log | `engine-tools restore-check --wal PATH --spool DIR --controls DIR` | Exit 0, or exit 3 with every row it lists reconciled against the venue. Exit 1, 2 or 4 stops the arming |
| Account identity | `scripts/ops.sh verify-account-identity --environment <realm>` | The venue answers as `EXPECTED_ENGINE_ACCOUNT_USER_ID` |
| Venue inventory | `scripts/ops.sh attest-flat --environment <realm>` | Flat (or `flat-dust`). When it is not flat, compare the venue's working orders and positions against `restore-check`'s `open_orders` and `positions` instead |
| Protection | The venue's working-order list | Every position `restore-check` expects has its protective reduce-only trigger present at the venue. A position with no such order is unprotected, whatever the log's `intended_stop_px` says |
| Readiness posture | `engine venues`, `deploy/realms.tsv` | The realm's readiness gate and `posture` are what they were before the loss. A host replacement promotes nothing |
| Latch | `restore-check`'s `may_open` | Read, not reset. `engine reconcile-clear --execute` is the only deliberate reset, and it takes the WAL's own lock with the engine stopped |

| Stop condition | Do |
| :--- | :--- |
| `restore-check` exits 2 (`incompatible-reader`) | The restored log holds a record this binary refuses. Install the newer release from `releases/<commit>/` — never restore an older backup to make an old binary boot |
| `restore-check` exits 4 (`stale-backup`) | The newest stamp in the restored family is older than `--max-age-min`. Take a fresher copy from `engine-state/latest/`, or from `history/` if `latest/` is the stale one, before arming anything |
| `restore-check` exits 1 (`unreadable`) | The copy is not a log. Restore again from `history/`; do not start an engine on it |
| Fencing is unproven | Stop. An engine started against a live old writer double-sends into one account, and no lease on either host can see the other |
| The original host returns | Never reconnect it with valid credentials. Disarm it first (`REAL_MONEY=false` in its credential file and its units stopped), or leave its credentials deleted. Only then is it safe to read its disk |

- **Must Never** start an engine on a replacement host until the previous
  writer is fenced by power-off or by credential deletion at the venue.
- **Must Never** treat an absent or stale lease file, a missed heartbeat, or a
  dead SSH session on the new host as evidence that the old host stopped
  trading.
- **Must Never** arm on a `restore-check` verdict of `stale-backup`,
  `incompatible-reader` or `unreadable`.
- **Must Never** reconnect a recovered original host while its credentials
  still work.
- **Must** re-establish account identity and compare the venue against the
  restored log before arming, read-only, on the new host.
- **Must** write every credential by hand on the replacement host: no backup
  holds one.
- **Must** treat the restored log as up to one backup interval behind the
  venue, and the venue as the authority on what is held.

```sh
# 1. Fence the old writer. One of these, proven at the venue or the provider,
#    not assumed:
#      - the instance is powered off at the provider console, or
#      - the realm's write key is deleted in the venue's own key list
#        (Bybit/MEXC), or its API wallet is revoked (Hyperliquid).
#    Until one holds, stop here.

# 2. Restore onto the replacement host from engine-state/latest/, then read the
#    log before anything is armed. Read-only: no lock, no truncation, no write.
/opt/liquidity-migration-engine/bin/engine-tools restore-check \
  --wal /var/lib/liquidity-migration-engine-mainnet/engine.wal \
  --spool /var/lib/liquidity-migration/signals/mainnet \
  --controls /var/lib/liquidity-migration/controls/mainnet
#    Exit 0 ready-to-reconcile, 3 reconcile-required, 4 stale-backup,
#    2 incompatible-reader, 1 unreadable. --json for the same report as JSON.

# 3. Write the credential files by hand (no backup holds them) and prove the
#    account, with REAL_MONEY still false.
scripts/ops.sh verify-account-identity --environment mainnet
scripts/ops.sh attest-flat --environment mainnet
#    attest-flat is a two-scan proof of a flat account. When it is not flat,
#    compare the venue's working orders and positions against restore-check's
#    open_orders and positions, and confirm a protective trigger at the venue
#    for every position it expects.

# 4. Only then arm, through the existing runbook for that realm
#    (§Real-Money Configuration Dials, and §MEXC Realm or §Hyperliquid Realm
#    for a funded alt realm).
```

| Drill | Date | Outcome | Evidence |
| :--- | :--- | :--- | :--- |
| Restore `engine-state/latest/` to a clean host and run `restore-check` | unknown | unknown | unknown |
| Fence by credential deletion and prove the old key refused at the venue | unknown | unknown | unknown |
| Measured RTO from provider provision to armed | unknown | unknown | unknown |

This drill has not been run. Every cell above stays `unknown` until it is, and
the 60 min RTO is a proposal until the third row holds a measured number.

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
| Account-binding registry | `/etc/liquidity-migration/mexc-account-bindings.json`, owner `root`, a regular file, not group- or world-writable (`mode & 0o022 == 0`), at most 64 KiB, written by hand. Schema: `schema_version` (must be `1`), `realm` (must be `mexc_mainnet`), `accounts[].account_uid` (the physical account or subaccount UID verified at provisioning: one canonical positive decimal, no leading zero, at most 40 digits, unique in the file), `accounts[].credential_sha256[]` (non-empty, unique lowercase 64-hex `sha256(api key)` fingerprints) |
| Bound identity | `AccountBinding::load` runs inside `MexcGateway::new` and `MexcInventoryProbe::new`, so a credential the registry does not name is refused before any socket, authentication or mutation. Identity is `uid-<account_uid>`: rotating the API key inside one account keeps the identity and the lease path `/run/lock/liquidity-migration/mexc-mexc_mainnet-user-uid-<account_uid>.lock`, and two subaccounts stay two accounts. `identity_for` re-proves the key on every identity read |
| REST pacing | Process-local classified quota in `engine-venue/src/venues/mexc/rest.rs`: a 2 s rolling window of 16 signed requests per `(REST base, sha256(key))`, shared by the gateway, its recovery reader and the probe. `OperationClass` is `Recovery`, `Trading` (default), `Administration` (`position/change_leverage`) and `Protection` (`stoporder/*`, `order/cancel_with_external`, reduce-only `order/create`); `QuotaGroup` is `General` and `StopWrite`. Only `Protection` may take all 16 slots — every other class stops at 12, so a `SAFETY_RESERVE` of 4 protective slots survives a recovery sweep — and `stoporder/*` writes are additionally capped at 4 per window against the venue's 5. This is not a cross-process or IP-wide rate-limit claim |
| Sleeves | LONG entries on; CARRY and EXODUS entries rendered off. CARRY scores Bybit funding, MEXC funding differs per symbol. No maker, no probe |
| Public data | MEXC's own: `sources.public_venue = "mexc"` in `configs/signal-worker.mexc.json`. Instruments, tickers, hourly klines and settled funding from `api.mexc.com`, the `contract.mexc.com` `edge` socket for the live ticker and candle; quantities converted from contracts to base by `contractSize`; funding carries each contract's own `collectCycle` (8 h, 4 h, 1 h or 24 h). The Binance top-trader ratio and the LLM gate are shared. Switching venue changes the realm's feature-contract hashes and the worker's public-source contract; at its next start the worker archives the old checkpoint, journal and pending files under `drifted-source-<sha8>-<unix_ms>/` in its state directory and cold-starts |
| Symbols the venue does not list | Dropped before ranking: `universe.listed_on` is `mexc`, so the worker reads `GET /api/v1/contract/detail` on `live.instrument_cadence_ms` and keeps the USDT-settled, API-tradable contracts in the engine's spelling (`BTC_USDT` is `BTCUSDT`; Bybit's `1000PEPEUSDT` is not MEXC's `PEPEUSDT`). A name that still reaches the engine waits at admission and is said once |
| Source readiness | `mexc_mainnet` is `live-canary`: `engine run` runs as the owner's forward test (posture `running`, `REAL_MONEY` armed) and logs the unproven capabilities at boot; `engine canary-order` is permitted with `REAL_MONEY` armed. `engine venues` prints the current value and `verify` prints it as `mexc readiness=...` |
| Evidence boundary | The 2026-09-10 11:59 UTC canary (venue order `853000482766018560`, client id `lmcan-1a08b2fd4a5-087e-0000`) observed create, `New`, cancel, `Cancelled` and four clean scans on the current adapter: submit, cancel and post-only are `observed`. No fill, no fee, no reduction, no protective place or trigger, no reconnect or history recovery has been observed |
| What promotes it | Reviewed evidence on this exact realm, with the current adapter, for the capabilities being enabled: fill attribution, protective order place and trigger, and reconnect/history recovery. The forward test gathers it; each receipt is a dated `Observed` cell in the matrix row |

**Must** write `/etc/liquidity-migration/mexc-account-bindings.json` before any
MEXC mode runs; without it every MEXC gateway and probe refuses at construction.
**Must** obtain `EXPECTED_ENGINE_ACCOUNT_USER_ID` from an authenticated venue
reply. MEXC exposes no numeric account id in its replies, so the engine reports
the registry's `uid-<account_uid>`; `deploy/engine.mexc.env.template` carries
the `uid-` prefix, and the host's `engine-mexc.env` still holds the retired
`key-…` value until the owner rewrites it.
**Must** know that `REAL_MONEY=true` in `mexc-mainnet.env` alone does not start
the realm: deploy reads `engine venues` from the installed binary, renders and
projects the realm's configuration whenever the switch is armed, and starts the
units only when the readiness is one `engine run` accepts (`live-proven` or
`live-canary`) and `deploy/realms.tsv` holds `mexc posture=running`. Deploy
tests readiness before posture.
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

# 3. Bind the credential to the physical account. Read the account UID from
#    MEXC's own interface, take sha256 of the API key, and write both into the
#    registry. Nothing on this host discovers the UID for you.
printf '%s' "$MEXC_REAL_API_KEY" | sha256sum   # the 64-hex fingerprint
cat > /etc/liquidity-migration/mexc-account-bindings.json <<'JSON'
{"schema_version": 1, "realm": "mexc_mainnet",
 "accounts": [{"account_uid": "<UID>", "credential_sha256": ["<64-hex>"]}]}
JSON
chown root:root /etc/liquidity-migration/mexc-account-bindings.json
chmod 0644 /etc/liquidity-migration/mexc-account-bindings.json

# 4. Deploy once with the switch off: deploy installs the units and leaves
#    them stopped. Then read the account id the gateway binds. Any wrong value
#    in engine-mexc.env mismatches and the message prints the id the
#    credentials answered as, which is `uid-<UID>`. Write it in, rerun until
#    it passes, then prove the account clean.
gh workflow run vps-deploy.yml --ref main -f mode=deploy
scripts/ops.sh verify-account-identity --environment mexc
scripts/ops.sh attest-flat --environment mexc

# 5. Arm REAL_MONEY=true in mexc-mainnet.env, then deploy; that deploy renders
#    engine-mexc.toml and projects the worker env so the canary has a
#    configuration, and leaves the units stopped while the posture is stopped.
#    Take the canary while the realm is stopped: a running engine holds the lease.
scripts/ops.sh real-money preflight-mexc
gh workflow run vps-deploy.yml --ref main -f mode=deploy
scripts/ops.sh canary-order --environment mexc --symbol BTCUSDT \
  --expected-user-id uid-<UID> --execute

# 6. Record the canary receipt in CHANGELOG.md and as dated Observed cells in
#    the realm's row in engine/engine-public/src/registry.rs, set its posture
#    to running in deploy/realms.tsv, push, and deploy again; that deploy
#    starts the realm. live-proven follows from the row, never from a label.
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
| Public data | Hyperliquid's own: `sources.public_venue = "hyperliquid"` in `configs/signal-worker.hyperliquid.json`. `meta` and `metaAndAssetCtxs` for instruments and tickers, `candleSnapshot` for hourly klines (quote turnover is approximated as base volume × the bar's mean price, because the venue states none), `fundingHistory` for the hourly settled rate stamped on the hour, and the `activeAssetCtx`/`candle` socket. Listing age comes from the first daily candle. The engine prices against Hyperliquid's own `bbo` / `activeAssetCtx` socket |
| Source readiness | `hyperliquid_mainnet` is `live-canary`: `engine canary-order` runs, and `engine run` runs as the owner's forward test (posture `running`, `REAL_MONEY` armed), logging the unproven capabilities at boot. `engine venues` prints the current value. The 2026-09-10 11:59 UTC canary (venue order `541177774027`, client id `lmcan-1a08b2fcd82-08b6-0000`) observed submit, cancel and post-only on the funded address |

| Venue fact | Where it changes a decision |
| :--- | :--- |
| Funding settles and is quoted hourly | A carry number taken from Bybit's eight-hourly rate is out by a factor of eight, so CARRY and EXODUS entries are rendered off |
| A stop is a separate reduce-only trigger order, not a field on the position | It appears in the working-order list of every scan, and a position with no such order reads as unprotected |
| Minimum order notional 10 USD | An order under it is refused at admission (`engine/engine-core/src/engine/intent_admission.rs`), not sent |
| Limit orders only | A market intent goes to the venue as an IOC limit through the book |
| Symbols the venue does not list | Dropped before ranking: `universe.listed_on` is `hyperliquid` in `configs/signal-worker.hyperliquid.json`, so the worker filters Bybit mainnet's domain to `POST /info {"type":"meta"}` on `live.instrument_cadence_ms`. A name that still reaches the engine is refused at admission |
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
reports a readiness `engine run` accepts (`live-proven` or `live-canary`) and
the table holds `posture=running`; `verify` prints `hyperliquid armed|off` and
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
#    hyperliquid unit stopped while the posture is stopped.
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

# 6. Record the canary receipt in CHANGELOG.md and as dated Observed cells in
#    the realm's row in engine/engine-public/src/registry.rs, set its posture
#    to running in deploy/realms.tsv, push, and deploy again; that deploy
#    starts the realm. live-proven follows from the row, never from a label.
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
| **Market Tape Hours** | Hourly at :10 (`upload.timer`)| `LiquidityMigration/market-tape/<tape>/YYYY/MM/DD/` | Permanent archive; the host keeps a 6 h sliding window of shipped hours ([market_tape/README.md](../market_tape/README.md) §Local Sliding Window) |
| **Reclaimed Sealed History** | Hourly at :41 (`storage-reclaim.timer`), only below the low-water mark | `LiquidityMigration/engine-state/sealed/` | Sealed WAL segments and archived quarantine files the host has reclaimed; permanent |

| Local backup stage | Contract |
| --- | --- |
| Sealed WAL segments | After successful remote checksum verification, byte-identical staged copies of numbered segments below the current maximum become hard links to the immutable source on the same filesystem |
| Growing WAL / other state | Remain independent copies; rsync uses replacement files, never `--inplace`; a later append cannot change the active segment's staged snapshot |
| Physical disk usage | Linking releases duplicate blocks without pruning cloud history; `du` on the stage alone still counts shared blocks. A source segment is pruned only by the reclaimer below, only below the engine's own retention floor, and only after remote verification |
| Stage on its own mount | `link` needs one mount, not one matching `st_dev`: a stage the kernel refuses a link into keeps both copies, counts `unlinkable_roots=` in the run's last line, and leaves the backup successful |
| Implementation | `scripts/runtime/backup_state.sh`, `scripts/runtime/link_sealed_backup_wals.py`; the existing backup lock covers staging, verification and linking |
| Mount namespace | The script creates `backup/` on the same mount as source WALs; systemd manages only `receipts/` through `StateDirectory`, because a separate backup bind mount prevents hard links even when device IDs match |
* **Security Invariant**: Backup scripts explicitly reject `*.env` files to prevent credentials from ever leaving the host.

---

### Host storage reclamation

`liquidity-migration-storage-reclaim.service`, hourly at :41 UTC, runs `scripts/runtime/reclaim_host_storage.py` as root. It measures the filesystem, prunes what is rebuildable, and reclaims sealed history only after that history is verified off-box.

| Budget term | Value |
| :--- | :--- |
| Reserve | `max(12% of capacity, 8 GiB)` |
| Tape floor | the highest `[storage].min_free_disk_gb` in `deploy/capture/*.toml` (12 GiB); a recorder under it counts every frame and writes none |
| Writer headroom | `6 GiB` |
| Low water | `max(reserve, tape floor)` + writer headroom; below it, sealed-WAL reclamation is permitted, so verified WAL yields before a recorder blocks |
| High water | low water + two days of measured growth |
| Growth measurement | `statvfs` samples appended to `samples.jsonl`, one per run |
| Runway | `estimated_runway_s` = (free − low water) / growth, and `runway_with_verified_history_s` counting retained verified WAL as reclaimable, both in `status.json` |

| Reclaim class | Rule | Keep set | Verification | Destination |
| :--- | :--- | :--- | :--- | :--- |
| Release directories and staged tarballs under `/opt/liquidity-migration-engine` | Every run | deployed commit, previous commit, `8c92c964…`, any override-referenced release, anything younger than 1 day | Local: the retained release is the one the fleet runs | Deleted |
| Apt cache | Every run, `apt-get clean` | — | Rebuildable from the archive | Deleted |
| Archive roots (`/var/lib/liquidity-migration-wal-quarantine`) | Every run, immutable files only, 2 GiB per run | — | Uploaded, then verified by size and md5 | `engine-state/sealed/`, then deleted |
| Sealed WAL segments | Only while free space is below low water; oldest first; 12 GiB per run | at or above `retention_floor_segment`, the newest three numbered segments, anything under 48 h old, and segment 1 (`engine.wal`) always | (a) below the engine's floor from `engine-tools wal-retention --json`, (b) not the newest 3, (c) older than 48 h, (d) hard-linked into the backup stage, (e) re-verified against `engine-state/latest/` by size and md5, (f) server-side copied to `engine-state/sealed/` and verified there | `engine-state/sealed/`, then unlinked from source and stage |

| File | Holds |
| :--- | :--- |
| `/var/lib/liquidity-migration/storage-reclaim/status.json` | The last run's measurement: capacity, free, reserve, tape floor, low and high water, measured growth, runway, per-class bytes reclaimed (`st_blocks × 512`), the unverified backlog with reasons, the run's `plan` (what went, or would go under `--dry-run`), `errors` and `lock_timeout` |
| `/var/lib/liquidity-migration/storage-reclaim/md5-cache.json` | Local md5 of sealed segments keyed by `dev:ino:size:mtime_ns`, so an hourly run does not re-read 40 GB beside the engines |
| `/var/lib/liquidity-migration/storage-reclaim/ledger.jsonl` | One append-only row per reclaimed file: class, path, bytes, md5, remote destination, timestamp; a `wal` row also carries the deleted inode's `st_dev` and `st_ino`. Written and fsynced before the unlink. The realm watchdogs read it (`reclaimed_wal_identities` in `check_fleet_liveness.py`) so a segment the reclaimer deleted is not a segment the family lost |
| `/var/lib/liquidity-migration/storage-reclaim/samples.jsonl` | One `statvfs` sample per run; the growth measurement reads this |
| `/var/lib/liquidity-migration/receipts/storage-reclaim.last-success` | `key=value` receipt written only when every step succeeded, like `backup.last-success`; the host watchdog warns (`storage-reclaim`) when it is older than `--max-reclaim-age-hours 3` or missing |

**Invariants**

- Must persist and fsync the ledger row before unlinking the file it describes.
- Must hold the backup's own lock (`/var/lib/liquidity-migration/backup/backup.lock`) while unlinking a source segment and its stage link.
- Must exit non-zero unless every step of the run succeeded.
- Must never delete a segment at or above `retention_floor_segment`, in the newest three numbered segments, or segment 1 (`engine.wal`).
- Must never delete anything that has not been verified remotely by size and md5.
- Must never touch the tape roots; the market-tape upload owns its own sliding window.
- Must never reclaim sealed WAL while free space is at or above the low-water mark.

```sh
# The last run's receipt and status, read on the host.
scripts/ops.sh storage

# What one run would reclaim right now, measured and reported, nothing touched.
scripts/ops.sh storage plan

# The engine's own retention floor for one family, on the host.
engine-tools wal-retention --wal /var/lib/liquidity-migration-engine-mainnet/engine.wal
```

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
| The worker quarantined a candidate | Its sequence is missing from the spool, so the engine treats it as a gap. Read the `.reason` sidecar, then either repair-and-republish the exact envelope or accept the gap through the rows above; the quarantined pair is removed by hand afterwards |
| The worker reports `spool_unreadable_files` | The candidate is still in the engine's scan path: quarantine is at its 256-file / 256 MiB bound, already holds that file name, or the rename failed. Clear the quarantine directory of resolved pairs, or repair the filesystem fault the reason names |

Must never delete later-generation rows, rewrite accepted hashes, or edit a live cursor to clear a gap. Inspect logs and quarantined evidence read-only before selecting a recovery action (`<realm>` is `demo`, `mainnet`, `mexc`, or `hyperliquid`):

```bash
journalctl -u liquidity-migration-signal-worker-<realm> -n 100 --no-pager
journalctl -u liquidity-migration-engine<-mainnet or empty> -n 100 --no-pager
ls -l /var/lib/liquidity-migration/signals/<realm>/quarantine/
cat /var/lib/liquidity-migration/signals/<realm>/quarantine/*.reason
```

After the sequence is republished or its gap accepted, remove the pair by hand; the worker never deletes it:

```bash
sudo -u liquidity-signal-worker rm \
  /var/lib/liquidity-migration/signals/<realm>/quarantine/<name> \
  /var/lib/liquidity-migration/signals/<realm>/quarantine/<name>.reason
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
