# Engine Runtime Specification

Architecture, boot order, risk admission, write-ahead log (WAL), and execution invariants for the native Rust trading engine (`engine`).

---

## 1. Workspace Crate Architecture

The engine workspace is under `engine/`:

| Crate | Binary / Library | Primary Responsibility |
| :--- | :--- | :--- |
| **`engine-types`** | Lib | Core domain contracts: market events, orders, strategies, signals, checkpoints, WAL types. |
| **`engine-wal`** | Lib | Checksummed append-only frame storage, fsync barriers, replay, and segment rotation. |
| **`engine-risk`** | Lib | Account-wide admission, gross/margin limits, quote freshness, and rolling-loss breaker. |
| **`engine-venue`** | Lib | Order gateways, private streams, and the venue registry for all six venues (§2); account lease locking. |
| **`engine-marketdata`**| Lib | Public market feeds and book rebuild per venue: quotes, trades, level-50 books, funding. |
| **`engine-strategies`**| Lib | Pure strategy reducers (`LONG`, `CARRY`, `EXODUS`, `MAKER`) and runtime plugs, plus the `PROBE` order-path plug ([trading_logic.md](trading_logic.md) §1). |
| **`engine-core`** | Lib | Event loop, boot recovery, command execution, controls, heartbeat, and trade reporting. |
| **`engine`** | Binary (`bin`) | Production engine runner, takeover tools, and config renderer. |
| **`signal-worker`** | Binary (`bin`) | Credential-free public market collector and observation streamer. |
| **`market-tape`** | Binary (`bin`) | High-throughput market data capture engine and zstd segment writer. |

---

## 2. Venue Adapters & Readiness

`engine.toml` names one venue; `VenueName::parse` turns that name into the one
adapter triple (gateway, private stream, public feed) so a config cannot
half-switch. Every host any adapter can reach is written in that venue's own
`realm` module and nowhere else — `engine/engine-venue/tests/venue_fence.rs`
reads the crate back and fails the suite if a host appears anywhere else.

### Selectable Realms

Readiness is declared in `VenueName::readiness` and enforced at boot by
`require_engine_run_ready` (`engine/engine-core/src/runner.rs`), before a log,
credential, or socket is opened.

| `engine.toml` venue | Readiness | Engine may run | Adapter lines | State |
| :--- | :--- | :---: | ---: | :--- |
| `bybit_demo` | `live-proven` | yes | 11,570 | Trades. Play money, real matching engine. |
| `bybit_mainnet` | `live-proven` | yes | (same adapter) | Trades. The funded account; also needs `REAL_MONEY` armed on the host. |
| `hyperliquid_testnet` | `testnet-canary` | yes | 4,884 | **Dormant.** Offline conformance green; no live order lifecycle observed. |
| `lighter_testnet` | `testnet-canary` | yes | 5,985 | **Dormant.** Offline conformance green; no live order lifecycle observed. |
| `hyperliquid_mainnet` | `production-blocked` | no | (same adapter) | **Dormant.** Refused at boot. |
| `lighter_mainnet` | `production-blocked` | no | (same adapter) | **Dormant.** Refused at boot. |
| `mexc_mainnet` | `production-blocked` | no | 3,211 | **Dormant.** Refused at boot. |
| `binance_testnet` | `production-blocked` | no | 4,654 | **Dormant.** Refused at boot. |
| `binance_mainnet` | `production-blocked` | no | (same adapter) | **Dormant.** Refused at boot. Binance's public feed *is* live: the second tape recorder and the cross-venue panel read it. |
| `variational_mainnet` | `read-only` | no | 867 | **Dormant.** No trading API in the adapter at all. |

### Invariants

* **Must**: every realm in `VenueName::ALL` be either traded or dormant in
  `engine/engine-venue/tests/dormant_venues.rs`. That test pins which realms
  are dormant, what dormancy means at boot per readiness class, and that every
  dormant gateway, private stream, and realm table is still linked — deleting
  an adapter fails to compile there rather than at somebody's order.
* **Must**: offline request-shape conformance stay green where it exists —
  `engine/engine-venue/tests/hyperliquid_requests.rs`,
  `engine/engine-venue/tests/lighter_requests.rs`, and
  `engine/engine-venue/tests/binance_requests.rs`. MEXC and Variational have
  in-module tests only, and no request-shape suite of their own.
* **Must Never**: a realm move to `live-proven` without reviewed live evidence
  from that exact realm — the smallest permitted order, and its cancel or fill.
  A compiled adapter is not evidence.
* **Must Never**: real capital reach a `production-blocked` or `read-only`
  realm. The boot gate refuses the run; there is no override flag.

Only Bybit is traded. Everything else is a kept option: ~19,600 lines whose
cost is CI time and whose value is that a venue decision is a config change
rather than a quarter of work.

---

## 3. Boot & Recovery Sequence

`engine run --config PATH` runs on a single-threaded Tokio runtime, establishing identity and state before admitting any order risk:

| Phase | Step | Action | Invariants / Constraints |
| :--- | :--- | :--- | :--- |
| **1. Config** | Parse & Hash | Reads TOML config and hashes exact bytes. | Rejects unknown keys (`deny_unknown_fields`). |
| **2. Plugs** | Plugs Bind | Resolves compiled venue and strategy reducers. | Rejects mismatched strategy kinds or counts. |
| **3. WAL** | Replay & Lock | Locks `/var/lib/.../engine.wal` and replays frames. | Rebuilds symbol table and unconsumed events. |
| **4. Lease** | Account Lock | Authenticates account and acquires writer lease. | Lock: `/run/lock/liquidity-migration/bybit-<realm>-*.lock`. |
| **5. Private WS**| Stream Watermark| Connects private WebSocket and awaits ready state. | Blocks if auth fails or private queue is cold. |
| **6. Reconcile**| State Audit | Compares WAL orders, positions, and stops against venue. | Unreconciled / stranger positions latch engine. |
| **7. Checkpoint**| Restore State | Restores sleeve checkpoints, covers, loss window. | Rejects schema, fingerprint, or payload mismatches. |
| **8. Re-plan** | Sleeve Wake | Wakes each reducer once to re-plan restored state. | Schedules timers without emitting premature orders. |
| **9. Inputs** | Feeds Live | Starts market data, signal IPC, and control spool. | Consumes hybrid spool/socket feeds. |

---

## 4. Storage, Paths & Memory Budget

| Resource | Demo Path | Mainnet Path | Quota / Budget |
| :--- | :--- | :--- | :--- |
| **WAL File** | `/var/lib/liquidity-migration-engine/engine.wal` | `/var/lib/liquidity-migration-engine-mainnet/engine.wal` | Rotates at `256 MB` (`wal_rotate_mb`). |
| **Account Lease**| `/run/lock/liquidity-migration/bybit-demo-*.lock` | `/run/lock/liquidity-migration/bybit-mainnet-*.lock` | Single-writer exclusive advisory lock. |
| **Signal IPC** | `/var/lib/liquidity-migration/signals/demo/` | `/var/lib/liquidity-migration/signals/mainnet/` | Disk spool row `<seq:020>-<sha256>.json` is the delivery; a frame on `stream.sock` is the doorbell, sent only when the row is ≤ 16 MiB. Payload ≤ 16 MiB; readers take it as a JSON string or a byte array. A gap in a source's sequence is an `ERROR` line, not an exit. |
| **Control Spool**| `/var/lib/liquidity-migration/control/demo/` | `/var/lib/liquidity-migration/control/mainnet/` | Immutable command files (`0750`). |
| **Heartbeat** | `/var/lib/liquidity-migration-engine/heartbeat.json`| `/var/lib/liquidity-migration-engine-mainnet/heartbeat.json` | Atomic 1-line JSON; max age 30s. |
| **Trade Log** | `/var/lib/liquidity-migration-engine/trades.jsonl` | `/var/lib/liquidity-migration-engine-mainnet/trades.jsonl` | Append-only round-trip closed trades. |

### Memory Scaling Invariant
The decoded in-memory WAL replay consumes approximately **$6\times$ the active segment size**. At the default 256 MB rotation size, replay holds ~1.5 GB in RAM. Systemd service units enforce `MemoryMax=2G`.

### REST History Fetch Ceilings
Bounded acquisition envelopes prevent runaway memory during cold starts:

| History Lane | Maximum Window | Upper Row Ceiling | Notes |
| :--- | :--- | :--- | :--- |
| **LONG Klines** | 180 cold-start days + 48h pad | 4,368 hourly rows | Chunked single-job fetches. |
| **CARRY Replay** | Complete feature window | 4,368 hourly rows | Bound matches full lookback. |
| **Merged Repair** | 1 LONG + 2 CARRY spans | 13,104 hourly rows | Reconnect repair maximum bound. |
| **Funding History**| 1-hour interval minimum | 4,369 rows | Inclusive interval bound. |
| **Whale Positioning**| 30 calendar days | 8,641 5-minute rows | Reduces to at most 30 daily points. |

---

## 5. Machine Configuration & Rendering

The engine configuration file (`engine.toml` / `engine-mainnet.toml`) contains:
* `[engine]`: WAL paths, group flush timing (`1-1000ms`), socket paths, heartbeat interval.
* `[risk]`: Gross capital reference, max leverage, order size bounds, rolling-loss limit.
* `[[strategy]]`: Ordered list of sleeves (`CARRY`, `LONG`, `EXODUS`, `MAKER`).

### Config Rendering Recipe
Configs are generated from registered rules and profiles:
```bash
engine render-native-config \
  --realm demo \
  --signal-config configs/signal-worker.demo.json \
  --long-rule configs/long_native_v12.json \
  --carry-rule configs/lane2_carry_hold_v7.json \
  --exodus-rule configs/lane2_exodus_short_v1.json \
  --operational-config configs/operational.json \
  --long-entries-enabled true \
  --carry-entries-enabled true \
  --exodus-entries-enabled true \
  --template deploy/engine.demo.toml.template \
  --output /tmp/engine.demo.toml
```
`--check` re-renders and verifies byte-for-byte identity against existing configs.

---

## 6. Risk Kernel & Emergency Circuit Breakers

The risk kernel (`engine-risk`) gates every order before it reaches the venue adapter:

| Gate | Check | Rejection Reason | Behavior on Failure |
| :--- | :--- | :--- | :--- |
| **Equity Freshness** | Private stream latency $< 10\text{s}$ | `StaleAccountView` | Blocks new/growing risk; exits allowed. |
| **Quote Freshness** | Top-of-book quote $< 45\text{s}$ old | `StaleQuote` | Blocks new entries; exits allowed. |
| **Capital Gross Cap**| Total notional $\le \text{Gross Cap}$ | `GrossExposureExceeded` | Refuses order size increase. |
| **Single-Sleeve Symbol**| One sleeve owns symbol | `SymbolAlreadyOwned` | Refuses entry until symbol is flat. |
| **Rolling-Loss Trip** | 24h closed net PnL $\le -\text{Loss Limit}$ | `RollingLossTripped` | **Emergency Halt**: All entries blocked. Exits pass. |

### Rolling-Loss Circuit Breaker Invariant
* **Calculation**: Sum of realized PnL minus venue fees over the last 24 hours across engine-closed round trips.
* **Threshold**: $\text{Loss Limit} = \text{max\_rolling\_loss\_fraction} \times \text{capital\_reference}$.
* **Trip Effect**: Blocks all entry and size-increasing orders. Exits and reduction-only orders are always permitted.
* **Reset**: Cannot be cleared manually or by process restart. Clears naturally as losing trades roll past 24 hours of age.

---

## 7. Runtime Controls

Operator controls are dispatched by placing JSON command files into the realm control spool:

| Command Action | CLI Syntax | Effect |
| :--- | :--- | :--- |
| **Set Entry Permission** | `engine set-strategy-entry-permission --config <cfg> --strategy <sleeve> --entries-enabled <true\|false>` | Enables or disables opening orders for a specific sleeve. |
| **Flatten Strategy** | `engine flatten-strategy --config <cfg> --strategy <sleeve> --request-id <uuid>` | Cancels working openings and emits reduction-only exits until flat. |

* **Refusal Handling**: Malformed or semantically invalid command files are moved to `<filename>.rejected` to prevent spool blockage.
* **Idempotency**: Repeated submissions of the exact same request ID are no-ops.

---

## 8. Native State Takeover & State Audit

When performing rollouts or cold starts, state is seeded or verified while units are stopped:

| CLI Subcommand | Purpose | Preconditions |
| :--- | :--- | :--- |
| `initialize-native-strategy-state` | Initializes canonical empty checkpoints in a fresh WAL. | Empty WAL file only. |
| `import-strategy-state` | Ingests verified historical strategy bundles into the WAL. | Requires WAL lock and account match. |
| `verify-native-strategy-state` | Verifies WAL checkpoint identity, frame CRC, and state provenance. | Run before restarting units on deploy. |

### Takeover Source Roles
| Sleeve | Source Format | Named Source Roles |
| :--- | :--- | :--- |
| **LONG** | `long-book-state-v2` | `state` |
| **CARRY** | `carry-sizing-anchors-v1-early-exits-v1-target-book-v1` | `early_exits`, `sizing_anchors`, `target_book` |
| **EXODUS** | `exodus-state-v1-v4-event-tape-v1-identity-v2` | `carry_events`, `identity`, `state` (and generated `legacy_paths`) |

---

## 9. Backtest Replay (`engine backtest`)

The live loop — `Engine::boot_as`, the risk kernel, the strategy reducers, the working-order supervisor, the log — driven by a recorded `market_tape` in the tape's own time, on a simulated venue.

| Input | Source | Contract |
| :--- | :--- | :--- |
| `--tape PATH` | `python -m market_tape rows ARCHIVE --hours A..B > tape.jsonl` (or `.jsonl.zst`) | `market_tape/schema.py` rows, `local_receive_ts_ns` ordered; a malformed row stops the run at its line. Book rows must be Bybit's: another venue's chaining is refused, not guessed |
| `--instruments PATH` | `ARCHIVE/<day>/<HH>/_meta/instruments-<stamp>.json[.zst]` | Bybit `instruments-info` rows; a wanted symbol without rules refuses boot |
| `--config PATH` | engine TOML with `[[strategy]]` blocks | `wal_path`, `trades_path`, spool paths are replaced by the flags |
| `--wal PATH` | new file | Must be absent or empty; every run starts from nothing |
| `--signals DIR` | signal spool | Rows validated as live; delivered at `available_wall_ts_ms` |

| Output | Written by | Holds |
| :--- | :--- | :--- |
| `--wal` | the engine | The run's log, byte-identical across runs of one tape |
| `--trades PATH` | the engine (`ClosedTrade`) | Closed round trips: gross, fees, net, holding time, maker share, shortfall |
| `--equity PATH` | the venue (`EquityPoint`) | Cash, unrealized, equity, initial margin at every fill and settlement |
| `--report PATH` | the runner (`BacktestReport`) | Tape stats, venue books, engine ledger, reconciliation |

| Dial | Default | Meaning |
| :--- | :--- | :--- |
| `--capital` | 10000 | Starting USDT |
| `--taker-fee` / `--maker-fee` | 0.00055 / 0.0002 | Bybit VIP0 linear |
| `--rtt-ms` | 175 | Order command round trip; half each way, matched at arrival |
| `--private-latency-ms` | 60 | Private-stream hop for fills, cancels, amends |
| `--mmr` | 0.005 | Maintenance margin fraction; equity ≤ Σ maintenance liquidates |
| `--durable-log` | off | Wait for the disk at every log barrier as the live engine does. Off, the log reaches the OS and no further: same bytes, no fsync per order |

Invariants:
- Time moves only when the tape feed releases a row or a due wait; nothing later is observed before anything earlier. Two runs of one tape write the same log.
- The virtual clock is thread-local and guard-held (`engine_types::clock::install_virtual`); the live loop's timers are the system's (`SystemTimer`), monomorphised, untouched.
- Fills walk the book level by level; resting orders wait behind the displayed queue; stops trigger on the mark and fill through the gap; funding settles once per published boundary; margin is posted; refusals carry Bybit's codes.
- The venue matches against the deepest book whose chain is intact. Until a deep snapshot lands, the `orderbook.1` stream (a snapshot every row) is the venue's book. An order with no chained book at all is refused, never priced.
- The recorder re-anchors every book topic once per UTC hour, so a tape cut to any hour range opens with a snapshot and needs no warm-up from earlier hours.
- Not modelled: our impact on the tape's liquidity, reactions to us, liquidation fees, rate limits. Every number is bounded by those omissions.
- A flat account whose venue books and engine ledger disagree fails the run.
- Throughput: a 2 h, 8,335-order tape runs in ~2 s; with `--durable-log` the same run pays one fsync per order (~4 ms on a laptop SSD, ~35 s in all) and writes the same bytes.

```bash
# One tape hour range to a flat file, then the replay and its report
python -m market_tape rows ARCHIVE --hours 2026-09-02T00..2026-09-03T00 > tape.jsonl
python scripts/research/run_engine_backtest.py --config engine/engine.demo.toml \
  --tape tape.jsonl --instruments ARCHIVE/2026-09-02/00/_meta/instruments-*.json.zst \
  --out-dir var/backtests/2026-09-02

# The engine alone
cd engine && cargo run --release -- backtest --config CONFIG --tape TAPE \
  --instruments INSTRUMENTS --wal run.wal --trades trades.jsonl --report report.json
```

---

## 10. Development Commands

```bash
# Style formatting check
cargo fmt --manifest-path engine/Cargo.toml --all -- --check

# Strict linting with warnings denied
cargo clippy --manifest-path engine/Cargo.toml --workspace --all-targets -- -D warnings

# Full workspace unit and integration tests
cargo test --manifest-path engine/Cargo.toml --workspace --all-targets
```
