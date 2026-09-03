---
name: run-strategy
description: Construct and run current liquidity_migration CLI, data, audit, and demo operational commands safely. Use whenever invoking python -m liquidity_migration or scripts/ops.sh so data roots, end-exclusive boundaries, profiles, PIT modes, and mutation handshakes come from current help and code. Never assume today's date, a dry run, cross-venue scope, or mainnet authority.
---

# Operational Commands & Strategy Execution Router

## 1. Purpose
Specify CLI routing, execution boundaries, safety handshakes, and operational parameters for running research workflows, data ingestion, and fleet operations across demo and funded environments.

---

## 2. Spec Tables

### Operations CLI Command Matrix (`scripts/ops.sh`)

| Command | Subcommand / Flags | Target Scope | Mutation | Role / Function |
| :--- | :--- | :--- | :---: | :--- |
| `status` | None | Fleet-wide | Read-only | Reports live commit, deployed commit, armed state, heartbeats, and disk. |
| `deploy` | `[deploy\|rollback\|verify]` | Production VPS | **Mutating** | Executes exact-commit binary update with automatic 180s health rollback. |
| `deploy` | `stop-mainnet` | Funded Engine | **Mutating** | Stops the funded engine unit while leaving demo units and signal workers active. |
| `deploy` | `disarm-mainnet` | Host Credentials | **Mutating** | Rewrites `REAL_MONEY=false` in the host credential file; persistent disarm. |
| `real-money` | `preflight` | Host Credentials | Read-only | Validates mainnet keys, IP whitelist, account user ID, and profile dials. |
| `attest-flat`| `--environment <realm>` | Venue Account | Read-only | Authenticated two-scan venue proof that the account holds zero open positions. |
| `flatten` | `--environment <realm> [--execute]` | Strategy Sleeves | **Mutating** | Commands reducers to close all open positions. Read-only without `--execute`. |
| `equity` | `[--sleeves S] [--combined]` | Research Data | Read-only | Generates standard repository equity curve plots and JSON summaries. |
| `units` | None | Systemd Units | Read-only | Lists status of all fleet services and timers. |
| `logs` | `<unit> [lines]` | Journald | Read-only | Tails live journal logs for a specific service. |

### Research Data Roots

| Data Root Path | Exchange Venue | Dataset Type | Coverage & Usage | Invariant |
| :--- | :--- | :--- | :--- | :--- |
| `~/SHARED_DATA/bybit_full_pit` | Bybit Perpetuals | Full Point-in-Time | Primary research root for LONG and CARRY strategies. | Must verify manifest before study. |
| `~/SHARED_DATA/binance_full_pit`| Binance Perpetuals | Full Point-in-Time | Secondary venue for cross-venue transfer and robustness studies. | Not an independent data source. |

### Operational Boundaries & Safety Flags

| Realm | Safety Switch | File Location | Behavior |
| :--- | :--- | :--- | :--- |
| **Demo** | None (Runs by default) | `/etc/liquidity-migration/bybit-demo.env` | Trades demo exchange account; not a zero-effect dry run. |
| **Mainnet** | `REAL_MONEY=true` | `/etc/liquidity-migration/bybit-mainnet.env` | **Armed**: Real capital is traded under strict risk kernel limits. |
| **Mainnet** | `REAL_MONEY=false` | `/etc/liquidity-migration/bybit-mainnet.env` | **Disarmed**: Engine aborts at startup before opening venue sockets. |

---

## 3. Invariants

- **Demo Is Not a Dry Run**: Commands executed against the demo realm mutate the live external demo account; treat demo state transitions with discipline.
- **Must Never Set `REAL_MONEY` Without Explicit Owner Command**: Never enable or inject `REAL_MONEY=true` on your own initiative; real money is the owner's single explicit arming switch.
- **Date Boundaries Are End-Exclusive**: In all data and research CLI commands, `--start` is inclusive and `--end` is strictly exclusive (`[start, end)`).
- **Canary Orders Restricted to Demo**: `engine canary-order` *must only* run on `bybit_demo` and *must never* be directed at mainnet.

---

## 4. Operational Recipes

### Inspect Fleet Health & Live Status
```bash
# Check running commit, arming state, unit heartbeats, and disk space
scripts/ops.sh status

# Inspect funded engine logs
scripts/ops.sh logs engine-mainnet.service 50
```

### Prove Account Flatness
```bash
# Authenticate against venue and prove zero open positions on demo
scripts/ops.sh attest-flat --environment demo

# Authenticate against venue and prove zero open positions on funded account
scripts/ops.sh attest-flat --environment mainnet
```

### Execute Single Bounded Canary Order (Demo Only)
```bash
# Verify resting post-only placement, stop attachment, cancellation, and clean account
/opt/liquidity-migration-engine/bin/engine canary-order \
  --config /etc/liquidity-migration/engine.toml \
  --symbol XRPUSDT \
  --expected-user-id 579580669 \
  --execute
```

### Build Point-in-Time Historical Dataset
```bash
# Ingest and verify Point-in-Time klines and funding data
python -m liquidity_migration --data-root ~/SHARED_DATA/bybit_full_pit archive-manifest \
  --start 2024-01-01 --end 2025-01-01
```
