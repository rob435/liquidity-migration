# Native strategy module

Every live strategy is a Rust module in `engine-strategies`. A module is a
small decision core behind a thin engine adapter; it is not a daemon, file
publisher, account client, or second risk engine.

## Module shape

```text
src/<strategy>/
├── mod.rs          public exports only
├── plan.rs         typed config, input, state, output, and pure reducer
├── plug.rs         Strategy adapter from engine facts to reducer effects
└── state_import.rs stopped-runtime decoder, only when a takeover format exists
```

Shared mechanics belong in `native_common` or `position_plan` only when two
real strategies use the same rule. Economic rules stay in the owning reducer.
The registry contains runnable strategies, not examples or dormant skeletons.

## Reducer contract

`plan.rs` owns these types:

| Type | Contains |
| --- | --- |
| `StrategyConfig` | exact decision and execution dials, strict validation, stable fingerprint |
| `ReducerInput` | one explicit clock, durable input identity, account and order facts, instrument rules |
| `SleeveState` | all decision memory needed after restart |
| `ReducerOutput` | next state, ordered typed effects, and a compact decision summary |

The reducer is a plain function:

```rust
fn reduce(
    input: ReducerInput,
    prior: SleeveState,
    config: &StrategyConfig,
) -> Result<ReducerOutput, StrategyError>;
```

It reads no clock, file, environment variable, socket, credential, global, or
engine object. Replaying the same typed bytes produces the same state and
effects. Account risk, venue rounding, order IDs, WAL writes, and network I/O
remain engine work.

## Plug contract

`plug.rs` implements `Strategy` and does four jobs:

1. restore and validate the whole-sleeve checkpoint;
2. translate `StrategyCtx` facts into `ReducerInput`;
3. emit the reducer's effects in their declared order; and
4. arm the next reducer deadline.

The boot hook runs once after account recovery and durable state restoration.
It must re-plan open state, re-arm clocks, and acknowledge redelivered inputs
without waiting for fresh market traffic. A reducer error is returned through
`health_error`; a symbol-specific admission reason belongs in
`entry_blockers`.

## Durable effect order

The reducer returns the order required for every crash prefix:

| Relationship | Required order |
| --- | --- |
| source records a cross-sleeve fire | append event, then persist source checkpoint |
| destination applies an event | persist destination checkpoint, then acknowledge event |
| strategy applies an external signal | persist checkpoint, then acknowledge signal |
| state authorizes an order | persist checkpoint and cross its barrier before the order can leave |

An acknowledgement is repeatable. If restart finds an input already reflected
in state but still pending, the reducer emits the acknowledgement again and no
second trading effect.

## Configuration and identity

A registered JSON rule and the public-feature contract are read by Rust.
`engine render-native-config` produces the exact TOML strategy block. Generated
TOML is never maintained by hand.

The decision fingerprint covers every byte that can change a decision. Cadence,
file paths, logging, and retry timing stay outside it. Strategy order is the WAL
identity: a new strategy is appended to the list unless a deliberate WAL
migration changes existing IDs.

## Research contract

Research sends typed JSONL requests to the persistent Rust
`strategy_contract` binary. Rust performs classification, selection, state
transition, sizing, and lifecycle decisions. Python owns historical event
ordering, data joins, modeled fills, accounting, charts, and evidence notes.
It does not reproduce a reducer rule.

Every strategy operation has a checked-in replay fixture with exact discrete
effects, checkpoint bytes, event identities, and missing-value positions.
Continuous output uses one declared tolerance. A fixture is a refactor fence;
it is not evidence of fills or profit.

## Required tests

A runnable strategy includes:

- config rejection and fingerprint tests;
- reducer lifecycle and malformed-input tests;
- crash-prefix tests for each checkpoint/event/signal boundary;
- boot restore with open state and no new market event;
- duplicate and redelivered input tests;
- plug tests for owned positions, working orders, timers, and health errors;
- exact research replay through the persistent Rust contract;
- generated demo and mainnet config checks; and
- registry tests proving stable sleeve names and strategy IDs.

The workspace gate is `cargo fmt`, `cargo check`, `cargo clippy -D warnings`,
`cargo test`, the Python replay tests, and exact config rendering.
