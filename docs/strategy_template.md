# Strategy plug contract

## Purpose

Define how a registered Rust strategy consumes engine facts, emits durable effects, and satisfies the shared plug conformance suite.

## Spec tables

| Path | Responsibility |
| --- | --- |
| `engine/engine-strategies/src/lib.rs::PLUGS` | Authoritative plug names and builders |
| `engine/engine-types/src/strategy.rs::Strategy` | Callback, checkpoint, subscription, and state-import contracts |
| `engine/engine-strategies/src/<name>/plan.rs` | Pure reducer and validated typed configuration |
| `engine/engine-strategies/src/<name>/plug.rs` | Translate engine facts to reducer inputs and ordered `Action` values |
| `engine/engine-strategies/src/<name>/state_import.rs` | Optional decoder for an existing runtime's checkpoint |
| `engine/engine-strategies/src/mock_ctx.rs` | Deterministic market, account, checkpoint, action, and timer context |
| `engine/engine-strategies/src/conformance.rs` | Shared definition of a conforming registered plug |
| `engine/engine-strategies/tests/fixtures/plug-events.jsonl` | Frozen synthetic event stream; no live-account or profitability evidence |

| Contract | Required behavior |
| --- | --- |
| Execution | `Strategy::on_event` runs embedded on the engine loop; `catch_unwind` faults a panicking sleeve and queues its live-order cancellations |
| Inputs | Market, account, order, clock, and sleeve ownership facts come from `StrategyCtx` |
| State | Whole-sleeve checkpoints have a schema version, decision fingerprint, validated canonical payload, and `MAX_STRATEGY_STATE_BYTES` bound |
| Durability | Changed checkpoints and ordered effects enter a durable strategy transition; the dispatch barrier covers its order attempt before venue submission |
| Unchanged state | An identical checkpoint emits no additional callback WAL record |
| External signals / cross-sleeve events | Reducer state and consumption effects preserve their durable order; duplicate input identity cannot create another opening |
| Retained private state | `runtime_state` and `runtime::restore` preserve existing callback WAL runtime payloads; current callbacks persist checkpoint actions |
| Opening ownership | Another sleeve's attributed exposure or live opening order makes `foreign_position` true; core admission rejects conflicting opening ownership |
| Own reductions | Attributed quantity remains available for exits and tighter stops when another sleeve also has exposure |

| Conformance check | Coverage |
| --- | --- |
| Recorded replay | Every `PLUGS` builder receives the same fixed event stream twice; emitted action and armed-timer bytes match exactly |
| Restore | Nonempty native checkpoints survive two fresh restores; all registered retained-runtime payloads round-trip byte-identically |
| Seeded event corpus | Four reproducible seeds, 512 callbacks per seed and plug, covering market resets, quote/depth/trades, timers, rejected signals/orders, refusals, permissions, and flatten requests |
| State bound | Every corpus callback checks retained private state size; every emitted checkpoint validates and round-trips within the payload bound |
| New registration | A `PLUGS` entry without a conformance parameter/state fixture fails the shared suite |

## Invariants

- Strategies must use engine-provided time and facts; reducers must never perform network, filesystem, credential, or wall-clock I/O.
- Equal configuration, state, context, and event bytes must produce equal ordered action bytes.
- A checkpoint must contain the complete durable sleeve state required after restart; its validator must reject incompatible schema, fingerprint, and invalid numerical state.
- Strategies must preserve owned exits, input acknowledgements, and effect order across restart and repeated delivery.
- The fixed corpus must remain reproducible; it is bounded coverage, not a claim that every possible event sequence is panic-free.
- Strategy-specific numerical, duplicate-input, open-position restart, and execution-accounting tests must accompany the shared harness where those behaviors apply.

## Operational recipes

```sh
cd engine
cargo +1.90.0 test -p engine-strategies conformance --locked -- --nocapture
cargo +1.90.0 test -p engine-strategies --locked
```
