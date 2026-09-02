# System Architecture

System topology, execution boundaries, inter-process communication, and durability invariants for the liquidity-migration trading platform.

---

## 1. Process Topology & Boundaries

The live trading platform runs natively in Rust. Python is restricted to offline research, backtesting, deployment orchestration, and Telegram notifications.

| Process / Component | Language | Authority | Credentials | State Root | Systemd Unit |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Trading Engine** (`engine`) | Rust | Sole order authority, WAL, risk kernel, position attribution | Venue API keys (`0600`) | `/var/lib/liquidity-migration-engine[-mainnet]` | `liquidity-migration-engine[-mainnet].service` |
| **Signal Worker** (`signal-worker`) | Rust | Public market ingestion, feature calculation, observation streaming | None (public data only) | `/var/lib/liquidity-migration-signal-worker-{demo,mainnet}` | `liquidity-migration-signal-worker-{demo,mainnet}.service` |
| **Market Tape** (`market-tape`) | Rust / Py | Raw tick/book capture, zstd segment compression, manifest logging | None (public WebSocket) | `/var/lib/liquidity-migration/forward-market` | `liquidity-migration-forward-capture[-binance].service` |
| **Observer / Notifier** | Python | Read-only trade logs, Telegram notifications, heartbeat monitoring | Telegram Bot Token | None (ephemeral) | `liquidity-migration-trade-notify.service` |

### Realm Isolation (Demo vs Mainnet)
Demo and Mainnet realms are strictly segregated across all resources:
* **No Fallback**: Neither realm can access, inherit, or fall back to the other's state, sockets, or credentials.
* **Leases**: Each engine acquires an exclusive single-writer lockfile: `/run/lock/liquidity-migration/bybit-<realm>-user-<uid>.lock`.

---

## 2. Inter-Process Communication (IPC)

The signal worker communicates with the engine via a high-throughput Unix Domain Socket (`AF_UNIX`) with automatic disk spool fallback.

| Property | Socket Path | Wire Protocol | Permissions | Ownership |
| :--- | :--- | :--- | :--- | :--- |
| **Demo IPC** | `/var/lib/liquidity-migration/signals/demo/stream.sock` | `[u32 len_le][utf8 json payload]` | `0770` | `liquidity-engine-demo:liquidity-migration` |
| **Mainnet IPC** | `/var/lib/liquidity-migration/signals/mainnet/stream.sock` | `[u32 len_le][utf8 json payload]` | `0770` | `liquidity-engine-mainnet:liquidity-migration` |
| **Spool Fallback** | `/var/lib/liquidity-migration/signals/<realm>/` | Atomic `.json` files (`obs-*.json`) | `0770` | `liquidity-signal-worker:liquidity-migration` |

### Signal Delivery Mechanics
1. **Socket-First**: Worker attempts non-blocking write to `stream.sock` ($< 10\mu s$ latency, zero disk I/O).
2. **Spool Fallback**: If engine is offline/restarting, worker writes atomic `.json` files to the spool directory.
3. **Hybrid Drain**: Upon startup, engine drains queued disk spool files in order before consuming the live socket stream.

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
       v (AF_UNIX streaming socket: stream.sock)
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

The engine hosts four dedicated native strategy sleeves (`engine/engine-strategies/src/`):

| Sleeve Name | Strategy ID | Trigger / Cadence | Core Mandate | Primary State Checkpoint |
| :--- | :--- | :--- | :--- | :--- |
| **`long_native`** | `0` | Hourly feature batch / LLM entry events | Momentum breakouts on top turnover USDT perps | `long-book-state-v2` |
| **`carry_native`** | `1` | Daily score at decision phase (00:00 UTC) | Captures extreme negative funding crowd fees | `carry-sizing-anchors-v1-early-exits-v1-target-book-v1` |
| **`exodus_native`** | `2` | Pre-settlement CARRY events | Short entry, retry, and cover for distressed pairs | `exodus-state-v1-v4-event-tape-v1-identity-v2` |
| **`quoter` (MAKER)** | `3` | Real-time level-50 book & trade ticks | Two-sided liquidity provision around fair mid | Quoter checkpoint |

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

Operator commands are durable engine events submitted through the control spool (`/var/lib/liquidity-migration/control/<realm>/`):

| Action | Entry Allowed | Exit Allowed | Signal Worker State | Reducer Behavior |
| :--- | :--- | :--- | :--- | :--- |
| **Pause** | **No** | Yes | Active | Sets `entry_permission=false`. Cancels working openings. Existing positions hold or exit normally. |
| **Resume** | **Yes** | Yes | Active | Restores entry permission (only if committed config allows entries). |
| **Flatten** | **No** | **Forced** | Active | Cancels all working orders. Emits reduction-only market/limit exits until attributed exposure is zero. |
| **Disarm** | **No** | No orders | Stopped | Sets `REAL_MONEY=false` in `/etc/liquidity-migration/bybit-mainnet.env` and stops engine. |

---

## 7. Trade Diagnostics & Markouts

Every order fill is attributed to its originating strategy and evaluated against microstructural anchors:

| Metric | Code Symbol | Definition |
| :--- | :--- | :--- |
| **Arrival Midpoint** | `M0` / `arrival_mid` | Midpoint of the order book at the exact millisecond the order left the socket. |
| **Fill Price** | `fill_px` | Volume-weighted execution price of the fill. |
| **Slippage** | `slippage_bp` | Realized execution deviation: $\text{Slippage} = \frac{\text{Fill} - M_0}{M_0} \times 10{,}000$ (basis points, signed by trade direction). |
| **Post-Trade Markouts** | `markout_<1s|15s|60s|300s>` | Book midpoint at fixed intervals post-fill: measures adverse selection and trade toxicity. |
