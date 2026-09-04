# Operational State

Current operational snapshot of the live trading fleet and host environment.

---

## 1. Live Deployment Snapshot

Verified against the running host on 2026-09-04 16:01 UTC:

| Property | Value | Description |
| :--- | :--- | :--- |
| **Host** | `ip-208-84-103-4.my-advin.com` (`208.84.103.4`) | 4 vCPU, 8 GB RAM, 118 GB disk (70% used, 35 GB free — 64 GB free at 00:10 UTC and 36 GB at 15:07 the same day; the watchdog's floor is 25 GB on `/var/lib`). |
| **Deployed Commit** | `1193043` (hold recorder alerts through deploy handoff) | Deployed 16:01:04 UTC 2026-09-04 by run `33891965516`, `deploy-ok`. Carries the two recorder-page fixes with it: `e6dec04` (`started_at_ns` in `status.json`) and `1193043` itself. Both realms took an atomic handover; both recorders `result=restarted` behind the new readiness gate, which printed `capture-ready pid=2171463 age=0s` (Bybit) and `capture-ready pid=2175857 age=2s` (Binance). Verified on the host in the same receipt: both engines `active` with 2 s and 3 s heartbeats, both signal workers 2 s and 1 s, the Bybit recorder 21 s and the Binance recorder 10 s, every timer `active`, `real-money armed`. |
| **Rollback Target** | `bf30fd67cb4d1b5c44d706c2f85bfa7adeac6d13` | Stored in `/opt/liquidity-migration-engine/previous-commit`. |
| **Funded Status** | `real-money armed` | `REAL_MONEY=true`. The engine is running with healthy heartbeats. |
| **Equity History** | `liquidity-migration-equity-recorder.timer`, every minute | One sample per engine and per tape recorder to `/var/lib/liquidity-migration/equity/<kind>-<realm>-<YYYY-MM>.jsonl`; a realm with no readable heartbeat is written as `state=absent`. `scripts/ops.sh curve mainnet` reads it on the host. Each run also pushes four samples to Grafana Cloud stack `proudtortoise1017` through `influx-prod-55-prod-gb-south-1`; `/etc/liquidity-migration/observability.env` is `root:liquidity-migration` mode `0640`, and the dedicated access policy grants only `metrics:write`. Verified through 12:32 UTC on 2026-09-04: every scheduled run reports `recorded and pushed 4 samples`. Dashboard UID `liqmig-fleet` is bound to `grafanacloud-proudtortoise1017-prom`. [docs/observability.md](docs/observability.md). |
| **Signal IPC** | spool row + `stream.sock` doorbell | Every observation is a spool row first; the socket frame only saves the engine its next poll. Worker generations were renewed on both realms on 2026-09-03 after the desync; the old `g805c44f0…` (mainnet) and `gc4d0071f…` (demo) cursors stay in the WAL. |

---

## 2. Active Strategy Sleeves & Dials

| ID | Sleeve | Status | Rules & Dials | Execution Mandate |
| :---: | :--- | :--- | :--- | :--- |
| **0** | **CARRY** | Active | `configs/lane2_carry_hold_v7.json` | $3\times$ notional, $5\times$ leverage, $35\%$ stop. Entry $\le -10\text{ bp}$, exit $>-3\text{ bp}$. |
| **1** | **LONG** | Active | `configs/long_native_v12.json` | $6.0\times$ operational mult, top-10 volume rank, $15\%$ price velocity, $3\times\text{ATR}$ stop, $3\text{d}$ hold. |
| **2** | **EXODUS** | Active | `configs/lane2_exodus_short_v1.json`| Consumes CARRY pre-settlement event. Enters short; covers at $S+60\text{m}$. |
| **3** (mainnet) | **MAKER** | Disabled | `configs/lane2_toxic_flow_quoter_v1.json` | Microstructural market making on `AGIUSDT` (`quote_enabled = false`). |
| **3** (demo) | **PROBE** | Active | `[[strategy]] probe` in `deploy/engine.demo.toml.template` | Order-path benchmark, not a trading sleeve: one venue-minimum post-only `BTCUSDT` buy 3% under the bid every 15 min on the wall clock, pulled 2 s later, so the latency ledger holds a measurement the :20 sample reads. A fill is closed at market and shows only as an entry blocker. Never a page or a Telegram message. Mainnet has no probe. |

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
| `liquidity-migration-forward-capture.service` | Bybit | Active | The tape that gets replayed, shaped for the sleeves' exits first. Domain is crypto only: 517 USDT perpetuals, stocks/ETFs/commodities in no tier. `core` = LONG's live band (turnover rank ≤ 120, leaves below 160, held 96 h after last ranking inside 120) with `book:50` + prints + ticker + liquidations; `crowded` = CARRY's whole hold zone (predicted funding ≤ −3 bp, held 72 h) with book + prints; discovery tiers (`overheated`, `movers`, `bursting`, `surging`, `flooding`, `levering`) with book + prints on what bandwidth is left; `wide` = every other name with **prints**, ticker and liquidations. `monthly_gb = 2400`; shed order `overheated` → `wide:trades` → discovery books → discovery prints; `core:*`, `crowded:*` and every ticker are never shed. Books re-anchor each UTC hour, so every uploaded hour replays on its own. Writer queue `queue_frames = 131072`; shards read frames with websocket-client's pure-Python UTF-8 validation off (`json.loads` rejects a bad frame anyway), which halves receive CPU per frame. |
| `liquidity-migration-forward-capture-binance.service`| Binance | Active | Cross-venue reference only, **no order book**: ticker (funding rate, mark, index) on all ~510 USDT `PERPETUAL`s (`TRADIFI_PERPETUAL` refused) plus trades on the acting tiers; `crowded` mirrors Bybit's −3 bp / 72 h trigger so the two venues' trades line up. `monthly_gb = 700` against ~480 measured. |
| `liquidity-migration-telegram-controls.service` | Global | Active | Polling authorized chats |
| `liquidity-migration-trade-notify.timer` | Global | Active | 5-minute trade scanning |
| `liquidity-migration-equity-recorder.timer` | Global | Active | 1-minute equity and recorder sampling. `independent`: runs through fleet restarts and funded stops, which is what lets it record them. |

---

## 4. Operational Invariants & Reference Runbooks

* **Runbook & CLI**: See [docs/operations.md](docs/operations.md).
* **Architecture & IPC**: See [docs/architecture.md](docs/architecture.md).
* **Engine Internals**: See [docs/engine.md](docs/engine.md).
* **Data & Tapes**: See [docs/data.md](docs/data.md).
* **Trading Logic**: See [docs/trading_logic.md](docs/trading_logic.md).
* **Observability**: See [docs/observability.md](docs/observability.md).
