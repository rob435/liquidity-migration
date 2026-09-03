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
| **Equity Recorder** | Python | Read-only heartbeat and recorder status sampling, one line per minute | None | None | `liquidity-migration-equity-recorder.service` |

### Realm Isolation (Demo vs Mainnet)
Demo and Mainnet realms are strictly segregated across all resources:
* **No Fallback**: Neither realm can access, inherit, or fall back to the other's state, sockets, or credentials.
* **Leases**: Each engine acquires an exclusive single-writer lockfile: `/run/lock/liquidity-migration/bybit-<realm>-user-<uid>.lock`.

---

## 2. Inter-Process Communication (IPC)

The signal worker delivers observations to the engine as immutable spool rows, and rings a Unix domain socket (`AF_UNIX`) so the engine need not wait for its next spool poll.

| Property | Path | Format | Permissions | Ownership |
| :--- | :--- | :--- | :--- | :--- |
| **Spool row** | `/var/lib/liquidity-migration/signals/<realm>/<sequence:020>-<content_sha256>.json` | One `SignalObservation` JSON envelope, renamed into place after `fsync` | `0770` dir | `liquidity-signal-worker:liquidity-migration` |
| **Demo doorbell** | `/var/lib/liquidity-migration/signals/demo/stream.sock` | `[u32 len_le][the row's bytes]` | `0770` | `liquidity-engine-demo:liquidity-migration` |
| **Mainnet doorbell** | `/var/lib/liquidity-migration/signals/mainnet/stream.sock` | `[u32 len_le][the row's bytes]` | `0770` | `liquidity-engine-mainnet:liquidity-migration` |

### Signal Delivery Mechanics

| Step | Who | Does |
| :--- | :--- | :--- |
| 1 | Worker | Writes the row atomically (temp file, `fsync`, rename, directory `fsync`). The row is the delivery. |
| 2 | Worker | Sends the same bytes as one frame down `stream.sock`, one `write`, 200 ms timeout. Best effort: a failed frame changes nothing. |
| 3 | Engine | Reads frames with resumable state, so a frame split across polls of the core's `select!` is still one frame. A client that disconnects mid-frame costs only that frame. |
| 4 | Engine | On a frame, scans the spool: rows with a lower sequence were written before it and go first; the frame waits. Sequences are per source (`<source>.long`, `<source>.carry`). |
| 5 | Engine | Retires a delivered row on the next poll, after the WAL barrier, whichever path it arrived by. The WAL cursor drops a duplicate. |
| 6 | Engine (down) | Nothing is lost: rows accumulate; boot drains them in order before the socket is read. |

* **Must**: every observation exist as a row before any frame names it.
* **Must never**: a read on the socket hold partial-frame state on the future's stack; the core drops that future on every market event.
* A sequence gap (`signal source … has sequence gap`) means a row was deleted from the spool by something other than the engine. It stops the engine. Recovery: [docs/operations.md §8](operations.md#8-incident-recovery-matrix).

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
