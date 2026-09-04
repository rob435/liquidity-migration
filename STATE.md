# Operational State

Current operational snapshot of the live trading fleet and host environment.

---

## 1. Live Deployment Snapshot

Verified against the running host and Grafana Cloud on 2026-09-04 18:13 UTC;
GitHub Actions state re-verified 19:16 UTC:

| Property | Value | Description |
| :--- | :--- | :--- |
| **Host** | `ip-208-84-103-4.my-advin.com` (`208.84.103.4`) | 4 vCPU, 8 GB RAM, 118 GB disk (72% used, 32 GB free; the watchdog's floor is 25 GB on `/var/lib`). |
| **Deployed Commit** | `65ee75a7` (transactional realm handover, deploy-queue repair, producer health, and persistent partial-shard debounce) | `main` carries newer commits the host does not run, among them `697341e4`, which makes a `degraded` signal-worker page name the transport input that decided it. That fix is **merged and undeployed**: every GitHub Actions run since 18:03 UTC — including this commit's checks (`33910211410`) and its dispatched deploy (`33910262256`) — fails within seconds with no job logs, because the account's payments failed. Deploying it hands over both realms (the fingerprint hashes the whole `engine` tree), so the funded engine restarts; the SSH path `EXPECTED_COMMIT=697341e48fed6f23137860a58be4c5c13e7ae02e scripts/ops.sh deploy` needs no runner. Exact release marker written 17:34:41 UTC 2026-09-04. Run `33900447763` passed CI, Rust, and its verified release artifact, then GitHub refused to start the VPS job because account payments failed. The VPS recovery recipe installed that same artifact and commit without restarting the unchanged funded process. Fresh verification: all engines, workers, recorders, and Telegram controls active; every liveness scope returns `ok`; `systemctl --failed` is empty. Mainnet remains on uninterrupted process commit `218905d4`, with 5 positions, `may_open=true`, no rolling-loss trip, no strategy errors, and no pending flatten. |
| **Rollback Target** | `16d52f88f54ee9bd936b821a36ffeecc7852240a` | Stored in `/opt/liquidity-migration-engine/previous-commit`. |
| **Funded Status** | `real-money armed` | `REAL_MONEY=true`. The engine is running with healthy heartbeats. |
| **Incident Response** | Telegram + Claude Code routine + external dead-man | Demo, mainnet, and independent host watchdogs run every 3 min. A 16:57 UTC live delivery drill returned `telegram accepted`, created no-change session `session_01Jf15281KD9pJvLF32sFJGw`, and returned `dead-man accepted` in 2.9 s. Telegram remains the human pager, trade feed, and constrained control surface; the routine receives no venue credentials and can diagnose or deploy only through `vps-deploy.yml`. Delivery works, but GitHub currently refuses to start that workflow's VPS job because of failed account payments, so autonomous host diagnosis and repair remain externally blocked until billing is fixed. The demo worker paged a second time at ~20:25 UTC on 2026-09-04 (`demo-0922e9f30da3bf98`, same ref, 62 min into a fresh process): the routine narrowed it by elimination to one Bybit frame drought over `mark_max_age_ms` = 30 000 ms and under the socket's 45 s idle tolerance, and the clause that would name it in the page itself ships with undeployed `697341e4`. One open host reading, in [CHANGELOG.md](CHANGELOG.md). Route files are separated and root-owned mode `0600`. [docs/notifications.md](docs/notifications.md). |
| **CI / Deploy Gate** | Private repository; release work is explicit | A 24-hour audit found 90 workflow runs and about 2,023 billed job-minutes: every push repeated Python, Rust, release-build, soak, and benchmark work. Pushes now start no cloud job. Code PRs run Python and Rust gates; docs-only PRs run none. `deploy` alone builds an exact-SHA artifact after Python and Rust gates; `qualify` alone runs release tests, soak, and benchmark; verify, rollback, diagnose, and disarm do not build. New artifacts retain for 2 days; 98 obsolete archives were deleted, leaving the latest deployed, rollback, and live-process archives at 37.1 MiB total. Run `33911407276` proved the build-free `verify` routing, then GitHub refused its sole `vps` job before runner assignment with the failed-payment or spending-limit annotation. Re-checked 21:36 UTC 2026-09-04: `verify` run `33921858031` on `e2345ca4` failed after 4 s with `vps` again the only scheduled job, every other job skipped, and no log content at all (log download returns HTTP 404), so the job never started. Hosted capacity remains externally blocked until GitHub restores it or a separate private Linux runner is registered. The funded VPS is never an Actions runner. |
| **Equity History** | `liquidity-migration-equity-recorder.timer`, every minute | One sample per engine, signal worker, and tape recorder goes first to `/var/lib/liquidity-migration/equity/<kind>-<realm>-<YYYY-MM>.jsonl`; a source with no readable heartbeat is written as `state=absent`. `scripts/ops.sh curve mainnet` reads it on the host. Each run then pushes six samples to Grafana Cloud stack `proudtortoise1017` through `influx-prod-55-prod-gb-south-1`; `/etc/liquidity-migration/observability.env` is `root:liquidity-migration` mode `0640`, and the dedicated access policy grants only `metrics:write`. Verified at 18:12 UTC on 2026-09-04: three consecutive journal rows say `recorded and pushed 6 samples`. Both workers are transport-healthy and fully covered while their bounded CARRY cold fill reports `starting`; Bybit is 16/16 connected and Binance 10/10, with zero reconnects, frame drops, or disk drops and no blocked disk. Dashboard UID `liqmig-fleet` is a 15-panel operator view bound to `grafanacloud-proudtortoise1017-prom`: four live status tiles; separate demo/mainnet equity and open-exposure cards with exact USDT values and independently scaled sparklines; execution activity; p99 order-path latency; current freshness; capacity; and fault state. [docs/observability.md](docs/observability.md). The order path is measured on a clock: the demo `probe` sleeve rests one order at :00/:15/:30/:45 and the sampler at :20 reads the engine's 60-second latency ledger, so each probe is caught exactly once. Readings 16:45 and 17:00 UTC: `end_to_end` p50 11.84 / 11.29 ms — market event to submitted order, which stops at the submit. `wire` (11.74 / 11.20 ms) is the whole venue task and so contains the round trip; `ack` is empty on demo because every `place` in the demo WAL carries no transport stamp, while 317 of 320 mainnet places do and report a round trip of p50 3.74 ms, p99 59.48 ms, worst 429.76 ms. `engine latency --wal` is the authority. |
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
