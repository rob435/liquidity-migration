# Notifications & Bot Controls Specification

Telegram notification surfaces, trade event dispatchers, liveness alerts, and interactive bot controls.

---

## 1. Notification Surfaces

| Surface | Systemd Unit | Schedule / Trigger | Channel Target | Authority |
| :--- | :--- | :--- | :--- | :--- |
| **Trade Updates** | `liquidity-migration-trade-notify.timer` | Every 5 minutes | `TELEGRAM_CHAT_ID` | Read-only (heartbeat & trade log) |
| **Realm Liveness** | `liquidity-migration-{demo,mainnet}-liveness.timer` | Periodic timer | `TELEGRAM_ALERT_CHAT_ID` | Read-only health monitor |
| **Host Liveness** | `liquidity-migration-host-liveness.timer` | Periodic timer | `TELEGRAM_ALERT_CHAT_ID` | Read-only host monitor |
| **Operator Bot** | `liquidity-migration-telegram-controls.service` | Continuous long-polling | Configured chat / whitelist | Sudo helper control spool |

---

## 2. Trade Updates (`trade-notify.service`)

Scans engine artifacts every 5 minutes and emits monospace HTML messages:

| Realm | Position Source (Openings) | Exit Source (Closed Trades) |
| :--- | :--- | :--- |
| **Demo** | `/var/lib/liquidity-migration-engine/heartbeat.json` | `/var/lib/liquidity-migration-engine/trades.jsonl` |
| **Mainnet** | `/var/lib/liquidity-migration-engine-mainnet/heartbeat.json` | `/var/lib/liquidity-migration-engine-mainnet/trades.jsonl` |

### Message Contents & Invariants
* **Entry Alert**: Emitted when a fresh engine heartbeat shows a new attributed position (`LONG`, `CARRY`, or `EXODUS`).
* **Exit Alert**: Emitted when a closed trade appears in `trades.jsonl`. Reports sleeve, symbol, side, hold time, realized PnL net of venue fees, return on notional, and slippage shortfall.
* **Canary Filter**: `maker_canary` trades are recorded in `trades.jsonl` but suppressed from Telegram alerts.
* **Daily Digest**: Emits a 00:00 UTC daily performance card splitting Demo and Mainnet realized totals.

---

## 3. Liveness Alert Matrix

Alerts trigger via `TELEGRAM_ALERT_CHAT_ID` (falls back to main chat if unset):

| Scope | Monitored Condition | Trigger Threshold |
| :--- | :--- | :--- |
| **Realm** | Fleet Unit State | Any expected manifest service inactive. |
| **Realm** | Heartbeat Freshness | Engine or Signal Worker heartbeat $> 30\text{s}$ old. |
| **Realm** | Engine Admission | Engine heartbeat reports `can_open = false`. |
| **Realm** | Circuit Breaker | Engine `RollingLossTripped` is active. |
| **Host** | Tape Recorders | Capture status file stale, dropped frames, or no frames in 2m. |
| **Host** | Budget Warning | Recorder monthly inbound projection exceeds 1,300 GB. |
| **Host** | Upload Age | Google Drive tape upload receipt $> 3\text{h}$ old or Drive $< 200\text{ GB}$ free. |
| **Host** | Backup Age | Off-box engine state backup receipt $> 8\text{h}$ old. |
| **Host** | Storage & Clock | `/var/lib` disk space $< 25\text{ GB}$ or host clock out of sync. |

---

## 4. Telegram Interactive Control Bot

Long-polls commands from authorized operators (`TELEGRAM_CONTROL_USER_IDS`):

| Command | Action | Behavior |
| :--- | :--- | :--- |
| `/status` | Fleet Status | Displays unit states, heartbeats, and entry permissions for Demo and Mainnet. |
| `/pause_demo` | Pause Demo | Sets `entries_enabled=false` on Demo. Exits, stops, and settlement clocks continue. |
| `/resume_demo` | Resume Demo | Restores pre-pause entry settings on Demo. |
| `/pause_mainnet` | Pause Mainnet | Sets `entries_enabled=false` on Mainnet. Available only when Mainnet is live. |

* **Safety Invariant**: There is **no `/flatten` button and no `/resume_mainnet` button** in the Telegram bot. Emergency position liquidation and real-money arming require explicit shell execution via `scripts/ops.sh`.

---

## 5. Configuration Variables (`/etc/liquidity-migration/*.env`)

| Variable | Description |
| :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | HTTP API authentication token from BotFather. |
| `TELEGRAM_CHAT_ID` | Primary destination chat ID for trade updates and status. |
| `TELEGRAM_ALERT_CHAT_ID` | Dedicated alert channel for critical liveness warnings. |
| `TELEGRAM_CONTROL_USER_IDS`| Comma-separated list of numeric Telegram user IDs authorized to execute commands. |

---

## 6. Diagnostic Commands

```bash
# Inspect notification and control logs
scripts/ops.sh logs trade-notify.service 100
scripts/ops.sh logs demo-liveness.service 100
scripts/ops.sh logs mainnet-liveness.service 100
scripts/ops.sh logs host-liveness.service 100
scripts/ops.sh logs telegram-controls.service 100
```
