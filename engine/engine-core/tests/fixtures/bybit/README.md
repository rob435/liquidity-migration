# Bybit private boundary fixtures

## Purpose

Exercise Bybit private decoding and engine accounting with synthetic protocol frames.

## Spec Tables

| File | Source | Runtime substitution |
| --- | --- | --- |
| `execution.json` | Constructed from the [execution schema](https://bybit-exchange.github.io/docs/v5/websocket/private/execution) | Client order ID, execution ID, quantity, venue timestamp |
| `position.json` | Constructed from the [position schema](https://bybit-exchange.github.io/docs/v5/websocket/private/position) | None |

## Invariants

- Must describe these as synthetic fixtures; they are not captured account traffic.
- Must account executions once across cancel races and duplicate private delivery.
- Must not turn position snapshots into executions.

## Operational Recipes

```sh
cargo test --manifest-path engine/Cargo.toml -p engine-core --lib tests::venue_boundary
```
