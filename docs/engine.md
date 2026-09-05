# Engine Runtime Specification

## Purpose

Architecture, boot order, risk admission, write-ahead log (WAL), and execution invariants for the native Rust trading engine (`engine`).

## Spec Tables

### 1. Workspace Crate Architecture

The engine workspace is under `engine/`:

| Crate | Binary / Library | Primary Responsibility |
| :--- | :--- | :--- |
| **`engine-types`** | Lib | Core domain contracts: market events, orders, strategies, signals, checkpoints, WAL types. |
| **`engine-wal`** | Lib | Checksummed append-only frame storage, fsync barriers, replay, and segment rotation. |
| **`engine-risk`** | Lib | Account-wide admission, gross/margin limits, quote freshness, and rolling-loss breaker. |
| **`engine-public`** | Lib | Nonsecret realm/catalog facts, symbol interning, public REST clients and shared HTTP/TLS transport. |
| **`engine-venue`** | Lib | Order gateways, private streams, and the venue registry for all six venues (§2); account lease locking. |
| **`engine-marketdata`**| Lib | Public market feeds and book rebuild per venue: quotes, trades, level-50 books, funding. |
| **`engine-strategies`**| Lib | Pure strategy reducers (`LONG`, `CARRY`, `EXODUS`, `MAKER`) and runtime plugs, plus the `PROBE` order-path plug ([trading_logic.md](trading_logic.md) §1). |
| **`engine-core`** | Lib | Event loop, boot recovery, command execution, controls, heartbeat, and trade reporting. |
| **`engine`** | Binary (`bin`) | Production engine runner, takeover tools, and config renderer. |
| **`signal-worker`** | Binary (`bin`) | Credential-free public market collector and observation streamer. |
| **`market-tape`** | Binary (`bin`) | High-throughput market data capture engine and zstd segment writer. |


#### `engine-core` Module Map

| Module (repository path) | Owns |
| :--- | :--- |
| `engine/engine-core/src/engine.rs` | `Engine`, the `select!` loop, one handler per loop arm, the `StopReason` |
| `engine/engine-core/src/engine/boot_recovery.rs` | `Engine::boot`, WAL replay into engine state, missed-fill recovery, venue reconciliation at boot |
| `engine/engine-core/src/engine/scheduling.rs` | Strategy wakes, at most 64 due timer callbacks per turn, durable actions, the per-wake drain |
| `engine/engine-core/src/engine/signal_intake.rs` | Durable signal admission: cursor and availability checks, symbol admission across the four id tables, barrier, delivery, acknowledgement |
| `engine/engine-core/src/engine/intent_admission.rs` | `prepare_intent`, `OpeningRefusal` codes, risk verdicts, order minting and placement groups |
| `engine/engine-core/src/engine/venue_completion.rs` | Venue command completions, `VenueTiming` journaling, private-stream updates, stop maintenance |
| `engine/engine-core/src/engine/telemetry.rs` | Heartbeat and closed-trade rows |
| `engine/engine-core/src/engine/free_helpers.rs` | Replay builders and pure helpers the modules above share |
| `engine/engine-core/src/ctx.rs` | `Books` (what strategies read), `StrategyHost` (the plugs and what is held for them), `Ctx`, `Timers` |
| `engine/engine-core/src/effects.rs`, `engine/engine-core/src/engine/strategy_effects.rs` | Ordered durable callback effects, placement identities, effect completion and recovery |
| `engine/engine-core/src/inflight.rs` | The order ledger and registry: what the log says is still out there, and whose it is |
| `engine/engine-core/src/covers.rs` | What each strategy has sent that the account reading has not yet absorbed |
| `engine/engine-core/src/attribution.rs` | Which strategy's fills a venue position came from |
| `engine/engine-core/src/working.rs` | Resting entries being worked at the venue |
| `engine/engine-core/src/reconcile.rs` | The log's exposure and intended stops against the venue's positions |
| `engine/engine-core/src/signal_state.rs` | Accepted input ownership, consumer backpressure, source cursors/gaps/subscriptions and current boot producer frontiers |
| `engine/engine-core/src/signals/` | Signal feeds: validation, availability deadlines, in-process channel, spool, socket doorbell |
| `engine/engine-core/src/venue_runtime.rs` | The venue task that owns blocking venue I/O |
| `engine/engine-core/src/ledger.rs`, `engine/engine-core/src/timing.rs` | Latency segments live, and read back from the log |
| `engine/engine-core/src/execution.rs`, `engine/engine-core/src/trades.rs` | Fill costs and closed round trips |
| `engine/engine-core/src/assembly.rs`, `engine/engine-core/src/runner.rs`, `engine/engine-core/src/config.rs` | Wiring feeds, venue, risk and strategies into one process |
| `engine/engine-core/src/replay.rs`, `engine/engine-core/src/takeover.rs`, `engine/engine-core/src/clear.rs`, `engine/engine-core/src/canary.rs`, `engine/engine-core/src/controls.rs` | Operator commands over a WAL or a live engine |
| `engine/engine-core/src/backtest/` | The live loop on a recorded tape against a simulated venue |
| `engine/engine-core/src/sim/` | `engine sim`: the live loop on a seeded synthetic market with injected venue, private-stream and market-feed faults and process deaths; the end-of-run invariants |

#### Worker ownership

| Module (repository path) | Owns | State boundary |
| --- | --- | --- |
| `engine/signal-worker/src/worker.rs` | Sequenced public inputs, per-event transition handlers, observation creation, durable batch preparation/commit, producer readiness response | `WorkerState` checkpoint schema is unchanged; candidate state is installed after journal/checkpoint commit |
| `engine/signal-worker/src/history.rs` | Row identity and causal coverage operations | Kline coverage replacement revokes an empty frontier; no inferred coverage |
| `engine/signal-worker/src/live.rs` | Cadence, stream events, live publication and health | One runner commits accepted acquisition results |
| `engine/signal-worker/src/live/lanes.rs` | Instrument, ticker, funding, whale, gate and repair completion handlers | `LaneContext` lends only current stream/pending/lane state; failed chunk acknowledgement retains existing retry behavior |
| `engine/signal-worker/src/live/acquisition.rs` | Public requests, bounded source windows and response normalization | Returns fetched inputs; cannot mutate a `LiveRunner` |
| `engine/signal-worker/src/store.rs` | Atomic checkpoints, input journal and spool publication | Readiness metadata is excluded from observation inventory |
| `engine/signal-worker/src/worker.rs::WorkerErrorCategory` | Config/input/state/network/I/O/JSON classification | Only input/network failures are lane-local; display labels are stable |

#### Venue and public ownership

| Module (repository path) | Owns | State boundary |
| --- | --- | --- |
| `engine/engine-public/src/venues/*/realm.rs` | Realm names, hosts, endpoints and credential-variable names | No credential reads or signing |
| `engine/engine-public/src/http.rs`, `engine/engine-public/src/tls.rs` | Common HTTP/TLS transport | No credential loading or signing; transports caller-supplied requests |
| `engine/engine-public/src/symbols.rs` | Append-only ordered symbol catalog and shared map learning | Existing dense ID order, case and overflow behavior remain |
| `engine/engine-public/src/` public catalog/client modules | Lighter/MEXC catalogs and Variational public statistics | `engine-marketdata` depends directly on public ownership |
| `engine/engine-venue/src/realm_credentials.rs` | Explicit credential-read capability for private adapters | Reexported public realms do not expose secret reads without the venue trait |
| `engine/engine-venue/src/wire.rs` | Shared optional-field and ID decoding | Missing/wrong-type optional fields, escaped strings and duplicate-key semantics stay compatible |
| Venue `parse.rs` and `ws.rs` modules | Bybit/Binance acknowledgements and Bybit/Binance/Hyperliquid private-event envelopes | Inner account/order rows outside these envelopes remain dynamic |
| `engine/engine-venue/src/signing.rs`, `engine/engine-venue/src/stream.rs` | HMAC, acknowledgement memory, cancellation and reconnect backoff | Venue authentication, subscriptions, resets and listen-key upkeep remain adapter-owned |
| `engine/engine-venue/src/lease.rs` | Account lease and typed lease note | Kernel lock remains the authority; serializer preserves existing bytes |

---

### 2. Venue Adapters & Readiness

`engine.toml` names one venue; `VenueName::parse` turns that name into the one
adapter triple (gateway, private stream, public feed) so a config cannot
half-switch. Every host an adapter can reach is declared in that venue's
public realm module — `engine/engine-venue/tests/venue/venue_fence.rs`
checks both public and private crate sources for undeclared hosts.

#### Selectable Realms

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

#### Invariants

* **Must**: every realm in `VenueName::ALL` be either traded or dormant in
  `engine/engine-venue/tests/venue/dormant_venues.rs`. That test pins which realms
  are dormant, what dormancy means at boot per readiness class, and that every
  dormant gateway, private stream, and realm table is still linked — deleting
  an adapter fails to compile there rather than at somebody's order.
* **Must**: offline request-shape conformance stay green where it exists —
  `engine/engine-venue/tests/venue/hyperliquid_requests.rs`,
  `engine/engine-venue/tests/venue/lighter_requests.rs`, and
  `engine/engine-venue/tests/venue/binance_requests.rs`. MEXC and Variational have
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

### 3. Boot & Recovery Sequence

`engine run --config PATH` runs on a single-threaded Tokio runtime, establishing identity and state before admitting any order risk:

| Phase | Step | Action | Invariants / Constraints |
| :--- | :--- | :--- | :--- |
| **1. Config** | Parse & Hash | Reads TOML config and hashes exact bytes. | Rejects unknown keys (`deny_unknown_fields`). |
| **2. Plugs** | Plugs Bind | Resolves compiled venue and strategy reducers. | Rejects mismatched strategy kinds or counts. |
| **3. WAL** | Replay & Lock | Locks `/var/lib/.../engine.wal` and replays frames. | Rebuilds symbol table and unconsumed events. |
| **4. Lease** | Account Lock | Authenticates account and acquires writer lease. | Lock: `/run/lock/liquidity-migration/bybit-<realm>-*.lock`. |
| **5. Private WS**| Stream Watermark| Connects private WebSocket and awaits ready state. | Blocks if auth fails or private queue is cold. |
| **6. Reconcile**| State Audit | Compares WAL orders, positions, and stops against venue. | Unreconciled / stranger positions latch engine. |
| **7. Checkpoint**| Restore State | Restores sleeve checkpoints and loss window; starts covers empty. | Rejects schema, fingerprint, or payload mismatches. |
| **8. Effects / Re-plan** | Ordered recovery | Drains unfinished durable strategy effects before boot callbacks and durable input redelivery. | Callbacks can queue effects and timers; admission blocks unready growth. |
| **9. Inputs** | Feeds Live | Starts market data, signal IPC and control spool; begins producer nonce exchange. | Required producer frontiers suspend growth until established and caught up; boot itself does not wait for participation. |

#### Exit classes

A run that ends without being asked returns one `EngineError`. The supervisor restarts the unit on every class; the class says what the restart settles. The log line starts with the prefix.

| Class | Log prefix | Means | After the restart |
| --- | --- | --- | --- |
| `Boot(_)` | `boot:` | This log, config, and venue cannot be started from. | Fails the same way until the input changes. |
| `TaskStopped { task, .. }` | `<task> stopped` | The venue task, a durability writer, or the strategy host ended. `task` names which. | Fresh tasks; the log carries the rest. |
| `TimedOut(_)` | `timed out waiting for` | A bounded wait on durability or a venue reply ran out (`MUTATION_DRAIN_TIMEOUT`, 10 s). | The wait is retried from the log. |
| `Reconcile(_)` | `venue reconciliation needed:` | A halt cancel was not confirmed, so the venue's account and the engine's view may disagree. | Boot phase 6 settles it against the venue. |
| `State(_)` | `state:` | An invariant on the engine's own state failed. | A defect. Report it with the log. |
| `Wal(_)`, `Venue(_)` | `log:`, `venue:` | The log or the venue refused. | As that error says. |

#### Durable state and producer protocol

| Boundary | Implemented contract |
| --- | --- |
| Stateful callbacks | `StrategyTransitionQueued` stores ordered effects and persisted placement IDs. `StrategyEffectCompleted` identifies completed indexes. Stateful order barriers settle before venue dispatch |
| Ordinary callbacks | Order-only callbacks retain optimistic WAL submission; dependent completion waits for the barrier. Reconciliation handles orders missing from a machine-failure WAL |
| Rotation | `segment_base_v3` requires `strategy_effects` and `signal_gaps`. Legacy/v2 records remain readable; v2 still requires gaps. Old readers refuse unknown required records without truncation |
| Accepted inputs | Existing 256-row/64-MiB observation envelope plus one missing-prefix slot; one unfinished ordinary delivery per destination. Allocation estimate excludes collection overhead and historical identities |
| Outcomes | Consumed, explicitly rejected and retained pending are distinct; terminal payload release follows its WAL barrier |
| Readiness request | Schema 1, fresh `boot_nonce`; `input-readiness-request.json` in the signal spool |
| Readiness response | Schema 1, matching nonce and generation-qualified source/destination/`published_through`; `input-readiness-response.json`. Both metadata files are excluded from observation inventory |
| Failure | Missing, malformed or I/O-failed exchange keeps growth suspended and retries. Request-time accepted prefix distinguishes a rewind from observations arriving after the request. Reductions, protective stops and account recovery remain available |
| Limits | Synchronous callbacks are trusted; output allocation and historical source identity are not globally bounded. A declared frontier cannot recover erased legacy history |

---

### 4. Storage, Paths & Memory Budget

| Resource | Demo Path | Mainnet Path | Quota / Budget |
| :--- | :--- | :--- | :--- |
| **WAL File** | `/var/lib/liquidity-migration-engine/engine.wal` | `/var/lib/liquidity-migration-engine-mainnet/engine.wal` | Rotates at `256 MB` (`wal_rotate_mb`). |
| **Account Lease**| `/run/lock/liquidity-migration/bybit-demo-*.lock` | `/run/lock/liquidity-migration/bybit-mainnet-*.lock` | Single-writer exclusive advisory lock. |
| **Signal IPC** | `/var/lib/liquidity-migration/signals/demo/` | `/var/lib/liquidity-migration/signals/mainnet/` | Disk spool row `<seq:020>-<sha256>.json` is the delivery; a frame on `stream.sock` is the doorbell, sent only when the row is ≤ 16 MiB. Payload ≤ 16 MiB; readers take it as a JSON string or a byte array. A gap in a source's sequence is an `ERROR` line, not an exit. |
| **Control Spool**| `/var/lib/liquidity-migration/control/demo/` | `/var/lib/liquidity-migration/control/mainnet/` | Immutable command files (`0750`). |
| **Heartbeat** | `/var/lib/liquidity-migration-engine/heartbeat.json`| `/var/lib/liquidity-migration-engine-mainnet/heartbeat.json` | Atomic 1-line JSON; max age 30s. |
| **Trade Log** | `/var/lib/liquidity-migration-engine/trades.jsonl` | `/var/lib/liquidity-migration-engine-mainnet/trades.jsonl` | Append-only round-trip closed trades. |

#### Memory Scaling Invariant
The decoded in-memory WAL replay consumes approximately **$6\times$ the active segment size**. At the default 256 MB rotation size, replay holds ~1.5 GB in RAM. Systemd service units enforce `MemoryMax=2G`.

#### REST History Fetch Ceilings
Bounded acquisition envelopes prevent runaway memory during cold starts:

| History Lane | Maximum Window | Upper Row Ceiling | Notes |
| :--- | :--- | :--- | :--- |
| **LONG Klines** | 180 cold-start days + 48h pad | 4,368 hourly rows | Chunked single-job fetches. |
| **CARRY Replay** | Complete feature window | 4,368 hourly rows | Bound matches full lookback. |
| **Merged Repair** | 1 LONG + 2 CARRY spans | 13,104 hourly rows | Reconnect repair maximum bound. |
| **Funding History**| 1-hour interval minimum | 4,369 rows | Inclusive interval bound. |
| **Whale Positioning**| 30 calendar days | 8,641 5-minute rows | Reduces to at most 30 daily points. |

---

### 5. Machine Configuration & Rendering

The engine configuration file (`engine.toml` / `engine-mainnet.toml`) contains:
* `[engine]`: WAL paths, group flush timing (`1-1000ms`), socket paths, heartbeat interval.
* `[risk]`: Gross capital reference, max leverage, order size bounds, rolling-loss limit.
* `[[strategy]]`: Ordered list of sleeves (`CARRY`, `LONG`, `EXODUS`, `MAKER`).

#### Config Rendering Recipe
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

### 6. Risk Kernel & Emergency Circuit Breakers

The risk kernel (`engine-risk`) gates every order before it reaches the venue adapter:

| Gate | Check | Rejection Reason | Behavior on Failure |
| :--- | :--- | :--- | :--- |
| **Equity Freshness** | Private stream latency $< 10\text{s}$ | `StaleAccountView` | Blocks new/growing risk; exits allowed. |
| **Quote Freshness** | Top-of-book quote $< 45\text{s}$ old | `StaleQuote` | Blocks new entries; exits allowed. |
| **Capital Gross Cap**| Total notional $\le \text{Gross Cap}$ | `GrossExposureExceeded` | Refuses order size increase. |
| **Single-Sleeve Symbol**| One sleeve owns symbol | `SymbolAlreadyOwned` | Refuses entry until symbol is flat. |
| **Rolling-Loss Trip** | 24h closed net PnL $\le -\text{Loss Limit}$ | `RollingLossTripped` | **Emergency Halt**: All entries blocked. Exits pass. |

#### Rolling-Loss Circuit Breaker Invariant
* **Calculation**: Sum of realized PnL minus venue fees over the last 24 hours across engine-closed round trips.
* **Threshold**: $\text{Loss Limit} = \text{max\_rolling\_loss\_fraction} \times \text{capital\_reference}$.
* **Trip Effect**: Blocks all entry and size-increasing orders. Exits and reduction-only orders are always permitted.
* **Reset**: Cannot be cleared manually or by process restart. Clears naturally as losing trades roll past 24 hours of age.

---

### 7. Runtime Controls

Operator controls are dispatched by placing JSON command files into the realm control spool:

| Command Action | CLI Syntax | Effect |
| :--- | :--- | :--- |
| **Set Entry Permission** | `engine set-strategy-entry-permission --config <cfg> --strategy <sleeve> --entries-enabled <true\|false>` | Enables or disables opening orders for a specific sleeve. |
| **Flatten Strategy** | `engine flatten-strategy --config <cfg> --strategy <sleeve> --request-id <uuid>` | Cancels working openings and emits reduction-only exits until flat. |

* **Refusal Handling**: Malformed or semantically invalid command files are moved to `<filename>.rejected` to prevent spool blockage.
* **Idempotency**: Repeated submissions of the exact same request ID are no-ops.

---

### 8. Native State Takeover & State Audit

When performing rollouts or cold starts, state is seeded or verified while units are stopped:

| CLI Subcommand | Purpose | Preconditions |
| :--- | :--- | :--- |
| `initialize-native-strategy-state` | Initializes canonical empty checkpoints in a fresh WAL. | Empty WAL file only. |
| `import-strategy-state` | Ingests verified historical strategy bundles into the WAL. | Requires WAL lock and account match. |
| `verify-native-strategy-state` | Verifies WAL checkpoint identity, frame CRC, and state provenance. | Run before restarting units on deploy. |

#### Strategy Table Invariants

The WAL's `Names` table maps `StrategyId(i)` to `strategies[i]`, and every
recorded fill is keyed on that id.

* **Must**: a config's strategy list *extend* the logged table — every id the
  log already names keeps the same name in the same position. Appending a
  block is how a running realm gains a sleeve. Both gates take this rule:
  `Engine::boot` (`engine/engine-core/src/engine/boot_recovery.rs`) and
  the takeover's `verify_names` (`engine/engine-core/src/takeover.rs`), which
  fails with `does not preserve the WAL Names prefix`.
* **Must Never**: a rename, a reorder, an insertion before an existing block,
  or a removal reach a realm with a non-empty WAL. Each renumbers an id the
  log's fills are keyed on, handing one sleeve's recorded fills to another.
* An appended sleeve needs no takeover source: it owns no earlier fill, and a
  block whose plug declares no checkpoint contract carries no state to import.

#### Takeover Source Roles
| Sleeve | Source Format | Named Source Roles |
| :--- | :--- | :--- |
| **LONG** | `long-book-state-v2` | `state` |
| **CARRY** | `carry-sizing-anchors-v1-early-exits-v1-target-book-v1` | `early_exits`, `sizing_anchors`, `target_book` |
| **EXODUS** | `exodus-state-v1-v4-event-tape-v1-identity-v2` | `carry_events`, `identity`, `state` (and generated `legacy_paths`) |

---

### 9. Backtest Replay (`engine backtest`)

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

### 10. Deterministic Simulation (`engine sim`)

The live loop on a seeded synthetic market against the backtest's simulated venue, with faults on every boundary the engine has with the world and process deaths at seeded instants. One seed is one run: two runs of one seed write byte-identical logs, so a failing seed reproduces on any machine.

| Flag | Default | Meaning |
| :--- | :--- | :--- |
| `--seed N` | 1 | The seed; `--seeds K` runs `N..N+K` |
| `--seconds S` | 600 | Tape length in virtual seconds; one ticker, one book delta and one print per symbol per second |
| `--symbols M` | 2 | Symbols the quoter trades, from `BTCUSDT`, `ETHUSDT`, `SOLUSDT` |
| `--crashes C` | 1 | Process deaths; the private socket dies with the process and the next boot recovers from the log and the venue's fill history |
| `--faults none\|light\|heavy` | `light` | Per-call fault rates (`engine/engine-core/src/sim/faults.rs`); `light` is one command in fifty going wrong |
| `--twice` | off | Run every seed twice and compare the logs byte for byte |
| `--out DIR`, `--keep` | temp dir, off | Where the tape, config, log and trades go; kept only with `--keep` |
| `--report PATH` | none | The sweep as JSON (`SweepReport`) |

| Injected | Where | What the engine must do |
| :--- | :--- | :--- |
| venue refusal; request lost before the venue; reply lost after it; slow reply; account read failure | `FaultyGateway` | treat the ambiguous send as ambiguous; learn the order's fate before growing |
| private update dropped, duplicated or delayed; socket hiccup | `FaultyOrderFeed` | dedupe by execution id; recover a gap from the venue's fill history |
| market feed hiccup; feed reset | `FaultyMarketFeed` | re-arm; never open against a stale quote |
| process death; engine exit with an error | `harness` | boot from the log; a supervisor restart is modelled by booting again, and more than 8 restarts in one run is a crash loop |

| Check | Holds when |
| :--- | :--- |
| `engine_ran_clean` | no restart loop and no final error |
| `stopped_by_feed_closed` | the loop stopped because the tape ended |
| `positions_agree` | the log's signed exposure per symbol equals the venue's positions |
| `every_fill_journaled` | the venue's execution ids and the log's are the same set |
| `no_orphan_orders` | every order working at the venue is in the engine's in-flight ledger |
| `cash_flow_agrees_when_flat` | with no position open, the log's fills as money equal the venue's realized P&L net of every fee |
| `ledger_agrees_when_flat` | with no position open, the round trips the log closes (as `engine fills` reads them) net to the venue's realized P&L net of closed fees |
| `numbers_finite` | no NaN or infinity in the venue's books or the engine's account view |

The `cfg(test)` build shortens the engine's confirmation windows, so the simulator's own tests run as an integration test against the library as shipped (`engine/engine-core/tests/integration/sim.rs`).

The engine keeps real-clock deadlines beside its virtual-clock waits (`MUTATION_DRAIN_TIMEOUT`, the one-second dispatch and callback deadlines in `engine/engine-core/src/engine/order_dispatch.rs` and `strategy_callbacks.rs`). On a heavily loaded machine one of those can fire inside a simulated run and the two logs of a seed diverge; `--twice` is judged on an idle machine.

---

## Invariants

* **Must**: the engine's own state be ordered maps only. Anything the engine iterates can reach the log, and two runs of one input write one log; a hash seed must never decide the order of two records. `engine sim --twice` is the gate.
* **Must**: a simulator fault wrapper decide before it awaits and park anything it took from the inner feed, so a lost `select!` branch loses nothing.
* **Must Never**: the simulator soften a failing check. A real engine defect is reported with its seed; a simulator gap is fixed in the simulator.
* **Must**: an `engine-core` tokio test start with the clock paused (`#[tokio::test(start_paused = true)]`), so a stop future resolves when the engine is idle and one input gives one interleaving. The wall clock is for tests that drive a real socket or wait on an engine timer or deadline, which read `clock::now_ns`.

- Must preserve one deterministic core as the account/order/risk/durability authority.
- Must retain ordered strategy effects through overload, failure, rotation and restart.
- Must keep required input readiness separate from account recovery, reductions and protective stops.
- Must preserve durable dense strategy/symbol identity and venue reconciliation.
- Must distinguish implemented local behavior from deployed behavior; local checks do not authorize funded changes.

## Operational Recipes

```bash
audit_rust_bin="$(dirname "$(rustup which --toolchain 1.90.0 rustc)")"
export PATH="$audit_rust_bin:$PATH"
export RUSTC="$audit_rust_bin/rustc"
export RUSTDOC="$audit_rust_bin/rustdoc"

# One simulated seed, kept, with its report; then a sweep with the byte-identity check
cargo run --manifest-path engine/Cargo.toml --release -- sim --seed 4 --seconds 300 --crashes 1 --faults light --keep --out /tmp/sim --report /tmp/sim/report.json
cargo run --manifest-path engine/Cargo.toml --release -- sim --seed 100 --seeds 24 --faults light --twice

# Style formatting check
cargo fmt --manifest-path engine/Cargo.toml --all -- --check

# Strict linting with warnings denied
cargo clippy --manifest-path engine/Cargo.toml --workspace --all-targets -- -D warnings

# Full workspace unit and integration tests
cargo test --manifest-path engine/Cargo.toml --workspace --all-targets --locked
cargo test --manifest-path engine/Cargo.toml --workspace --doc --locked
cargo test --manifest-path engine/Cargo.toml --workspace --all-targets --release --locked
cargo test --manifest-path engine/Cargo.toml --workspace --doc --release --locked
scripts/dev.sh check
```
