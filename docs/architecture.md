# System Architecture

System topology, execution boundaries, inter-process communication, and durability invariants for the liquidity-migration trading platform.

---

## 1. Process Topology & Boundaries

The execution engine and signal worker run in Rust; Python runs market-tape capture, research, deployment orchestration and notifications.

| Process / Component | Language | Authority | Credentials | State Root | Systemd Unit |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Trading Engine** (`engine`) | Rust | Sole order authority, WAL, risk kernel, position attribution | One venue's API keys (`0600`) | `/var/lib/liquidity-migration-engine[-mainnet\|-mexc\|-hyperliquid]` | `liquidity-migration-engine[-mainnet\|-mexc\|-hyperliquid].service` |
| **Signal Worker** (`signal-worker`) | Rust | Public market ingestion, feature calculation, observation streaming | None (public data only) | `/var/lib/liquidity-migration-signal-worker-{demo,mainnet,mexc,hyperliquid}` | `liquidity-migration-signal-worker-{demo,mainnet,mexc,hyperliquid}.service` |
| **Market Tape** (`python -m market_tape`) | Python | Raw tick/book capture, zstd segment compression, manifest logging | None (public WebSocket) | `/var/lib/liquidity-migration/forward-market` | `liquidity-migration-forward-capture[-binance].service` |
| **Observer / Notifier** | Python | Read-only trade logs, Telegram notifications, heartbeat monitoring | Telegram Bot Token | None (ephemeral) | `liquidity-migration-trade-notify.service` |
| **Equity Recorder** | Python | Read-only heartbeat and recorder status sampling, one line per minute | None | None | `liquidity-migration-equity-recorder.service` |

### Realm Isolation
Every account-owning realm is strictly segregated across all resources. The realms, and every unit, user, path and env file derived from them, come from [`deploy/realms.tsv`](../deploy/realms.tsv); see [operations.md](operations.md) §Realm table.

| Fleet realm | Engine venue name | Heartbeat venue / realm | Lease file |
| :--- | :--- | :--- | :--- |
| `demo` | `bybit_demo` | `bybit` / `demo` | `/run/lock/liquidity-migration/bybit-demo-user-<uid>.lock` |
| `mainnet` | `bybit_mainnet` | `bybit` / `mainnet` | `/run/lock/liquidity-migration/bybit-mainnet-user-<uid>.lock` |
| `mexc` | `mexc_mainnet` | `mexc` / `mexc_mainnet` | `/run/lock/liquidity-migration/mexc-mexc_mainnet-user-key-<16 hex>.lock` |
| `hyperliquid` | `hyperliquid_mainnet` | `hyperliquid` / `hyperliquid_mainnet` | `/run/lock/liquidity-migration/hyperliquid-hyperliquid_mainnet-user-0x<40 hex>.lock` |

* **No Fallback**: No realm can access, inherit, or fall back to another's state, sockets, or credentials.
* **Leases**: Each engine acquires an exclusive single-writer lockfile named `/run/lock/liquidity-migration/<venue>-<realm>-user-<id>.lock`. Venues added after Bybit qualify the realm with the venue name. MEXC exposes no numeric account id, so its id is `key-` plus the first eight bytes of `sha256(api key)` in hex; Hyperliquid's is the master account address in lower case, and the engine checks the signing key is one of that account's `extraAgents` before it trades.
* **Public data**: each realm's worker reads the public data of the venue its `sources.public_venue` names (`configs/signal-worker.<realm>.json`): Bybit mainnet for `demo` and `mainnet`, MEXC for `mexc`, Hyperliquid for `hyperliquid`. Funding, settlement clock, klines, tickers and instruments come from that venue natively through `engine/signal-worker/src/venue/`; the Binance top-trader ratio and the LLM gate file are shared by every realm. A realm's checkpoint key folds the venue in, so switching venue cold-starts its features.

---

## 2. Inter-Process Communication (IPC)

The signal worker delivers observations to the engine as immutable spool rows, and rings a Unix domain socket (`AF_UNIX`) so the engine need not wait for its next spool poll.

| Property | Path | Format | Permissions | Ownership |
| :--- | :--- | :--- | :--- | :--- |
| **Spool row** | `/var/lib/liquidity-migration/signals/<realm>/<sequence:020>-<content_sha256>.json` | One `SignalObservation` JSON envelope, renamed into place after `fsync` | `0770` dir | `liquidity-signal-worker:liquidity-migration` |
| **Quarantined candidate** | `/var/lib/liquidity-migration/signals/<realm>/quarantine/<original file name>` | The candidate byte for byte, renamed out of the scan path, beside `<original file name>.reason`: JSON `{reason, bytes, modified_wall_ts_ms, quarantined_wall_ts_ms, original_path}` | `0770` dir | `liquidity-signal-worker:liquidity-migration` |
| **Demo doorbell** | `/var/lib/liquidity-migration/signals/demo/stream.sock` | `[u32 len_le][the row's bytes]` | `0770` | `liquidity-engine-demo:liquidity-migration` |
| **Mainnet doorbell** | `/var/lib/liquidity-migration/signals/mainnet/stream.sock` | `[u32 len_le][the row's bytes]` | `0770` | `liquidity-engine-mainnet:liquidity-migration` |
| **MEXC doorbell** | `/var/lib/liquidity-migration/signals/mexc/stream.sock` | `[u32 len_le][the row's bytes]` | `0770` | `liquidity-engine-mexc:liquidity-migration` |
| **Hyperliquid doorbell** | `/var/lib/liquidity-migration/signals/hyperliquid/stream.sock` | `[u32 len_le][the row's bytes]` | `0770` | `liquidity-engine-hyperliquid:liquidity-migration` |

### Signal Delivery Mechanics

| Step | Who | Does |
| :--- | :--- | :--- |
| 1 | Worker | Writes the row atomically (temp file, `fsync`, rename, directory `fsync`). The row is the delivery. |
| 2 | Worker | Sends the same bytes as one frame down `stream.sock`, one `write`, 200 ms timeout. Best effort: a failed frame changes nothing. |
| 3 | Engine | Reads bounded socket chunks as wake notifications; only the immutable spool parser delivers observations. Socket-only payloads cannot advance a cursor. |
| 4 | Engine | Scans in bounded pages and prioritizes requested missing sequences. A gap writes and barriers `SignalGapRecorded`; its later row stays on disk. |
| 5 | Engine | Appends and barriers each contiguous observation before reducer delivery, then explicitly acknowledges it. The next poll completes deletion; cancelled reads/deletions retain resumable state. |
| 6 | Engine | Blocks affected strategies and declared input dependents from opening/amending exposure, cancels their resting entries, and withholds their other source/generation inputs until catch-up. Exact missing-prefix rows, independent destinations, private updates and protective actions remain serviceable. |
| 7 | Engine (restart) | Replays accepted cursors, routes and gaps from WAL; `segment_base_v2` retains these across rotation. Deferred payloads remain in the spool. |

* **Must**: every observation exist as a row before any frame names it.
* **Must never** retire an unacknowledged spool row or advance a cursor across a known gap.
* **Must never** delete anything under `quarantine/`: the worker only adds there, and an operator removes the pair by hand after [signal recovery](operations.md#signal-prefix-recovery).
* **Must** preserve the complete source/generation identity and its strategy destination; changing generation cannot clear an older known gap.
* **Must** retain the spool together with WAL during recovery. WAL retains gap metadata and accepted observations; it does not copy deferred payloads. See [signal recovery](operations.md#signal-prefix-recovery).
* **Must** treat legacy accepted cursors as an evidence boundary: history already skipped by an older engine cannot be reconstructed from a cursor.

---

## 3. Runtime Data Flow

```text
Public Market Data (Bybit & Binance WS / REST)
       |
       v
Rust Signal Worker
  ├── Hourly Klines & Tickers
  ├── Settled Funding & Whale Flow
  └── Universe Selection (Top 30/120 Turnover)
       |
       v  immutable spool row (the delivery)
       |  + AF_UNIX frame on stream.sock (the wake, best effort)
Rust Execution Engine
  ├── 1. Append observation to checksummed WAL (Durable Barrier)
  ├── 2. Pure Strategy Reducer Step (LONG, CARRY, EXODUS, MAKER)
  ├── 3. Risk Kernel Admission (Margin, Gross Cap, Quote Freshness)
  └── 4. Venue Order Execution (Bybit Private WebSocket)
       |
       +---> Engine WAL / Trade Log (trades.jsonl)
       +---> Heartbeat (heartbeat.json)
```

---

## 4. Strategy Sleeve Registry

A strategy's ID is its block's position in that realm's `engine.toml`, so IDs are per realm and append-only: every new block appends, nothing is inserted, and each realm's tail differs.

The sleeves, their IDs per realm, and their mandates: [trading_logic.md](trading_logic.md) §1.

---

## 5. Durability & Ordering Invariants

1. **WAL Barrier Precedes Wire**: An order request is written and synced to the WAL *before* the order bytes leave the network socket. A process crash can never forget an in-flight order.
2. **Event Sourcing Sequence**:
   - `Observation` appended to WAL $\to$ Reducer executes $\to$ Strategy emits target state / order request.
   - Cross-sleeve events (e.g. CARRY $\to$ Exodus) are durable in WAL before the receiving sleeve consumes them.
3. **Pure Reducer Separation**: Reducers have zero I/O, network, credential, or wall-clock access. All external state is supplied by the engine plug layer.
4. **Idempotent Recovery**: Duplicate observations, fills, or operator commands are deduplicated by stable UUID / sequence ID.

---

## 6. Operator Controls & Safety Stops

Operator commands are durable engine events submitted through the control spool (`/var/lib/liquidity-migration/controls/<realm>/`):

| Action | Entry Allowed | Exit Allowed | Signal Worker State | Reducer Behavior |
| :--- | :--- | :--- | :--- | :--- |
| **Pause** | **No** | Yes | Active | Sets `entry_permission=false`. Cancels working openings. Existing positions hold or exit normally. |
| **Resume** | **Yes** | Yes | Active | Restores entry permission (only if committed config allows entries). |
| **Flatten** | **No** | **Forced** | Active | Cancels all working orders. Emits reduction-only market/limit exits until attributed exposure is zero. |
| **Disarm** | **No** | No orders | Stopped | Sets `REAL_MONEY=false` in that realm's credential file (`bybit-mainnet.env`, `mexc-mainnet.env` or `hyperliquid-mainnet.env`) and stops its units. |

---

## 7. Trade Diagnostics & Markouts

Every order fill is attributed to its originating strategy and evaluated against microstructural anchors:

| Metric | Code Symbol | Definition |
| :--- | :--- | :--- |
| **Arrival Midpoint** | `M0` / `arrival_mid` | Midpoint of the order book at the exact millisecond the order left the socket. |
| **Fill Price** | `fill_px` | Volume-weighted execution price of the fill. |
| **Slippage** | `slippage_bp` | Realized execution deviation: $\text{Slippage} = \frac{\text{Fill} - M_0}{M_0} \times 10{,}000$ (basis points, signed by trade direction). |
| **Post-Trade Markouts** | `markout_<1s|15s|60s|300s>` | Book midpoint at fixed intervals post-fill: measures adverse selection and trade toxicity. |
