# Execution Specification

## Purpose

Define execution latency qualification, benchmark budgets, and the one-sided counterfactual execution study contract and policies.

## Spec Tables

### 1. Engine Latency Qualification & Budgets

| Property | Status / Evidence |
| :--- | :--- |
| **Deployed Engine** | Generation `76ad13ae`; verified engine and worker after 300 healthy demo seconds. Active images and account readiness live in [STATE.md](../STATE.md). |
| **Latency Qualification** | Pinned run `34128439094` at source `70f4c557`: 1,981 release tests, zero failed, account/history workloads, and all eight latency cells pass. Candidate median decision p99 / submit p50 is 2,000 / 721,150 ns. Candidate submit p99 is 23.22 ms and max 86.05 ms. |
| **Current Baseline** | Clean archived `80db33df4b3113c3c75769f22d10237e7d32d37e` source. |
| **Qualification Target** | Apple M4, 10 CPUs, 16 GiB RAM, macOS 15.7.2 arm64, Rust 1.90.0 release. Local synthetic venue; no funded-service load. |

#### Pinned Latency Budgets

Configured in [docs/execution-latency-budgets.toml](execution-latency-budgets.toml):

| Workload | Decision p50 / p99 / p99.9 | Submit p50 / p99 / max | Dispatch Barrier Obs | Max WAL Bytes |
| :--- | :--- | :--- | :--- | :--- |
| **Unloaded** (100 Hz, 1 symbol) | $\le 25\ \mu\text{s} / 50\ \mu\text{s} / 100\ \mu\text{s}$ | $\le 7.0\ \text{ms} / 10.0\ \text{ms} / 15.0\ \text{ms}$ | $\le 100$ | 250,000 B |
| **Wide** (200 Hz, 270 symbols) | $\le 20\ \mu\text{s} / 50\ \mu\text{s} / 100\ \mu\text{s}$ | $\le 10.0\ \text{ms} / 16.0\ \text{ms} / 25.0\ \text{ms}$ | $\le 600$ | 1,500,000 B |

#### Sustained Workload & Reader Measurements

| Workload | Submits / Opps | Elapsed / CPU s | Max RSS | Decision p50 / p99 / max | Submit p50 / p99 / max |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **60 s, 200 q/s, BTC** | 600 / 600 | 60.84 s / 0.46 s | 11.93 MB | 2.0 / 7.4 / 40.9 µs | 6.30 / 24.31 / 28.33 ms |
| **Unpaced 40k q, 270 names** | 2,000 / 2,000 | 11.16 s / 1.63 s | 18.25 MB | 0.209 / 0.500 / 10.8 µs | 4.98 / 9.51 / 10.26 s |
| **60 s, 200 q/s, 270 names** | 299 / 600 | 60.41 s / 1.44 s | 14.16 MB | 3.5 / 35.2 / 95.4 µs | 5.88 / 14.93 / 21.99 ms |
| **60 s, 200 q/s, 20 ms delay**| 600 / 600 | 60.54 s / 2.07 s | 14.01 MB | 10.3 / 18.5 / 33.0 µs | 26.87 / 31.56 / 44.66 ms |

| Reader Target (1.12 MB WAL) | Time | Peak Python Alloc | Result |
| :--- | :--- | :--- | :--- |
| **Frozen Old Reader** | 5.656 s | 9.41 MB | 4,210 frames; missed current-format orders |
| **New Full Reader** | 0.114 s | 8.61 MB | 4,210 records, sequence & CRC checked; finds 600 orders |
| **Accounting-Only Reader** | 0.103 s | 4.70 MB | 1,203 retained rows; matches full-reader accounting exactly |
| **Native Checksum (1 MiB)** | 0.0132 s | 55 KB | Same CRC `1806706747` (vs 4.951 s in pure Python) |

---

### 2. One-Sided Execution Study

Compares execution costs for actual directional order intentions using recorded books, finite public trades, observed account fees, and explicit queue and latency counterfactuals.

| Component | Contract |
| :--- | :--- |
| **Command** | `engine-tools execution-study --config PATH` |
| **Implementation**| `engine/engine-tools/src/execution_study/`; uses `engine-core::working::plan` |
| **Configuration** | `configs/execution_study_mainnet_v1.json`, schema 1; rolling 48 h; CARRY, LONG, EXODUS; Bybit USDT linear contracts |
| **Order Source** | `/var/lib/liquidity-migration-engine-mainnet/engine.wal`; incremental CRC-checked projection, client/execution IDs, frozen instrument rules |
| **Market Source** | Closed hours under `/var/lib/liquidity-migration/forward-market/YYYY-MM-DD/HH/SYMBOL/`; Bybit L50 books and public trades |
| **Account Rates** | Authenticated `GET /v5/account/fee-rate` per symbol, refreshed every 24 h. Standard Ukrainian VIP-0 table (10/3.6 bp taker/maker; CAP and HEMI 11/4 bp) |
| **Service** | `liquidity-migration-execution-study.service`; downstream timer job on `liquidity-engine-mainnet` |
| **Schedule / Resource** | 120 s after boot; 900 s after completion; 30 s timer accuracy; 600 s timeout; 384 MiB RAM; 1 CPU max |
| **Writes** | `/var/lib/liquidity-migration/execution-study/` only; no WAL or account mutation. Reports `latest.txt`, `latest.json`, `orders/...` |
| **Retention** | `observed.json` resumes frame; `replay-cache.json` preserves comparisons after recorder files expire |
| **Operator Read** | `scripts/ops.sh execution-study [--json]` |

#### Evaluated Execution Policies

| Policy | Hypothetical Behavior |
| :--- | :--- |
| `cross` | Cross finite displayed L50 liquidity at simulated arrival |
| `current` | Original request kind/price and existing native working planner; GTC can take liquidity |
| `passive_entry_30s` | LONG openings join even a 1-tick spread; native GTC working planner, 30 s window, 15 s reprice, at most 1 passive amend, no lean/urgency improvement, then bounded cross and 20 s cross grace. Other sleeves retain `current` behavior |
| `post_only5s`, `post_only30s`, `post_only120s` | Join decision near touch; post-only rejection if marketable on arrival; retry at 5 s intervals; cancel deadline then cross remainder |
| `adaptive120s` | Re-evaluate every 5 s; join, improve 1 tick, or retreat 1 tick using book lean and decayed aggressive flow; cross remainder at 120 s |
| `passive_skip120s` | Join touch; cancel at 120 s; value unfilled quantity at common horizon |

#### Model Terms & Accounting Formulas

| Term / Metric | Formula / Definition |
| :--- | :--- |
| **Decision Clock** | Bridge intent monotonic decision time to WAL core-handled wall time within process epoch. |
| **Arrival Delay** | Observed decision-to-socket delay + 5, 25, 100, or 250 ms hypothetical one-way network hop. |
| **Queue Scenarios** | `trades_only` (displayed queue ahead, no cancel credit); `cancellations_ahead` (infer displayed decreases minus trades and credit ahead). |
| **Common Horizon** | Decision + 180 s; first valid book within 2 s supplies midpoint; unfinished execution unscored. |
| **Price Cost (bp)** | $10^4 \times \sum(\text{side\_sign} \times \text{filled\_qty} \times (\text{fill\_price} - \text{arrival\_mid})) / (\text{requested\_qty} \times \text{arrival\_mid})$ |
| **Fee Cost (bp)** | $10^4 \times \sum(\text{fill\_fee}) / (\text{requested\_qty} \times \text{arrival\_mid})$ |
| **Missed Cost (bp)**| $10^4 \times \text{side\_sign} \times \text{unfilled\_qty} \times (\text{horizon\_mid} - \text{arrival\_mid}) / (\text{requested\_qty} \times \text{arrival\_mid})$ |
| **Total / Saving** | $\text{Price} + \text{Fee} + \text{Missed}$; $\text{saving\_vs\_cross\_bp} = \text{cross\_cost} - \text{candidate\_cost}$. Positive favors candidate. |
| **Crossing Error** | Fully observed market orders vs `cross` at each latency: signed, absolute, and max fill-price error. |
| **Actual All-in Basis** | Divide each cost component by $\sum(\text{filled\_qty} \times \text{order\_arrival\_mid})$ over costed fills $\times 10^4$. |
| **Markouts** | Signed midpoint move from fill price at +1, +15, +60, +300 s (within 2 s tolerance). |

#### Runtime Execution Selection

* **LONG Openings**: Both realms use `WorkPolicy::passive_entry_30s()` PostOnly at the near touch; marketable arrival rejected rather than paying taker.
* **Remainder Crossing**: Cancel, confirm terminal status via REST, reconcile exact fills, then issue IOC cross against fresh book.
* **Restart Recovery**: `OrderSent.dispatch.intent.work` retained in `OpenOrderState.entry_work` across rotation. Boot cancels venue-confirmed remainder without waiting for quote.

---

## Invariants

- **Benchmarking**:
  - Must complete order barriers and verify candidate binaries against both absolute and relative latency limits before deployment.
  - Must report full quantile distributions (p50, p99, p99.9, max); low per-handler decision time does not establish burst tail bounds.
  - Must keep synthetic venue latency fixtures explicit (e.g. 20 ms localhost delay injection is not exchange RTT).
- **Execution Study**:
  - Must report all configured policy/queue/latency cells, including missing observations, partial fills, rejected quotes, and missed moves.
  - Must keep actual fills and hypothetical fills strictly separate; simulated savings do not establish live fill probability.
  - Must treat every order as an independent marginal counterfactual without joint inventory, market response, or quota simulation.
  - Must keep observed features causal; future fills and markouts are evaluation labels, never decision inputs.
  - Must never interpret passive reductions as permission to delay protective exits or strategy deadlines.
  - Must never promote an execution policy on its filled subset's slippage alone.

---

## Operational Recipes

### Execution Study Operations

```bash
# View latest execution study summary and JSON output
scripts/ops.sh execution-study
scripts/ops.sh execution-study --json > /tmp/execution-study-latest.json

# Trigger execution study service on the host
scripts/ops.sh start execution-study.service
scripts/ops.sh logs execution-study.service 40

# Run offline study over copied WAL and tape
engine/target/release/engine-tools execution-study --config /tmp/execution-study-config.json
```

### Verification & Conformance Tests

```bash
# Run execution study unit tests
PATH="$(rustup which --toolchain 1.90.0 cargo | xargs dirname):$PATH" cargo test --manifest-path engine/Cargo.toml -p engine-tools execution_study
PATH="$(rustup which --toolchain 1.90.0 cargo | xargs dirname):$PATH" cargo test --manifest-path engine/Cargo.toml -p engine-venue fee_rate_tests

# Engine simulation and replay verification
cargo test -p engine-core --test fill_costs
```
