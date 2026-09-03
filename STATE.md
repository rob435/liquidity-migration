# Operational State

Current operational snapshot of the live trading fleet and host environment.

---

## 1. Live Deployment Snapshot

Verified against the running host on 2026-09-03 19:12 UTC:

| Property | Value | Description |
| :--- | :--- | :--- |
| **Host** | `ip-208-84-103-4.my-advin.com` (`208.84.103.4`) | 4 vCPU, 8 GB RAM, 118 GB disk (44% used, 64 GB free). |
| **Deployed Commit** | `ce252af8` (`engine backtest`: unsynced log, deepest chained book, the recorder's budget controller) | Deployed 19:09 UTC 2026-09-03 from the CI artifact; both engines and both workers on fresh heartbeats. Both recorders restarted with the budget fix, which published one fresh 50-level book snapshot per symbol at 19:09:10Z — the anchor every deep-book replay chains from. |
| **Rollback Target** | `d43efaaf811b3192ac1bd36691204dcea7508ac8` | Stored in `/opt/liquidity-migration-engine/previous-commit`. |
| **Funded Status** | `real-money armed` | `REAL_MONEY=true`. The engine is running with healthy heartbeats. |
| **Signal IPC** | spool row + `stream.sock` doorbell | Every observation is a spool row first; the socket frame only saves the engine its next poll. Worker generations were renewed on both realms on 2026-09-03 after the desync; the old `g805c44f0…` (mainnet) and `gc4d0071f…` (demo) cursors stay in the WAL. |

---

## 2. Active Strategy Sleeves & Dials

| ID | Sleeve | Status | Rules & Dials | Execution Mandate |
| :---: | :--- | :--- | :--- | :--- |
| **0** | **CARRY** | Active | `configs/lane2_carry_hold_v7.json` | $3\times$ notional, $5\times$ leverage, $35\%$ stop. Entry $\le -10\text{ bp}$, exit $>-3\text{ bp}$. |
| **1** | **LONG** | Active | `configs/long_native_v12.json` | $6.0\times$ operational mult, top-10 volume rank, $15\%$ price velocity, $3\times\text{ATR}$ stop, $3\text{d}$ hold. |
| **2** | **EXODUS** | Active | `configs/lane2_exodus_short_v1.json`| Consumes CARRY pre-settlement event. Enters short; covers at $S+60\text{m}$. |
| **3** | **MAKER** | Disabled | `configs/lane2_toxic_flow_quoter_v1.json` | Microstructural market making on `AGIUSDT` (`quote_enabled = false`). |

### Shared Account Risk Dials (`configs/operational.json`)
* **Gross Exposure Ceiling**: $5.0\times$ equity.
* **Initial Margin Ceiling**: $1.0\times$ equity.
* **Capital Reference**: Floating equity (floored at $\$100$).
* **Rolling-Loss Circuit Breaker**: $0.10$ ($10\%$ of capital reference lost in 24h trips emergency entry halt). A trip survives a restart, is reported by `rolling_loss_tripped`, and shows every sleeve as `entries_enabled: false`.

---

## 3. Fleet Health & Systemd Unit Matrix

| Systemd Unit | Realm | Health State | Heartbeat SLA |
| :--- | :--- | :--- | :--- |
| `liquidity-migration-engine.service` | Demo | Active | $\le 5\text{s}$ fresh |
| `liquidity-migration-engine-mainnet.service` | Mainnet | Active | $\le 5\text{s}$ fresh |
| `liquidity-migration-signal-worker-demo.service` | Demo | Active | $\le 5\text{s}$ fresh |
| `liquidity-migration-signal-worker-mainnet.service`| Mainnet | Active | $\le 5\text{s}$ fresh |
| `liquidity-migration-forward-capture.service` | Bybit | Active | The tape that gets replayed: `book:50` and every print on the acting tiers, `book:1` on the canary, ticker and liquidations on all ~716 USDT perps. `monthly_gb = 1800` against 1,710 projected at full tier width, so nothing sheds; `core:trades` and the tickers are not in `shed` at all. Books re-anchor each UTC hour, so every uploaded hour replays on its own. |
| `liquidity-migration-forward-capture-binance.service`| Binance | Active | Cross-venue reference only, **no order book**: ticker (funding rate, mark, index) on all ~511 USDT perps plus trades on the acting tiers. `monthly_gb = 700`. |
| `liquidity-migration-telegram-controls.service` | Global | Active | Polling authorized chats |
| `liquidity-migration-trade-notify.timer` | Global | Active | 5-minute trade scanning |

---

## 4. Operational Invariants & Reference Runbooks

* **Runbook & CLI**: See [docs/operations.md](docs/operations.md).
* **Architecture & IPC**: See [docs/architecture.md](docs/architecture.md).
* **Engine Internals**: See [docs/engine.md](docs/engine.md).
* **Data & Tapes**: See [docs/data.md](docs/data.md).
* **Trading Logic**: See [docs/trading_logic.md](docs/trading_logic.md).
