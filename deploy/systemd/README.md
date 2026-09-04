# Fleet Systemd Units & Lifecycle Specification

Systemd service topologies, daemon identities, security sandboxing, and execution lifecycle.

---

## 1. Systemd Fleet Unit Inventory

Defined canonically in [`deploy/fleet_manifest.tsv`](../fleet_manifest.tsv):

| Unit Family | Realm | Systemd Target / Activation | User / Group | Authority & Role |
| :--- | :--- | :--- | :--- | :--- |
| `liquidity-migration-signal-worker-demo` | Demo | `multi-user.target` | `liquidity-signal-worker:liquidity-migration` | Public market data ingestion & observation streaming. |
| `liquidity-migration-signal-worker-mainnet` | Mainnet | `multi-user.target` | `liquidity-signal-worker:liquidity-migration` | Public market data ingestion & observation streaming. |
| `liquidity-migration-engine` | Demo | `multi-user.target` | `liquidity-engine-demo:liquidity-migration` | Demo execution engine & order authority. |
| `liquidity-migration-engine-mainnet` | Mainnet | `manual` (needs `REAL_MONEY`) | `liquidity-engine-mainnet:liquidity-migration` | Funded execution engine & order authority. |
| `liquidity-migration-forward-capture` | Global | `independent` (boot) | `liquidity-capture:liquidity-migration` | Bybit tick, book, and L2 market tape recorder. |
| `liquidity-migration-forward-capture-binance` | Global | `independent` (boot) | `liquidity-capture:liquidity-migration` | Binance tick, book, and L2 market tape recorder. |
| `liquidity-migration-market-tape-upload` | Global | Timer (hourly at :10) | `root:root` | Tar & rclone sync to Google Drive. |
| `liquidity-migration-backup` | Global | Timer (every 6h) | `root:root` | Off-box mirror of engine state & WAL to Google Drive. |
| `liquidity-migration-trade-notify` | Global | Timer (every 5m) | `liquidity-observer:liquidity-migration` | Monospace HTML trade alerts to Telegram. |
| `liquidity-migration-telegram-controls` | Global | `multi-user.target` | `liquidity-controls:liquidity-controls` | Long-polling Telegram bot helper. |
| `liquidity-migration-demo-liveness` | Demo | Timer (periodic) | `liquidity-observer:liquidity-migration` | Realm-level SLA & heartbeat monitoring. |
| `liquidity-migration-mainnet-liveness` | Mainnet | Timer (periodic) | `liquidity-observer:liquidity-migration` | Realm-level SLA & heartbeat monitoring. |
| `liquidity-migration-host-liveness` | Global | Timer (periodic) | `liquidity-observer:liquidity-migration` | Host, recorder, realm-watchdog supervision, and the one external dead-man ping. |
| `liquidity-migration-chaos-drill` | Demo | Timer (weekly) | `root:root` | Automated demo restart & state recovery drill. |

---

## 2. Independent Units (Host-Level Daemons)

The 5 unit families marked `independent` (`forward-capture`, `forward-capture-binance`, `market-tape-upload`, `backup`, `host-liveness`):
* **Never Stopped**: Fleet deploys, safety stops, and disarm actions never terminate independent units.
* **Boot Activation**: Start automatically on machine boot.
* **Conditional Restart**: Deploy restarts capture services only if `deploy/capture/`, `market_tape/`, or dependencies changed.

---

## 3. Sandboxing & Linux Security Invariants

1. **State Directory Isolation**: Each service writes strictly to its declared `StateDirectory` in `/var/lib/` (`0750` / `0770`).
2. **Environment Scrubbing**: Signal worker units explicitly unset venue API credentials, real-money switches, and Telegram tokens.
3. **No Private Leaks in Backups**: Off-box backup scripts strictly ignore `*.env` files to prevent credentials from leaving the host.
4. **Arming Protection**: `engine-mainnet.service` requires `REAL_MONEY=true` in `/etc/liquidity-migration/bybit-mainnet.env` to start.
5. **Notification Isolation**: Observer and control units load `/etc/liquidity-migration/notifications.env`; liveness units additionally load `/etc/liquidity-migration/oncall.env`. They never load venue credential files.
