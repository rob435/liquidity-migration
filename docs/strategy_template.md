# Native Strategy Implementation Contract

Architecture and developer contract for implementing new trading strategies within `engine-strategies`.

---

## 1. Module Architecture

New strategy implementations reside in `engine/engine-strategies/src/<strategy_name>/`:

| File | Role | Constraints |
| :--- | :--- | :--- |
| `mod.rs` | Public API surface | Re-exports plug and config types only. |
| `plan.rs` | Pure decision reducer | **Pure logic only**. Zero I/O, no network, no clock reads, no credentials. |
| `plug.rs` | Engine adapter | Implements `Strategy` trait; converts engine facts to reducer inputs and orders. |
| `state_import.rs`| Takeover decoder | Implements legacy state import (optional). |

---

## 2. Pure Reducer Contract

Every strategy reducer is a pure mathematical state transition function:

```rust
pub fn reduce(
    input: ReducerInput,
    prior: SleeveState,
    config: &StrategyConfig,
) -> Result<ReducerOutput, StrategyError>;
```

### Core Type Contracts
* **`StrategyConfig`**: Immutable strategy dials with strict validation and deterministic hash fingerprint.
* **`ReducerInput`**: External facts provided by engine: current timestamp, order book facts, attributed fills, active rules.
* **`SleeveState`**: Minimal persistent state required to resume execution after process crash.
* **`ReducerOutput`**: Next checkpoint state, ordered target positions, and durable cross-sleeve events.

---

## 3. Durability & Ordering Invariants

Reducers must enforce deterministic ordering across process crash boundaries:

| Event Boundary | Durability Sequence |
| :--- | :--- |
| **External Signal** | Append observation to WAL $\to$ Persist sleeve checkpoint $\to$ Acknowledge signal. |
| **Order Dispatch** | Persist checkpoint $\to$ Write WAL `OrderSent` record $\to$ Transmit order bytes over socket. |
| **Cross-Sleeve Fire** | Append cross-sleeve event to WAL $\to$ Persist emitting checkpoint $\to$ Deliver event. |
| **Event Consumption**| Persist consuming checkpoint $\to$ Acknowledge event receipt. |

---

## 4. Required Test Matrix

Before a strategy can be registered in production, it must implement:
1. **Determinism Test**: Replaying identical input bytes over identical state yields bit-for-bit identical outputs.
2. **Crash-Prefix Audit**: Replaying truncated WAL frames never causes duplicate orders or position desynchronization.
3. **Boot State Recovery**: Strategy recovers open positions and timers from cold start without incoming market ticks.
4. **Idempotent Acknowledgment**: Duplicate signals or repeated event deliveries produce no additional orders.
