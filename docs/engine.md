# Engine Runtime Specification

## Purpose

This specification defines the native Rust engine’s architecture, boot order, risk admission, write-ahead log (WAL), and execution invariants.

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
| **`engine-tools`** | Lib and two binaries | `engine` runs the core and embedded strategy callbacks; `engine-tools` owns simulation, backtest, benchmark, takeover, canary, configuration, equity sampling and reports. Both ship together. |
| **`signal-worker`** | Binary (`bin`) | Credential-free public market collector and observation streamer. |


#### `engine-core` Module Map

| Module (repository path) | Owns |
| :--- | :--- |
| `engine/engine-core/src/engine.rs` | `Engine`, the priority loop, rotating ordinary inputs, one handler per turn, the `StopReason` |
| `engine/engine-core/src/engine/boot_recovery.rs` | `Engine::boot_as_exact`, WAL replay into engine state, missed-fill recovery, venue reconciliation at boot; optional scalar boot is test-only |
| `engine/engine-core/src/engine/scheduling.rs` | Strategy wakes, at most 64 due timer callbacks per turn, durable actions, the per-wake drain |
| `engine/engine-core/src/engine/signal_intake.rs` | Durable signal admission: cursor and availability checks, symbol admission across the four id tables, barrier, delivery, acknowledgement |
| `engine/engine-core/src/engine/intent_admission.rs` | `prepare_intent`, `OpeningRefusal` codes, risk verdicts, order minting and placement groups |
| `engine/engine-core/src/engine/venue_completion.rs` | Venue command completions, `VenueTiming` journaling, private-stream updates, stop maintenance |
| `engine/engine-core/src/engine/telemetry.rs` | Heartbeat and closed-trade rows |
| `engine/engine-core/src/engine/free_helpers.rs` | Replay builders and pure helpers the modules above share |
| `engine/engine-core/src/ctx.rs` | `Books` (what strategies read), `StrategyHost` (the plugs and what is held for them), `Ctx`, `Timers` |
| `engine/engine-core/src/effects.rs`, `engine/engine-core/src/engine/strategy_effects.rs` | Ordered durable callback effects, placement identities, effect completion and recovery |
| `engine/engine-core/src/inflight.rs` | Canonical live and retained terminal orders, exact remaining quantities, ambiguous amendment price ranges and sleeve ownership |
| `engine/engine-core/src/engine/order_lineage.rs`, `engine/engine-wal/src/order_lineage.rs` | Bounded terminal cache, asynchronous archive reads and durable activation before late fills |
| `engine/engine-core/src/portfolio_control.rs`, `engine/engine-core/src/engine/portfolio_runtime.rs` | Durable sleeve exit targets, aggregate emergency phases and internal offset settlement |
| `engine/engine-core/src/identities.rs`, `engine/engine-core/src/engine/symbol_admission.rs` | Stable sleeve/instrument identity, durable dense slots and exact instrument catalog installation |
| `engine/engine-core/src/callback_recovery/`, `engine/engine-core/src/engine/strategy_callbacks.rs` | Retained callback paging and migration; embedded callbacks share the live core path |
| `engine/engine-types/src/execution_history.rs`, `engine/engine-core/src/engine/history_recovery.rs` | Disk-sorted execution windows and incremental canonical recovery |
| `engine/engine-core/src/covers.rs` | What each strategy has sent that the account reading has not yet absorbed |
| `engine/engine-core/src/attribution.rs` | Exact virtual sleeve quantities, cost basis, stops and asset-denominated accounting; same-symbol sleeves may share or oppose |
| `engine/engine-core/src/working.rs` | Resting entries being worked at the venue |
| `engine/engine-core/src/reconcile.rs` | The log's exposure and intended stops against the venue's positions |
| `engine/engine-core/src/signal_state.rs` | Accepted input ownership, consumer backpressure, source cursors/gaps/subscriptions and current boot producer frontiers |
| `engine/engine-core/src/signals/` | Signal feeds: validation, availability deadlines, in-process channel, spool, socket doorbell |
| `engine/engine-core/src/venue_runtime.rs` | The venue task that owns blocking venue I/O: one gateway call in flight; the next is chosen among the commands the venue's request quota would take now (`VenueGateway::quota_wait`), then by `DispatchClass` (risk-reducing > amend > opening > administration, FIFO within a class, a cancel or amend waits only behind its own order's still-queued send). When every selectable command is over quota the task parks for the shortest of their waits, preempted by whatever arrives meanwhile; a held opening or amend whose `CommandAuthority` epoch or TTL has already lapsed is answered `never sent` at once instead of being held, and one that lapses during the hold is answered `never sent` when the hold ends, before any gateway call. Once the engine drops the command channel the task drains what it holds without parking and the adapter's own pacer serves the wait inside the call |
| `engine/engine-core/src/ledger.rs`, `engine/engine-tools/src/timing.rs` | Latency segments live, and read back from the log |
| `engine/engine-core/src/execution.rs`, `engine/engine-core/src/trades.rs` | Fill costs and closed round trips |
| `engine/engine-tools/src/cohort.rs` | `engine cohort`: every opportunity the log holds and where it stopped. Two lanes — source rows keyed by `(destination, source, sequence, observation_id)` and settled by `signal_observation_consumed`/`_rejected`; order decisions keyed by the `intent` record and settled by its `verdict`, an `order_sent_v2`, a `never sent:` reject or an `intent_refused` record. Each lane splits into admitted, rejected, expired and unresolved, grouped by `intent_refused.code` and `DenyReason::code`; every record is counted once and the totals are printed. `intent.cause` joins the lanes: a decision woken by a signal names its row's id, so the `rows → intents → allowed → wire → filled` funnel runs per source and in total, and every other decision is counted under what woke it (`timer`, `order`, `none`, …). Measurable ages: source `observed_wall_ts_ms` to the acknowledgement's `wall_ts_ms` and to `intent.cause.callback_wall_ms` (two processes' realtime clocks), the acknowledgement to that same callback stamp (one realtime clock read twice), and `order_sent_v2.dispatch.intent.decided_ns` to `wire_ns` (one monotonic clock); `decided_ns` is never subtracted from a wall stamp, and a decision carrying no such stamp is counted in `without_a_stamp`. A log with the old free-text refusal notes and no `intent_refused` record counts its refusals unresolved and says so. Coalesced and unlisted-name drops happen in the worker and are named in the footer, never counted here |
| `engine/engine-core/src/assembly.rs`, `engine/engine-core/src/runner.rs`, `engine/engine-core/src/config.rs` | Wiring feeds, venue, risk and strategies into one process |
| `engine/engine-core/src/replay.rs`, `engine/engine-tools/src/takeover.rs`, `engine/engine-core/src/clear.rs`, `engine/engine-tools/src/canary.rs`, `engine/engine-core/src/controls.rs` | Operator commands over a WAL or a live engine |
| `engine/engine-tools/src/backtest/` | Same core/reducers on a virtual timeline; recorder or normalized historical sources and explicitly selected execution assumptions |
| `backtest/source.rs`, `tape.rs`, `instruments.rs` | Normalized event contract, recorder-specific decoding/book reconstruction and exact fixed metadata; source decoding is independent of delivery |
| `backtest/feed.rs`, `execution.rs`, `venue.rs` | Chronological delivery; observed-book or declared trade/bar execution; shared venue accounting. [Input schemas and runnable examples](data.md#historical-adapters-and-execution) |
| `engine/engine-tools/src/sim/` | `engine sim`: the live loop on a seeded synthetic market with injected venue, private-stream and market-feed faults and process deaths; the end-of-run invariants |
| `engine/engine-tools/src/engine.rs`, `engine/engine-tools/src/main.rs`, `engine/engine-tools/src/cli.rs` | Lean runtime executable plus companion operator CLI; existing `engine COMMAND` calls execute the companion |
| `engine/engine-tools/src/bench.rs` | Real-clock core run with a registered embedded strategy, durable WAL and synthetic venue; reports workload and sample scope |
| `engine/engine-tools/src/equity_recorder.rs` | Minute fleet observations, monthly equity JSONL, optional metrics push and recorded curve display; [observability contract](observability.md) |

#### Log reader CLI contract

| Property | Contract |
| --- | --- |
| Unknown arguments | Every `engine-tools` subcommand refuses an argument it does not know, naming it, instead of ignoring it |
| `bench --json` | Prints the benchmark result as JSON on stdout |
| `latency --wal` | Streams the family through `engine_wal::replay_chain_visit`; it does not hold every record |
| `fills` / `latency` / `cohort` torn flag | A trusted segment ended part-way through a record. That segment is not necessarily the newest one in the family, and records after that point are outside the numbers |

#### Worker ownership

| Module (repository path) | Owns | State boundary |
| --- | --- | --- |
| `engine/signal-worker/src/worker.rs` | Sequenced public inputs, per-event transition handlers, observation creation, durable batch preparation/commit, producer readiness response | `WorkerState` checkpoint schema is unchanged; candidate state is installed after journal/checkpoint commit |
| `engine/signal-worker/src/history.rs` | Row identity and causal coverage operations | Kline coverage replacement revokes an empty frontier; no inferred coverage |
| `engine/signal-worker/src/universe.rs` | Tradable domain, turnover ranking with enter/leave hysteresis, and the optional `universe.listed_on` filter to what the engine's own venue lists | An unknown `listed_on` value is a config refusal at load; a rule that names one derives no universe until that venue's listing arrives, and the last good listing stands through a failed fetch |
| `engine/signal-worker/src/live.rs` | Cadence, stream events, live publication and health | One runner commits accepted acquisition results |
| `engine/signal-worker/src/live/lanes.rs` | Instrument, ticker, funding, whale, gate and repair completion handlers | `LaneContext` lends only current stream/pending/lane state; failed chunk acknowledgement retains existing retry behavior |
| `engine/signal-worker/src/live/acquisition.rs` | Lane spawning, the optional `listed_on` listing sources, the LLM gate read, whale and repair fetch loops | Returns fetched inputs; cannot mutate a `LiveRunner` |
| `engine/signal-worker/src/venue/mod.rs`, `venue/stream.rs` | `PublicVenueKind`, the `PublicVenue` (REST) and `PublicStream` (socket) traits, `open_public_venue`, the source wire contract every venue fills (Bybit-shaped rows: ms timestamps, kline `[start, o, h, l, c, volume_base, turnover_quote]`, funding `{settlement ms on the hour, rate as the venue states it, interval hours}`) and the grid validators | Selected by `sources.public_venue` (default `bybit`); the journal, `WireEvent` kinds and heartbeat field names are unchanged for every venue |
| `engine/signal-worker/src/venue/bybit.rs`, `venue/bybit/stream.rs` | Bybit v5 REST fetchers and the `tickers.*`/`kline.60.*` socket | Logic-identical to the pre-seam code; demo and mainnet checkpoint keys unchanged |
| `engine/signal-worker/src/venue/mexc.rs`, `venue/mexc/stream.rs` | MEXC contract table, tickers, klines (seconds → ms, contracts → base by `contractSize`, `real*` columns), funding history with per-contract `collectCycle`, the `edge` socket | `code 510` is a retryable network error; a field MEXC does not state is left absent |
| `engine/signal-worker/src/venue/hyperliquid.rs`, `venue/hyperliquid/stream.rs` | `meta`/`metaAndAssetCtxs`, `candleSnapshot` (turnover approximated as volume × mean bar price), `fundingHistory` floored to the hour with interval 1, listing age from the first daily candle, the `activeAssetCtx`/`candle` socket | Settle coin `USDC` is a venue fact (`PublicVenue::settle_coin`); the rate is the hourly one and is never rescaled |
| `engine/signal-worker/src/store.rs` | Atomic checkpoints, input journal and spool publication | Readiness metadata is excluded from observation inventory. The inventory reads only each spooled row's `kind`: a row whose envelope names a `kind` counts toward that class even when its body no longer parses, and oldest-first class trimming is what removes it; a row with no readable `kind` still fails the inventory |
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
| Venue account, order and execution parsers; `engine/engine-venue/src/account_numbers.rs` | Typed acknowledgements, native IDs, account amounts, positions and private executions | Canonical decimal values retain provenance; malformed types or incompatible projections are refused before account/order authority changes |
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

Readiness is derived from the capability matrix below by
`VenueName::readiness` and enforced at boot by `require_engine_run_ready`
(`engine/engine-core/src/runner.rs`), before a log, credential, or socket is
opened.

| `engine.toml` venue | Code readiness | Engine run permitted | Boot contract |
| :--- | :--- | :---: | :--- |
| `bybit_demo` | `live-proven` | yes | Demo realm credentials and account lease. |
| `bybit_mainnet` | `live-proven` | yes | Mainnet realm credentials, account lease and `REAL_MONEY` arming. |
| `hyperliquid_testnet` | `testnet-canary` | yes | Testnet realm only. |
| `lighter_testnet` | `testnet-canary` | yes | Testnet realm only. |
| `mexc_mainnet` | `live-canary` | yes, as the owner's forward test | Funded realm credentials, account lease and `REAL_MONEY` arming; the boot log names the unproven capabilities. Evidence boundary: the 2026-09-10 11:59 UTC canary (venue order `853000482766018560`) observed submit, cancel and post-only on the current adapter. No fill, stop place or trigger, or reconnect recovery has been observed. |
| `hyperliquid_mainnet` | `live-canary` | yes, as the owner's forward test | Funded realm credentials, account lease and `REAL_MONEY` arming; the boot log names the unproven capabilities. Evidence boundary: the 2026-09-10 11:59 UTC canary (venue order `541177774027`) observed submit, cancel and post-only on the funded address. `hyperliquid_testnet` is a different chain and a different account, so its evidence does not carry. |
| `lighter_mainnet` | `production-blocked` | no | Refused before credential or socket access. |
| `binance_testnet` | `production-blocked` | no | Refused before credential or socket access. |
| `binance_mainnet` | `production-blocked` | no | Private engine run refused; public market clients are separate. |
| `variational_mainnet` | `read-only` | no | No trading API in the adapter. |

Readiness labels are code policy; this table does not establish current deployment or live venue qualification.

| Cargo feature | Default build | Contract |
| --- | --- | --- |
| `bybit`, `mexc`, `hyperliquid` | Enabled | Public, private, market-data and runtime crates forward these features |
| `binance`, `lighter`, `variational` | Disabled | Selecting a disabled adapter fails before credentials or sockets; per-feature CI builds and conformance qualify enabled adapters |
| `hyperliquid` cryptography | In the default build | `k256` and `sha3` are optional dependencies of the `hyperliquid` feature, and that feature is on by default, so the funded binary links them. No CI step asserts their absence |

#### Operator tooling per realm

| Command | Realms it accepts | Contract |
| --- | --- | --- |
| `engine canary-order` | `bybit_demo`, and every `live-canary` realm (`VenueName::require_canary_ready`) — `mexc_mainnet` and `hyperliquid_mainnet` today | One minimum-lot post-only order away from the touch, cancelled, with two flat account scans; any fill is closed in full and fails the command. Client ids are at most 30 characters, inside MEXC's 32-character `externalOid`; a Hyperliquid id takes the hashed half of the 16-byte `cloid` scheme (prefix `0x02`) and still looks itself up. The order is sized by the notional term, not the lot, wherever the venue states a minimum notional — 10 USD on Hyperliquid. Venue clock: `/v5/market/time` on Bybit, `/api/v1/contract/ping` on MEXC, `/info {"type": "exchangeStatus"}` → `time` on Hyperliquid. Terminal proof: Bybit's order receipt, otherwise `VenueGateway::order_status`; a Hyperliquid order the venue has already dropped from its retained set answers `unknownOid`, which reaches the canary as an error, not as never-accepted. |
| `engine verify-account-identity`, `engine attest-flat` | Realms with an `InventoryProbe`: `bybit_demo`, `bybit_mainnet`, `mexc_mainnet`, `hyperliquid_mainnet` | Read-only credentials, no order/cancel/amend/stop API on the probe type. MEXC's `AccountIdentity.user_id` is `key-<first 8 bytes of sha256(api key)>`; its scan covers futures balances, positions, working orders and position-bound stop records. Hyperliquid's is the master account address, lower-case `0x` and 40 hex digits, and its scope reads `credential account: Hyperliquid — every open perpetual position and the cross-margin account value, every working order including the reduce-only trigger orders a stop is kept as, and every spot token balance. Vaults and sub-accounts are separate addresses this scan does not read.` Each says so in `AccountInventory.scope`. |

| Private conformance scope | Verified fixture behavior |
| --- | --- |
| Bybit, Hyperliquid and MEXC | Reconnect emits StreamReset before the next fill. REST history restores a 0.002 execution omitted during disconnect; overlapping private/history IDs retain identical exact quantities and the complete fixture totals 0.01. MEXC additionally pins the login frame (`apiKey` + `reqTime` signature, `subscribe:false`), the `personal.filter` frame, the 15 s keep-alive, and the contract-count conversion; a refused login keeps the 5 s paced StreamReset running while the socket retries. |
| Binance | Reconnect emits StreamReset. Complete account-wide execution recovery explicitly refuses; the engine-run readiness policy remains blocked. |
| Lighter | Paced StreamReset events request account/history reconciliation; the fixture verifies the resync interval. |
| Variational | Private updates remain silent; unsupported mutations refuse before HTTP. |
| Sequence interpretation | The Bybit `seq` field associates fills with position updates and can repeat across transactions and symbols; it is not treated as a per-account contiguous counter. The gap fixture uses a disconnect and a known omitted execution. [Bybit execution schema](https://bybit-exchange.github.io/docs/v5/websocket/private/execution). |
| Evidence boundary | Constructed local HTTP/WebSocket fixtures exercise adapter contracts. They do not establish live-account completeness or promote a dormant realm. |


#### Capability matrix

`VenueName::capability` holds one row per realm, and `VenueName::readiness`
derives the label above from it — no realm is assigned a readiness by hand.
A realm is `live-proven` when, and only when, all six capabilities in
`UNATTENDED_PROTECTED_TRADING` (`submit`, `cancel`, `fill-attribution`,
`protection-place`, `protection-trigger`, `reconnect-history-recovery`) carry a
current receipt from that exact realm.

Legend: `observed <date>` = a dated receipt in `CHANGELOG.md`
or `STATE.md`; `stale <date>` = that receipt predates a change to the adapter's
execution semantics and counts as `implemented`; `implemented` = the adapter
does it and offline conformance covers it, the live venue has not been seen
doing it here; `unknown` = the adapter does not do it.

<!-- BEGIN GENERATED capability-matrix -->
| Capability | `bybit_demo` | `bybit_mainnet` | `mexc_mainnet` | `hyperliquid_mainnet` | `hyperliquid_testnet` |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `submit` | observed 2026-09-06 | observed 2026-09-06 | observed 2026-09-10 | observed 2026-09-10 | implemented |
| `cancel` | observed 2026-09-06 | observed 2026-08-29 | observed 2026-09-10 | observed 2026-09-10 | implemented |
| `post-only` | observed 2026-09-04 | observed 2026-08-30 | observed 2026-09-10 | observed 2026-09-10 | implemented |
| `fill-attribution` | observed 2026-09-06 | observed 2026-09-08 | implemented | implemented | implemented |
| `partial-fill` | implemented | observed 2026-09-06 | implemented | implemented | implemented |
| `amend` | implemented | observed 2026-08-29 | unknown | implemented | implemented |
| `exact-quantity` | observed 2026-09-06 | observed 2026-09-06 | implemented | implemented | implemented |
| `reduce-below-minimum` | implemented | implemented | unknown | unknown | unknown |
| `protection-place` | observed 2026-09-09 | observed 2026-09-09 | implemented | implemented | implemented |
| `protection-change` | observed 2026-09-06 | observed 2026-09-06 | implemented | implemented | implemented |
| `protection-trigger` | observed 2026-08-25 | observed 2026-09-02 | implemented | implemented | implemented |
| `reconnect-history-recovery` | observed 2026-09-06 | observed 2026-09-02 | implemented | implemented | implemented |
| `funding-fee-cash` | observed 2026-09-06 | observed 2026-09-08 | unknown | unknown | unknown |
<!-- END GENERATED capability-matrix -->

The table is generated from the code by
`registry::capability_matrix_tests::the_published_capability_matrix_matches_the_registry`,
which fails on any drift between it and the block above.

#### Strategy execution requirements

`Strategy::execution_requirements` is asked of each built sleeve, so a
requirement that follows from config is answered by the sleeve that is actually
configured. `engine/engine-core/src/assembly.rs::compatibility` checks the set
against the chosen realm's row above.

| Plug | Requires | Never requires | Note |
| --- | --- | --- | --- |
| `long_native`, `carry_native`, `exodus_native` | `submit`, `cancel`, `fill-attribution`, `exact-quantity`, `protection-place`, `protection-change`, `protection-trigger`, plus `post-only` when the config sets `rest_entries` | `amend`, `reduce-below-minimum`, `funding-fee-cash` | One list through `native_common::sleeve::SleeveCore`: all three share `emit_effects`. `rest_entries` is true for `carry` and `long` and false for `exodus` in every deployed template. |
| `quoter` | `submit`, `cancel`, `post-only`, `fill-attribution`, `amend`, `protection-place` | — | `amend` is the plug's own verb: it moves its resting quote rather than replacing it, so it cannot run on `mexc_mainnet`. `stop_loss_fraction` is a required positive parameter, so every opening quote carries a stop. |
| `probe` | `submit`, `cancel`, `post-only`, `fill-attribution`, `protection-place` | `amend` | One post-only limit with a stop, pulled by id after its rest window. |
| `bench` | `submit`, `protection-place` | — | A market order with a stop; the workload never cancels or reads a position back. |

`amend` is absent from every native sleeve on purpose. A resting entry is
repriced by the ENGINE's working supervisor (`engine-core/src/working.rs`), not
by the sleeve. On a venue with `amend: unknown` that reprice is refused with a
WAL `Note` and the entry rests unrepriced until its window ends — 30 s in the
LONG templates. LONG runs exactly that way on `mexc_mainnet` today, so
requiring `amend` here would refuse the boot of a funded engine.

#### Boot checks on the chosen realm

| Check | Where | Refuses on |
| --- | --- | --- |
| Strategy × venue compatibility | `assembly::compatibility`, called from `runner.rs` after the sleeve identities are resolved and before the venue, lease, credential or socket | A required capability whose row is `unknown` — the adapter does not do it, so the sleeve's action would die inside the engine every time. `implemented` and a stale receipt are accepted and logged per sleeve as a forward test (`WARN forward test: this sleeve's execution requirements hold no current live receipt on this realm`). |
| Adapter-semantics pin | `VenueName::adapter_semantics_fingerprint`, one sha256 per venue over `engine-venue/src/venues/<venue>/**.rs` excluding test modules; checked by `engine/engine-venue/tests/venue/adapter_semantics.rs` | Nothing at runtime; it fails the suite, naming the realms of that venue that hold a current receipt. `Evidence::Observed { current }` stays a hand flag — deriving it would demote a realm on a comment edit. |

#### Invariants

* **Must**: every realm retain its declared readiness and feature mapping in
  `engine/engine-venue/tests/venue/dormant_venues.rs`; disabled features refuse
  selection before credential or socket access.
* **Must**: `engine/engine-venue/tests/venue/conformance.rs` pass under each
  enabled feature, alongside that adapter's exact request and private-stream tests.
* **Must**: readiness for a funded or practice realm be derived from that
  realm's capability row. `production-blocked` and `read-only` are the only
  overrides, and each names its reason in `VenueName::readiness_override`.
* **Must Never**: a realm reach `live-proven` other than through the matrix —
  every capability in the unattended set `observed` on that exact realm.
  A compiled adapter is not evidence, and neither is a promotion note.
* **Must Never**: a receipt taken before a change to that adapter's execution
  semantics — request encoding, order types, quantity conversion, fill
  interpretation — count as current. It is `stale`, and worth what
  `implemented` is worth.
* **Must**: every plug declare what its own actions need in
  `Strategy::execution_requirements`, and boot refuse a requirement the chosen
  realm's row calls `unknown` before any credential or socket is opened.
  `engine-core/src/assembly.rs::deployed_templates::every_deployed_template_is_compatible_with_its_venue`
  is what keeps that check from being the thing that stops a funded engine.
* **Must**: an adapter change re-pin that venue's
  `adapter_semantics_fingerprint` after reviewing every `observed` cell on its
  realms. `VenueCaps` and the row are one claim each about the same three
  behaviours (`amend`, `protection-place`, `reduce-below-minimum`) and
  `adapter_semantics.rs` asserts them equal for every compiled realm; where
  they disagree the code is right and the row is edited.
* **Must Never**: real capital reach a `production-blocked` or `read-only`
  realm. The boot gate refuses the run; there is no override flag.
* **Must**: `engine run` on a `live-canary` realm log the unproven capabilities
  at boot (`runner.rs`, `WARN forward test`). The run itself is the owner's:
  `posture=running` in `deploy/realms.tsv` and `REAL_MONEY=true` in the realm's
  credential file. The state still admits `engine canary-order`, and its
  receipts promote the matrix row; the label is never edited. A practice
  sibling elsewhere on the venue does not settle it: `hyperliquid_testnet` is
  `testnet-canary` and `hyperliquid_mainnet` is `live-canary` at the same
  time, because they are a different chain and a different account.

---

### 3. Boot & Recovery Sequence

`engine run --config PATH` runs on a single-threaded Tokio runtime, establishing identity and state before admitting any order risk:

| Phase | Step | Action | Invariants / Constraints |
| :--- | :--- | :--- | :--- |
| **1. Config** | Parse & Hash | Reads TOML config and hashes exact bytes. | Rejects unknown keys (`deny_unknown_fields`). |
| **2. Plugs** | Plugs Bind | Resolves compiled venue and strategy reducers by stable sleeve key. | Existing durable slots keep their owners; absent configured sleeves use passive owners restored from committed paged callback state. Pending queue cursors remain for engine recovery. |
| **3. WAL** | Replay & Lock | Locks `/var/lib/.../engine.wal` and replays the newest trusted segment, keeping only the record kinds boot's readers match (`assembly::boot_reads`) and carrying each kept record's WAL sequence (`assembly::BootReplay`). | Rebuilds identities and unfinished work; persists `ExecutionPrecisionV1` and a fresh `OrderIdEpoch` before new engine work. `Intent`, `IntentRefused`, `Verdict`, `CancelSent`, `QuoteFill`, `VenueTiming` and `LatencyLedger` never enter the boot replay; `engine replay`, `cohort`, `fills` and `latency` read them from the whole segment chain. |
| **4. Lease** | Account Lock | Authenticates account and acquires writer lease. On Hyperliquid the authentication also checks that the signing key's address is listed in the account's `extraAgents`, and refuses before trading when it is not. | Lock: `/run/lock/liquidity-migration/<venue>-<realm>-user-<id>.lock`. |
| **5. Private WS**| Stream Watermark| Connects private WebSocket and awaits ready state. | Blocks if auth fails or private queue is cold. |
| **6. Reconcile**| State Audit | Streams missed executions into canonical orders, sleeve accounting, physical exposure and stops; compares them with the account. | Unknown engine lineage is loaded from retained WAL archives; unresolved ownership, unfinished durable dispatches or account disagreement prevent history-frontier advancement and opening. |
| **7. Checkpoint**| Restore State | Restores sleeve checkpoints, exact open trade cost basis, loss rows and pending reservations; starts covers empty. | Rejects incompatible schema, fingerprint, quantities, price ranges or payloads; unknown monetary valuation remains explicit. |
| **8. Effects / Re-plan** | Ordered recovery | Drains unfinished durable strategy effects before boot callbacks and durable input redelivery. | Callbacks can queue effects and timers; admission blocks unready growth. |
| **9. Inputs** | Feeds Live | Starts market data, signal IPC and control spool; begins producer nonce exchange. | Required producer frontiers suspend growth until established and caught up; boot itself does not wait for participation. |

#### Exit classes

A run that ends without being asked returns one `EngineError`. The supervisor restarts the unit on every class; the class says what the restart settles. The log line starts with the prefix.

| Class | Log prefix | Means | After the restart |
| --- | --- | --- | --- |
| `Boot(_)` | `boot:` | This log, config, and venue cannot be started from. | Fails the same way until the input changes. |
| `TaskStopped { task, .. }` | `<task> stopped` | The venue task, a durability writer, or the strategy host ended. `task` names which. | Fresh tasks; the log carries the rest. |
| `TimedOut(_)` | `timed out waiting for` | A bounded wait on durability or a venue reply ran out (`MUTATION_DRAIN_TIMEOUT`, 10 s). | The wait is retried from the log. |
| `Reconcile(_)` | `venue reconciliation needed:` | An opening order an account-level halt pulled is still live 5 s after the first cancel reply: the private stream did not confirm the cancel and the venue's status reads did not settle the order either, so the venue's account and the engine's view may disagree. | Boot phase 6 settles it against the venue. |
| `State(_)` | `state:` | An invariant on the engine's own state failed. | A defect. Report it with the log. |
| `Wal(_)`, `Venue(_)` | `log:`, `venue:` | The log or the venue refused. | As that error says. |

#### Durable state and producer protocol

| Boundary | Implemented contract |
| --- | --- |
| Production callbacks | Every harness and production uses `CallbackExecution::Embedded`. Trusted reducers run on the loop thread; `catch_unwind` faults the panicking sleeve and cancels its orders while other sleeves continue. |
| Volatile market callbacks | Strategies read current `Books` directly. Changed checkpoints and ordered effects persist; unchanged checkpoint proposals write nothing. No current callback record contains a market snapshot. |
| Ordered effects | `StrategyTransitionQueued` retains effect order and placement IDs; retained process-transition records replay through the same effects. `StrategyEffectCompleted` retires an index after its completion; a per-turn budget retains the suffix, including reductions. |
| Orders | Checkpoint, `Intent`, `Verdict`, `OrderSent` and `OrderDispatchAttempted` share one barrier before dispatch. An uncached leverage mutation first flushes dependent strategy state. Ambiguous sends retain ownership; terminal rejection/cancellation releases an attempt ID while its durable exit target remains. |
| Rotation | `segment_base_v7` restates canonical portfolio/accounting, open trade lots, pending dispatches, callbacks/effects, identities, metadata, input lifecycle, explicit legacy source retirements, retained terminal orders and markout horizons still owed. Ordinary readers accept v1 and v7; the offline converter privately accepts v5. The retained `8c92c964` release reads all original host families. Required precision and unsupported record kinds cause refusal without truncating the log. |
| Accepted inputs | The channel admits at most 256 rows / 64 MiB. Durable admission retains one ordinary delivery per destination plus missing-prefix recovery ownership within byte limits; spool acknowledgement follows the acceptance barrier. |
| Outcomes | Consumed, explicitly rejected and retained pending are distinct. Terminal payload release follows its WAL barrier; failed callbacks retain the accepted input for retry. |
| Readiness exchange | `input-readiness-request.json` / `input-readiness-response.json` carry a fresh matching `boot_nonce`. Schema 2 lifecycle reports bind producer generations, granted epochs, stable sleeve destinations and published frontiers; schema 1 responses retain legacy compatibility. Metadata files are excluded from observation inventory. |
| Producer retirement | A generation seals its published tail before retirement. `retired_through` compacts managed generations; unresolved legacy tails keep their owner and opening restriction until reconciled. An explicit offline `LegacySignalSourceRetired` outcome can terminate a permanently stopped legacy suffix while retaining its original accepted cursor; managed sources cannot use it. Retired generations cannot reopen a cursor. |
| Failure | Missing, malformed or I/O-failed readiness keeps required growth suspended and retries. Request-time accepted prefixes distinguish a rewind from concurrent arrivals. Reductions, protective stops and account recovery remain available. |
| Metadata | A durable exact catalog binds native instruments to venue/environment. A retained catalog supports recovery during a failed refresh; new growth waits for an authoritative refresh, and a delisted instrument retains recovery ownership without becoming eligible for growth. |
| Trusted code boundary | Embedded reducers have panic containment. They share engine memory and loop time; there is no child callback deadline or process memory sandbox. Retained process payload readers keep their bounded migration contracts. |

---

### 4. Storage, Paths & Memory Budget

| Resource | Demo Path | Mainnet Path | Quota / Budget |
| :--- | :--- | :--- | :--- |
| **WAL File** | `/var/lib/liquidity-migration-engine/engine.wal` | `/var/lib/liquidity-migration-engine-mainnet/engine.wal` | Rotates at `256 MB` (`wal_rotate_mb`). |
| **Account Lease**| `/run/lock/liquidity-migration/bybit-demo-*.lock` | `/run/lock/liquidity-migration/bybit-mainnet-*.lock` | Single-writer exclusive advisory lock. |
| **Signal IPC** | `/var/lib/liquidity-migration/signals/demo/` | `/var/lib/liquidity-migration/signals/mainnet/` | Disk spool row `<seq:020>-<sha256>.json` is the delivery; a frame on `stream.sock` is the doorbell, sent only when the row is ≤ 16 MiB. Payload ≤ 16 MiB; readers take it as a JSON string or a byte array. A gap in a source's sequence is an `ERROR` line, not an exit. |
| **Control Spool**| `/var/lib/liquidity-migration/controls/demo/` | `/var/lib/liquidity-migration/controls/mainnet/` | Immutable command files (`0750`). |
| **Heartbeat** | `/var/lib/liquidity-migration-engine/heartbeat.json`| `/var/lib/liquidity-migration-engine-mainnet/heartbeat.json` | Atomic 1-line JSON; max age 30s. |
| **Trade Log** | `/var/lib/liquidity-migration-engine/trades.jsonl` | `/var/lib/liquidity-migration-engine-mainnet/trades.jsonl` | Append-only round-trip closed trades. |

#### Resident State and Archive Bounds

| Owner | Resident bound / scaling | Overflow or recovery behavior |
| --- | --- | --- |
| Active WAL replay | Resident records scale with the kinds boot reads, not with the newest trusted segment: `assembly::boot_wal` refuses per-decision and per-call telemetry at decode time, about half a trading segment's frames and bytes; `wal_rotate_mb` bounds the rotation target, not total process RSS. | Boot opens the newest trusted segment through `open_current_with`; operator state verification reads the same segment through `replay_current`; archive readers stream older segments without decoding the whole family. |
| Callback backlog | Bounded payload admission; at most `MAX_PROCESS_PROPOSAL_BYTES / 64` disk queue slots, with WAL cursors and event hashes. | One asynchronous page load owns its input; the core continues unrelated work while stalled destinations retain their queues. |
| Execution response | Disk-sorted runs target 256 KiB plus one bounded row; individual encoded rows below 8 MiB; stable timestamp/arrival ordering. | Complete venue windows are consumed row by row at boot and runtime; no aggregate execution-response `Vec` is retained. |
| Execution identities | `1 << 20` IDs and 64 MiB of ID bytes across a 7-day reach plus 120 s pad. | Exhaustion is explicit; an ID still inside retention is never discarded to make room. |
| Terminal orders | At most 256 rows / 4 MiB of encoded terminal payload in the production cache; live orders are excluded from eviction. | The same canonical order row is reconstructed from WAL archives on demand. Unchanged housekeeping does not rescan or serialize cached terminal rows. |
| Cold order lookup | One pending private event/history row and one asynchronous reader; selected WAL row at most 8 MiB with a 64 KiB read buffer. | Lookup failure retains the event and affected symbol. A successful read appends `OrderLineageRestored` before applying the execution; absent lineage remains unresolved. Worker reads are cancelled when their owner is dropped. |
| Terminal rotation retention | Terminal timestamp compared with `min(wall_ms, execution_history_through_ms) - (7 days + 120 s)`. | Incomplete history cannot expire ownership early. Cache eviction uses the retained archive; missing archive segments are explicit errors. |
| Retained WAL family | Order lineage requires every segment from segment 1; unresolved callback cursors retain their source segments. | Rotation and terminal-cache expiry do not authorize archive pruning. Missing source frames remain recovery errors. |

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

| Configuration section | Contents |
| --- | --- |
| `[engine]` | WAL paths, group flush timing (`1–1000 ms`), socket paths and heartbeat interval. |
| `[risk]` | Gross capital reference, leverage, order size bounds and rolling-loss limit. |
| `[[strategy]]` | Sleeve configurations (`CARRY`, `LONG`, `EXODUS`, `MAKER`) resolved by stable key into durable ID order. |

#### Signal identities and who checks them

The signal worker's config identity (`engine/signal-worker/src/config.rs`) carries these digests; `signal_feature_contract_sha256` (`engine-strategies/src/native_common/mod.rs`) hashes `schema_version`, `kind`, `routing.source`, the whole `sources` block (its four `bybit_*` keys included, so a key rename is a cold start on every realm), `live.public_market_realm` and the sleeve's feature physics.

| Identity | Compared where | On mismatch |
| --- | --- | --- |
| `long_rule_sha256`, `long_feature_contract_sha256` | worker load vs the engine TOML's `native_long` block; `native_long/plug.rs` vs `core.config` | load: `signal feature profile disagrees with native LONG engine params`, the worker does not start; engine: `LONG signal config does not bind this reducer`, the observation is refused |
| `carry_rule_sha256`, `carry_feature_contract_sha256` | worker load vs `native_carry`; `native_carry/plug.rs` vs `core.config` | load: `registered CARRY JSON disagrees with native CARRY engine params`; engine: `CARRY signal config does not bind this reducer` |
| `operational_profile_sha256` | worker load vs both native blocks | `operational profile or CARRY public clock disagrees with native engine params`; never value-compared engine-side |
| `signal_config_sha256`, `engine_config_sha256` | shape only (`validate_signal_identity`) | a non-sha256 value is refused; never value-compared |
| `long_decision_fingerprint` | worker watermarks; `native_long/plug.rs` outer observation vs inner envelope and vs `core.config.fingerprint()`; engine boot vs `strategy.checkpoint_identity()` | worker: LONG watermarks cleared and republished; engine: `LONG outer and inner decision fingerprints disagree`, reducer refusal, or `checkpoint identity (…) does not match configured (…)` at boot |
| `carry_decision_fingerprint` | worker watermarks; `native_carry/plug.rs` outer vs inner only | worker: CARRY watermarks cleared; engine: `CARRY outer and inner decision fingerprints disagree`. CARRY is bound to its own config only through `carry_rule_sha256` |
| checkpoint `source_contract_sha256` | restore, open, bootstrap coverage | restore: `checkpoint public source contract has drifted; a new cold start is required`; open: checkpoint, journal and pending are archived under `drifted-source-<sha8>-<unix_ms>/` and history cold-starts with the producer generation kept |
| checkpoint `state.config`, `long_feature_sha256`, `carry_feature_sha256` | `checkpoint_needs_adoption` on open | re-restored and re-saved; the config identity is overwritten, not refused |
| checkpoint `destination_sleeves` | `restore_destinations`, `bind_destination_sleeves` | `checkpoint directional sleeve keys changed`, `engine named destinations reinterpret already published source history` |
| heartbeat copies, `check-config` object | nothing reads them | reported only |

#### Execution and account limits

| Control | Runtime contract |
|---|---|
| Entry work | LONG in both realms rests PostOnly for 30 s. Crossing cancels first; an independent terminal REST lookup must agree exactly with recovered fills before one fresh IOC remainder is admitted. Restart cancels recovered entries, including sleeve growth that reduces the physical position |
| Mark collar | `engine.execution_limits.mark_collar_bps = 100`; market requests become bounded IOC limits before risk admission. Limits and amendments outside the mark band are refused; mark age is bounded by `max_quote_age_ms`. Durable exits retry unfilled remainders |
| Venue-native stops | Bybit Full-position stops remain exchange-hosted market orders. Bybit does not permit this custom collar on their trigger execution; it therefore does not guarantee a maximum liquidation fill price |
| Rejections | Five distinct engine order IDs rejected within 10 s latch `may_open=false` durably and cancel remaining openings. Repeated reports for one ID do not multiply the count; owned reductions remain eligible |
| Symbol cap | Gross owned sleeve exposure, manual residual and pending openings share a cap of 0.50 × reference per symbol; opposing sleeves count separately |
| Margin cap | Initial-margin allowance is 0.70 × reference. Account IM ratio at or above this fraction or MM ratio at or above 1 refuses growth |
| Stop distance | Opening and held sleeve stops use at most `min(disaster_stop_fraction, 0.5 / max(configured_leverage, observed_leverage))`; default 10% at 5×. Held same-side stops tighten further toward the midpoint of known `markPrice` and `liqPrice`. Tighter existing stops remain |
| Account metrics | Optional exact `accountIMRate`, `accountMMRate`, `markPrice`, `liqPrice` survive serialization and appear under heartbeat `account_metrics`. Blank/zero liquidation prices remain unknown |
| Routine drift | Account reads every 2.5 s compare physical exposure after outstanding mutation generations settle. A mismatch requests execution history; a confirmed unexplained residual latches openings |
| Account authority | Mainnet uses sole leverage authority at owner direction. Shared mode remains available for accounts with another trader. Proven own-lot reductions may increase the physical net behind a hand position; the opening latch does not block them. Existing hand-side stops remain; the existing virtual sleeve stop owns an opposing logical lot |
| Public book gap | Invalidate and resubscribe only the affected L1/L50 topic; healthy symbols retain quotes |
| Trade WebSocket | Ping every 20 s; missing pong for 10 s causes proactive reconnect with backoff. Sent requests with uncertain outcomes are resolved independently, never blindly resent |
| Reprices | Up to ten adjacent distinct amendments enter the Bybit transport before replies; IDs route out-of-order results. Cancels and other intervening commands retain ordering |
| CARRY clock | The midnight decision becomes eligible at receipt time 00:20 UTC with complete midnight data; the 60 s worker cadence and source readiness may delay publication. Historical hourly replay does not prove fills at 00:20 |
| Cancel on disconnect | No account DCP configuration is present in the authenticated September 8 query. Bybit institutional enablement and private-stream `dcp` subscription are prerequisites; a local reconnect is not exchange DCP |

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

| Gate | Check | Rejection / outcome | Behavior on Failure |
| :--- | :--- | :--- | :--- |
| **Account Freshness** | Account/private-state age within configured bound. | `StaleAccountView` | Blocks growth; a reduction must still be provably safe for the physical exposure interval. |
| **Quote Freshness** | Quote age within the configured limit. | `StaleQuote` | Blocks new entries; recovery and protective work retain their own admission rules. |
| **Capital / Margin** | Exact virtual gross and incremental physical margin, including outstanding orders and ambiguous amendment ranges. The book's modelled stop charge — each position's notional × `max(stop_fraction, disaster_stop_fraction)` — against `allowance_usdt`. | `GrossExposureExceeded`, `EnvelopeBreached`, margin or leverage refusal | Refuses additional exposure beyond available account capacity. The modelled stop charge is what the configured stops lose if they fill at their triggers; it is not a bound on account loss, and does not cover a gap through a trigger, a liquidation, a venue outage or collateral revaluation. |
| **Shared Symbol Ownership** | Exact quantity and stop belong to `(StrategyId, SymbolId)`; physical exposure is the net of all sleeves and pending effects. | Portfolio admission verdict | Same-direction and opposing sleeves are supported; another sleeve’s position is never reassigned or silently netted away. |
| **Sleeve Reduction** | Requested quantity does not exceed the owning sleeve; any resulting physical exposure has valid protection and margin. | Exact allowed quantity or durable emergency takeover | A virtual reduction can increase physical exposure when sleeves oppose; it does not inherit blanket physical reduce-only permission. |
| **Rolling Loss / Valuation** | Exact 24-hour net closed PnL compared with the capital loss limit; unknown canonical valuation is explicit. | `RollingLossTripped` or unknown-state refusal | Blocks entry and size increases; exits remain subject to physical safety and venue legality. |

#### Quantity, Price and Accounting Contracts

| Boundary | Canonical contract |
| --- | --- |
| Strategy intent | `Intent.exact_quantity` and `Intent.exact_prices` carry chosen exact quantities and limit/stop prices; supplied values must match compatibility projections. `StrategyCtx` exposes exact owned and in-flight quantities. |
| General exits | Exact retained targets survive partial fills, market maximum chunks and restart. Native full exits use the canonical owned lot; explicit partial reductions retain their chosen amount even when its `f64` projection equals the full lot. Legacy scalar full-close projection matching is a compatibility rule only. |
| Legacy quantity adoption | Required durable grid context resolves aggregate legacy units within 64 ULPs per input before canonical fill reduction and at the adoption boundary. Contextual replay preserves raw legacy cash/fees, native exact totals and real close timestamps. Validated legacy-dependent automatic FIFO and internal full-close allocations are reconstructed; internal settlement retains its recorded price, time and zero-net contract. Canonical-only allocations and explicit native partial amounts remain exact. Canonical inventory never uses legacy dust deletion. |
| Runtime controls | Resolve durable sleeve IDs from the newest trusted WAL segment, matching engine boot; reject a torn current tail before writing the control request. Retained archive volume does not accumulate in control-command memory. |
| Ordinary input scheduling | Tick, timer, control, signal and market lanes rotate after each selected ordinary input. Each continuously ready enabled lane wins within five ordinary turns. Private updates, recovery, halt and mutation-drain priorities remain outside that rotation; they prevent a universal wall-time bound. |
| Control spool cancellation | The feed owns one pending scan, retirement or rejection operation and the next empty-scan deadline across cancelled polls. A delivered file remains until the core records its outcome; restart rereads retained immutable requests. IO errors remain observable. |
| Retained archive readers | Epoch recovery distinguishes WAL tags from nested order kinds and permits null client IDs on verdicts. Callback payloads are decoded only for their record kind; source CRCs and pinned byte boundaries remain enforced. |
| Terminal order recovery | Exact cumulative fills must be covered by durable execution history before terminal retirement; legacy fill frontiers retain their binary64 value. Archive activation appends `OrderLineageRestored` with the original request and frontier, including legacy scalar terms. |
| Callback contention | Busy market invocations defer order news, timers and controls without marking the strategy failed. Durable source cursors advance only after acceptance. |
| Missing closes | A native flat reading cannot erase owned inventory. Unmatched physical exposure blocks openings until history or explicit operator reconciliation resolves it. Historical `ClaimsDropped` records remain readable. |
| Terminal order lookup | Native cumulative fill quantities are compared exactly with the durable per-order frontier. Unseen fills retain the unresolved order and request history; repeated terminal status alone cannot retire it. |
| Wire legality | Exact instrument steps, minima/maxima, notional bounds and directional price rounding determine `ExactOrderTerms`; canonical terms flow through dispatch, amendment and risk reassessment. |
| Account / Risk | Canonical venue decimals and provenance determine equity, available balance, position quantity/entry/stop, reservations and risk comparisons. Legacy numeric inputs retain explicit binary64 semantics; display projections do not replace known exact values. |
| Admission clock | New orders, queued dispatches and price amendments assess account freshness against the current parent clock supplied to `RiskKernel`; an intent's persisted decision timestamp never supplies account age. |
| Replay timing | Persisted monotonic stamps do not enter a new process's latency samples. Restored effects and dispatches have no runtime source timing; a callback executed after restart retains its new completion time, with its prior-process input origin absent. Current-process queue, venue and barrier timing remains measurable. |
| Ambiguous amendments | Exact reservation lower/upper prices retain both possible wire outcomes until resolved. Replay refuses a persisted range that excludes the current canonical request. |
| Send-boundary authority | Every `SendOrders` group that opens exposure, and every amend, carries `CommandAuthority { epoch, queued_ns, expires_at_ns }` minted at enqueue; cancels, stops and reducing sends carry none and are never refused. The engine advances the shared `AuthorityEpoch` when a permission an opening was admitted under goes away: `may_open` latched false, private stream lost, the first unanswered dispatch, a strategy's entries disabled, callback fault or retirement, an instrument catalog install, the rolling-loss window tripping. `authority_refusal` is re-read by the venue task after the queue wait and by the paced adapters (`send_orders_under`: Bybit after its create budget, MEXC after `admit`) before signing; a lapsed command is answered `Err(BadRequest("authority: …"))` with no venue call and takes the never-sent path (reject `never sent: …`, reservation released). A command the gateway already holds resolves through the lookup path. `engine.opening_dispatch_ttl_ms` (default `10000`) is the queue-age bound; under `engine sim`/`engine backtest` it runs on tape time. |
| Native account stops | Hyperliquid/Lighter protection is derived from uniquely identified, correctly directed stop orders and their canonical remaining quantities. The aggregate trigger is the first price covering the full position; partial or opposite-side orders leave uncovered quantity for repair/reduction. |
| Protective repair durability | A held position without catalog metadata can use its durable same-side scalar stop intent for repair. Repair writes `StopSet` before venue mutation unless matching durable intent and an exact same-side sleeve stop already cover the requested trigger. Exact-only admission does not remove these paths. |
| Portfolio retries | Retry deadlines use engine monotonic nanoseconds in production and virtual replay. Attempts back off from 250 ms exponentially to 30 s; deadlines are process-local and do not change event priority. |
| Fill ownership | One execution ID applies once to canonical order progress, physical exposure and sleeve allocation. An aggregate emergency fill carries deterministic exact allocation slices; fees split by the same quantities and sum to the original fee. |
| Legacy FIFO allocation | Optional `ExecutionAllocation.legacy_quantity_step` records the actual quantity grid for an `amounts=None` emergency fill. Absence preserves raw binary64 semantics. The existing 64-ULP unique-grid rule determines positional quantity; raw quantity, price, fee and asset availability stay unchanged. Fills apportions original consideration and the whole fee across normalized slices. Native amounts never use this interpretation; an exact native residual cannot be rounded away. Reader support must precede runtime writes because older allocation readers reject normalized slices. |
| Legacy removal condition | Both realms must boot and rotate canonical state, and no retained WAL replay/rollback contract may depend on the writers. Normalized legacy contributions must remain distinguishable from native exact accounting. Archive reactivation, legacy inventory, book-simulator binary64 fills and protective repair still require compatibility. The accepted outcome of zero `exact_terms: None` occurrences in non-test engine-core and deletion of legacy modules remains unmet; deletion is stopped while these dependencies remain. |
| Emergency exits | Durable phases resolve outstanding orders, close physical net exposure in legal exact chunks, then settle opposing virtual offsets. Rejection/cancellation retains the obligation with a new attempt; ambiguous sends keep their existing identity until resolved. |
| Client order identity | Normal, general-exit and emergency orders use `eng-<whole-second-ms>-<counter>`. A durable logical boot epoch advances beyond prior epochs even if wall time moves backward; the 18-bit counter remains reversible through Lighter’s native client index. |
| Markout horizons | A fill is owed marks at 1 s / 15 s / 60 s / 300 s, each given up on 5 s past its horizon. `segment_base_v7.owed_markouts` restates the horizons still owed; boot rebuilds the queue from that restatement plus the replayed fills, less every `Markout` the segment already holds, so one `(client_order_id, fill_ts_ms, horizon_ms)` is asked for and recorded once across any number of restarts. A restated obligation carries the venue's wall stamp only, and its age is recomputed against this process's monotonic clock, which starts after the fill. A horizon already past its lateness bound at boot is written as a late mark with its true age and kept out of its column; a fill older than 305 s is not taken over. `engine-tools fills` splits late marks into those owed across a restart and those this engine looked late for. |
| Cost basis / Loss | Exact open trade lots, entry cash and proportional fees survive rotation. Closed canonical net amounts plus `min(current account open PnL, 0)` feed the exact rolling-loss sum. Missing cost basis or an unvalued settlement/fee asset produces an unpriced row, never a fabricated zero or USDT value. Funding is outside this closed-fill loss calculation. |
| Prospective portfolio risk | A sleeve with unknown historical cost uses the latest accepted market price for gross exposure and stop distance; the accounting basis remains unknown. Known cost retains conservative entry/current-price valuation. Missing both market price and basis, missing/crossed stops, and breached gross limits refuse openings. Shared and opposing sleeves count separately. |

| State owner | Key / purpose |
| --- | --- |
| `OrderRec.fill_quantity` | Client order ID; completion and remaining quantity. Legacy/display scalars are derived at boundaries; serialized legacy fields remain readable |
| `Inventory.positions` | Sleeve and symbol; owned quantity, basis and stops. Physical owned net is derived, preserving opposing holdings |
| `ExecutionAccounting` | Sleeve, symbol and asset; cash, fees and unresolved valuation, including after positions close |
| Risk exposure book | Outstanding orders and fills newer than the account snapshot; reservations are separate from settled positions |
| `logged_exposure` | Symbol; trusted physical reconciliation baseline, including accepted external positions |
| `Fills.by_key` / `Fills.lots` / `Fills.pending` | Execution-cost aggregates / round-trip lifecycle and closed-trade output / markout horizons still owed, keyed by client order ID and venue fill stamp; none authorizes inventory changes |
| Execution ID / legacy overlap caches | Exact execution identity / multiplicity for old fills without an execution ID |

#### Rolling-Loss Circuit Breaker Invariant

* **Must** compare exact net closed PnL plus current account open losses with `max_rolling_loss_fraction × capital_reference` over the last 24 hours.
* **Must** restore exact open trade basis and loss rows across restart; process restart cannot clear a loss trip or unknown valuation.
* **Must** release valid expiring loss/unpriced rows as their venue timestamps leave the 24-hour window; malformed canonical money remains invalid rather than expiring as a valid loss row.
* **Must Never** treat absent fees, foreign fee assets without valuation, or missing entry basis as known zero net PnL.

---

### 7. Runtime Controls

Operator controls are dispatched by placing JSON command files into the realm control spool:

| Command Action | CLI Syntax | Effect |
| :--- | :--- | :--- |
| **Set Entry Permission** | `engine set-strategy-entry-permission --config <cfg> --strategy <sleeve> --entries-enabled <true\|false>` | Enables or disables opening orders for a specific sleeve. |
| **Flatten Strategy** | `engine flatten-strategy --config <cfg> --strategy <sleeve> --request-id <uuid>` | Retains a durable exact sleeve target, resolves working orders and retries legal exit chunks; opposing-sleeve physical risk can invoke aggregate emergency handling. |

* **Refusal Handling**: Malformed or semantically invalid command files are moved to `<filename>.rejected` to prevent spool blockage.
* **Idempotency**: Repeated submissions of the exact same request ID are no-ops.

---

### 8. Canonical Native State

When performing rollouts or cold starts, state is seeded or verified while units are stopped:

| CLI Subcommand | Purpose | Preconditions |
| :--- | :--- | :--- |
| `initialize-native-strategy-state` | Initializes canonical empty checkpoints in a fresh WAL. | Empty WAL file only. |
| `retire-legacy-signal-sources` | Records an operator-selected terminal outcome for stopped legacy sources without rewriting accepted cursors. | WAL lock; full plan validation; explicit `--execute`; no accepted pending observations. |
| `reconcile-clear` | Restates canonical authenticated physical quantities and records the operator's historical evidence note; currently owned net quantities must agree first. | WAL lock; exact native quantities; explicit `--execute`; identical interrupted clear retries append nothing. |
| `verify-native-strategy-state` | Verifies WAL checkpoint identity, frame CRC, and state provenance from the newest trusted segment, the records boot replays (`engine_wal::replay_current`). | Run before restarting units on deploy. |
| `wal-convert-v5 --wal FAMILY --output-dir NEW_DIRECTORY` | Copies a complete family while upgrading v5 bases to v7, retaining source frames and sequence identity; existing replay materializes unknown or recorded cost basis. Callback offsets use segment/sequence lookup after relocation. | Offline input; family path, not a numbered suffix; new output directory. Original files remain unchanged. Missing, corrupt or incomplete sources are refused. |

#### Handover Invariants

* **Must** use the newest trusted segment for takeover state verification;
  `engine_wal::replay_chain` is an offline reader whose memory grows with the
  retained family, while `engine_wal::replay_chain_visit` streams it.
* **Must** verify canonical checkpoints before restarting units. Deployment initializes
  only an empty WAL with no retained legacy source files; other unverified state
  requires recovery through the compatible retained release.
* **Must Never** reinterpret an unsupported required record as a torn tail or
  truncate it. `ExecutionPrecisionV1` and `segment_base_v7` require a compatible
  reader; an older reader’s explicit refusal is the compatibility behavior.
* **Must** use converter output only after successful command completion;
  interruption can leave a partial directory, which a rerun refuses.

#### Strategy Table Invariants

| Identity boundary | Implemented contract |
| --- | --- |
| Durable ID | `StrategyId(i)` refers to the stable `SleeveKey` in `IdentityState.sleeves[i]`; current reports learn names from that registry, while retained `Names` records remain readable. |
| Config reorder / insertion | Configuration keys resolve into existing durable slots; a newly named sleeve appends a slot regardless of its configuration position. Boot and takeover both construct strategies in registry order. |
| Config removal | The durable slot remains with a passive owner, preserving its fills, positions, checkpoints, stops and reduction obligations. Its ID is never reused. |
| Rename | A new key creates a new sleeve; it does not transfer the prior key’s state. Existing runtime kind/configuration/checkpoint compatibility is checked separately. |
| Instrument identity | A dense symbol slot binds the native instrument key to authenticated venue/environment. A changed native mapping or scope is refused; delisting does not erase the existing slot. |
| Legacy migration | Unambiguous `Names` tables establish the initial registry. A legacy table that reorders/reassigns IDs, or state without an identity table, is refused instead of inferred. |

* **Must** preserve every existing durable slot’s key and every recorded fill’s owner.
* **Must Never** use current configuration position to reinterpret an existing `StrategyId` or transfer a removed sleeve’s position to another sleeve.
* **Must** treat a newly appended key as owning no earlier fill; a plug without a checkpoint contract requires no initial checkpoint.

---

### 9. Backtest Replay (`engine backtest`)

The live loop — `Engine::boot_as_exact`, the risk kernel, the strategy reducers, the working-order supervisor, the log — driven by a recorded `market_tape` in the tape's own time, on a simulated venue.

| Input | Source | Contract |
| :--- | :--- | :--- |
| `--tape PATH` | `python -m market_tape rows ARCHIVE --hours A..B > tape.jsonl` (or `.jsonl.zst`) | `market_tape/schema.py` rows, `local_receive_ts_ns` ordered; a malformed row stops the run at its line. Book rows must be Bybit's: another venue's chaining is refused, not guessed |
| `--instruments PATH` | `ARCHIVE/<day>/<HH>/_meta/instruments-<stamp>.json[.zst]` | Bybit `instruments-info` rows; original decimal strings and optional price/quantity bounds survive in the exact catalog. Missing required exact metadata refuses boot |
| `--config PATH` | engine TOML with `[[strategy]]` blocks | `wal_path`, `trades_path`, spool paths are replaced by the flags |
| `--wal PATH` | new file | Must be absent or empty; every run starts from nothing |
| `--signals DIR` | signal spool | Rows validated as live; delivered at `available_wall_ts_ms` |
| Numeric boundary | Existing simulated venue | Decimal instrument strings remain exact; JSON numbers retain their binary64 input precision. Simulated fills and cash remain binary64, with `amounts=None`; no native asset or multiplier is invented |

| Output | Written by | Holds |
| :--- | :--- | :--- |
| `--wal` | the engine | The run's log, byte-identical across runs of one tape |
| `--trades PATH` | the engine (`ClosedTrade`) | Closed round trips: gross, fees, net, holding time, maker share, shortfall |
| `--equity PATH` | the venue (`EquityPoint`) | Cash, unrealized, equity, initial margin at every fill and settlement |
| `--report PATH` | the runner (`BacktestReport`) | Tape stats, venue books, engine ledger, reconciliation |

| Dial | Default | Meaning |
| :--- | :--- | :--- |
| `--capital` | 10000 | Starting USDT |
| `--taker-fee` / `--maker-fee` | Maximum observed rate from the snapshot compiled into the binary at build time from `configs/bybit_fee_rates.json`; `LIQUIDITY_MIGRATION_FEE_SNAPSHOT=PATH` overrides it | Decimal fee rates; explicit flags select a scenario. Report retains snapshot hash and observation time; unobserved symbols have no fee coverage claim |
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

```bash
# One tape hour range to a flat file, then the replay and its report
python -m market_tape rows ARCHIVE --hours 2026-09-02T00..2026-09-03T00 > tape.jsonl
python scripts/research/run_engine_backtest.py --config engine/engine.demo.toml \
  --tape tape.jsonl --instruments ARCHIVE/2026-09-02/00/_meta/instruments-*.json.zst \
  --out-dir var/backtests/2026-09-02

# The engine alone
cargo build --manifest-path engine/Cargo.toml --release --locked -p engine-tools --bins
engine/target/release/engine-tools backtest --config CONFIG --tape TAPE \
  --instruments INSTRUMENTS --wal run.wal --trades trades.jsonl --report report.json
```

---

### 10. Deterministic Simulation (`engine sim`)

The core loop with embedded strategy reducers and a virtual clock on a seeded synthetic market against the backtest's simulated venue, with faults on every boundary the engine has with the world and process deaths at seeded instants. Repeated runs compare log bytes under the declared execution conditions. This exercises the same embedded callback mode as production; synthetic timing does not measure host or network latency.

| Flag | Default | Meaning |
| :--- | :--- | :--- |
| `--seed N` | 1 | The seed; `--seeds K` runs `N..N+K` |
| `--strategies quoter\|demo\|mainnet\|mexc\|hyperliquid` | `quoter` | `quoter` runs one market maker and no producer. A realm name runs that deployed template's own generated `[[strategy]]` blocks, byte for byte from `deploy/engine.<realm>.toml.template`, on `configs/operational.json`, against the synthetic producer. Both are compiled in |
| `--seconds S` | 600 | Tape length in virtual seconds (quoter mode) |
| `--hours H` | 12 | Tape length in virtual hours (realm mode) |
| `--tape-step-s S` | 1 quoter, 10 realm | Seconds between tape rows: one ticker, one book delta and one print per symbol per step. Must stay inside `max_quote_age_ms` |
| `--symbols M` | 2 quoter, 3 realm | Symbols traded, from `BTCUSDT`, `ETHUSDT`, `SOLUSDT` |
| `--capital USDT` | 10 000 quoter, 500 realm | The venue's starting cash, which is the account's whole equity. 500 is where the LONG target clears every catalogue minimum and stays under the profile's per-symbol cap |
| `--shock on\|off` | off quoter, on realm | One seeded symbol falls 20 % over 60 s from 1.5 h in and holds there an hour |
| `--pump P` | 0.5 | Chance per symbol and UTC day that the producer's features carry an entry trigger |
| `--gate` | off | Also publish one `llm_gate_candidates` row |
| `--crashes C` | 1 | Process deaths; the private socket dies with the process and the next boot recovers from the log and the venue's fill history |
| `--faults none\|light\|heavy` | `light` | Per-call fault rates (`engine/engine-tools/src/sim/faults.rs`); `light` is one command in fifty going wrong |
| `--twice` | off | Run every seed twice and compare the logs byte for byte |
| `--out DIR`, `--keep` | temp dir, off | Where the tape, config, log and trades go; kept only with `--keep` |
| `--report PATH` | none | The sweep as JSON (`SweepReport`) |

Realm mode does not model the signal worker, the spool files, the socket, or
the venue's real latency: the producer is a pure function of the seed, the
spool is a queue in memory, and every clock is virtual. The producer publishes
on the worker's own grids — LONG features close at UTC midnight and republish
hourly, CARRY publishes readiness, hourly market snapshots, funding on the
eight-hour grid and a daily feature batch that carries the replay window
every day — and holds the worker's own
two-round producer lifecycle handshake, because an engine booted with exact
instruments refuses to let a legacy source make a dependent sleeve ready.

| Injected | Where | What the engine must do |
| :--- | :--- | :--- |
| venue refusal; request lost before the venue; reply lost after it; slow reply; account read failure | `FaultyGateway` | treat the ambiguous send as ambiguous; learn the order's fate before growing; a halt cancel refused, unanswered or unconfirmed by the private stream is settled by reading the order's status, which cancels a working order again or records the ending |
| private update dropped, duplicated or delayed; socket hiccup | `FaultyOrderFeed` | dedupe by execution id; recover a gap from the venue's fill history |
| market feed hiccup; feed reset | `FaultyMarketFeed` | re-arm; never open against a stale quote |
| signal row delayed past its window, delivered twice, or withheld so its source has a hole | `FaultySignalFeed` | consume a late row without deciding; dedupe on the durable cursor; record the gap, suspend the destination's openings, accept the missing row and resume |
| one symbol falls 20 % and holds there | `market::Shock` | the native position stop triggers on the mark, fills by walking the book, and its fill is owned by the sleeve that opened the position |
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
| `strategies_healthy` | no sleeve reports a health error, and no `intent_refused` record carries `strategy_callback_unavailable` |
| `signals_consumed_exactly_once` | every row published in time to matter is in the log once, settled once, and no recorded gap is still open |
| `checkpoint_identity_holds` | each sleeve accepts its own newest durable state, at the block's fingerprint, and no boot rewrote the initial checkpoint |
| `sleeve_attribution_agrees` | the sleeves' own inventories add up per symbol to the venue's position |
| `no_opening_before_readiness` | LONG sent nothing before it durably consumed one of its producer's rows |
| `working_entries_settled` | every worked LONG entry is terminal in the log or in flight in the engine |

The last six report "not judged" and pass in quoter mode, which has no producer.

The `cfg(test)` build shortens the engine's confirmation windows, so the simulator's own tests run as an integration test against the library as shipped (`engine/engine-tools/tests/integration/sim.rs`).

`MUTATION_DRAIN_TIMEOUT` is 10 s, and dispatch confirmation has its own deadline. Simulated market time and asynchronous completion scheduling remain distinct; byte-identity comparisons require matching input, configuration and execution conditions.

---

## Invariants

* **Must**: every boot cursor into the callback WAL be the sequence its frame was written at, carried on the replay (`BootReplay::sequence`, `by_sequence`). **Must never**: a record's position in the boot replay be used as a WAL sequence.
* **Must**: iteration that determines WAL records or allocation order use deterministic ordering. Hash lookup tables may serve lookups; a hash seed must never choose the order of durable effects. `engine sim --twice` exercises byte identity.
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
cargo build --manifest-path engine/Cargo.toml --release --locked -p engine-tools --bins
engine/target/release/engine-tools sim --seed 4 --seconds 300 --crashes 1 --faults light --keep --out /tmp/sim --report /tmp/sim/report.json
engine/target/release/engine-tools sim --seed 100 --seeds 24 --faults light --twice

# The funded forward test: the mexc template's own blocks against the producer.
# Two runs of one seed write one log, from the CLI as from the paused-clock
# suite: the Boot record's config identity is taken with the seed's scratch
# directory abstracted out of `operational_profile_path`.
engine/target/release/engine-tools sim --strategies mexc --hours 3 --pump 1.0 \
  --crashes 0 --faults none --keep --out /tmp/sim-mexc --report /tmp/sim-mexc/report.json
engine/target/release/engine-tools sim --strategies mexc --hours 2 --pump 1.0 \
  --seed 1 --seeds 6 --crashes 1 --faults light

# Convert a stopped, complete copied WAL family into a new directory.
engine/target/release/engine-tools wal-convert-v5 \
  --wal /tmp/copied-family/engine.wal --output-dir /tmp/converted-family

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
