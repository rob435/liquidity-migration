# Tier-1 audit, round 3

## Purpose

Define the target execution architecture and the ordered work that follows from two owner decisions of 2026-09-06: strategies are trusted code the owner writes, and the six venue implementations stay for future use without being compiled into the funded binary.

## Spec Tables

### 1. Decisions and their consequences

| Decision | Made by | Consequence |
| --- | --- | --- |
| Strategy code is trusted; the owner writes every strategy file | Owner | Process isolation is not a requirement. The engine runs strategies embedded on the loop thread. Required containment is panic survival; hang and memory containment are not required |
| Six venue implementations are retained for future venues | Owner | Each venue is a Cargo feature; the default build is Bybit only; a venue is enabled only when its conformance suite passes under its feature |
| Every round-3 item, R3-01 through R3-12 | Owner, 2026-09-06 | Accepted in full, including the operational gates in R3-09; §4 records the trade-off taken on each round-2 fork |
| Product target | Owner | A modular execution system: pluggable strategies behind the `Strategy` trait, pluggable venues behind the `VenueGateway`, `MarketFeed` and `OrderFeed` traits, one measured order path |

### 2. The order path: current and target on this box

Measured with `engine/target/release/engine-tools bench --events 2000 --rate 100 --every 20`, macOS, local synthetic venue; `--symbols` takes a comma-separated list of names. VPS figures are recorded in [execution-performance.md](execution-performance.md) once measured there.

| Segment | Today, isolated | Target, embedded | What closes the gap |
| --- | --- | --- | --- |
| Market to decision, p50 | 754 µs | ≤ 10 µs | R3-01: one function call replaces snapshot build, four state (de)serializations, two pipe crossings and four thread handoffs |
| Market to decision, p99 over 270 symbols | ~200 ms ([execution-performance.md](execution-performance.md)) | ≤ 50 µs | R3-01: strategies read `Books` on demand; no per-callback snapshot of every symbol |
| Market to submit result, p50 | 15.6 ms | ≤ 5 ms | R3-03: one durability barrier before the send instead of three (~3.7 ms each on this disk) |
| WAL records between decision and wire | 7 | ≤ 4 | R3-02, R3-07: no snapshot record, no duplicate request copies |
| WAL bytes per changed callback | full market snapshot + state payload | state delta only | R3-02 |
| Execution mode booted by production, bench, sim, backtest | 1 isolated, 3 embedded | 1 | R3-01 |
| Venue crates in the funded binary | 6 adapters, 19 crypto crates | 1 adapter | R3-04 |

### 3. Work items

Status: every item is **Decided** by the owner on 2026-09-06; **Mechanical** marks items that change no behaviour. Acceptance is what the bench, tests or `cargo tree` show afterwards.

| ID | Item | Change | Acceptance | Status |
| --- | --- | --- | --- | --- |
| R3-06 | Enforce the stored latency budget | Keep the fixed paired baseline for Linux attribution and enforce the stored runner-class absolute limits as required by the original row. A relative pass must not permit publication after an absolute median miss | Existing `qualify()` rejects an absolute miss even when the same-worker relative comparison passes; 2× either stored reference fails. Fixed cells, all individual verdicts and archive identity remain intact; the verified eight-cell log also passes the corrected checker | Decided; completion audit reopens absolute enforcement before implementation |
| R3-07 | WAL record set | Stop writing `FastExecution` (write `OrderUpdate::FastFill`), `StopSet` where `SleeveStopSet` covers it, and `Names` once `IdentityState` is on both realms. Remove the `serde_json::to_value` rewrites in `engine/engine-wal/src/lib.rs` by writing current tags and keeping v1 readers. `reads_back` runs in debug builds only. Implement an offline converter that writes a separate family with v5 bases restated as v7, preserving segment/frame sequences, retained source records, exact quantities, current unpriced-lot semantics and retirement state; reset moved callback byte offsets through the existing segment/sequence lookup. Qualify original/converted replay, callback retrieval and archived-order lookup before converting retained families. Delete readers for `segment_base_v2` through `v6` only after every retained host family has a compatible reader or verified conversion. Binary framing deferred: barriers, not encoding, hold the milliseconds | 58 variants become ≤ 50 with zero unwritten ones; boot parses each host record once | Decided |
| R3-08 | Legacy exit | Give exact terms to the four order constructors that lack them: `engine/engine-core/src/order_dispatch.rs:254`, `engine/engine-core/src/working.rs:184`, `engine/engine-core/src/portfolio_protection.rs:201`, `engine/engine-tools/src/canary.rs:382`. Remove Python-sleeve import writers/codecs after both realms verify canonical native state; initialization, verification and persisted provenance remain. Remove f64 writers and grid adoption only after rotation and the retained WAL/rollback conditions in [tier1-audit-round-2.md](tier1-audit-round-2.md) | `grep 'exact_terms: None'` in non-test engine-core is zero; the legacy modules are gone | Decided; native verification permits Python-writer removal; f64 removal remains conditional |
| R3-13 | Mac point latency | Meet the point targets on final source with the unchanged workload and timing/durability boundaries. Retain every fixed-control and before/after cell, including the reader-stage narrow-decision miss | Narrow decision p50 ≤ 10 µs, wide decision p99 ≤ 50 µs, narrow submit p50 ≤ 5 ms; all opportunities complete with one barrier each. Retain every before/after cell and qualify current source | Decided |
| R3-16 | Exact simulation instrument metadata | Preserve source decimal instrument constraints in the existing catalog, expose them from the simulated venue, and boot sim/backtest with the same exact metadata requirement as production. The current tape loader discards decimal strings and both harnesses select optional metadata; fix this independently of archived legacy expiry. Preserve unavailable metadata and disclose the separate binary64 simulated-fill/accounting boundary | Source decimal strings survive into exact order terms; sim/backtest refuse missing required exact metadata; behavioral tests cover order grids, restart and deterministic replay. Record any quantization difference explicitly; no claim that simulated cash/fills become native exact accounting | Decided |
| R3-17 | Independent qualification builds | Build reference and candidate source trees in separate Cargo target directories. The shared target reuses a baseline dependency in the candidate build, so binary hashes alone do not establish candidate source contents | An actual two-source Cargo fixture fails its candidate-value assertion with the old qualifier and passes with isolated targets; fresh hosted qualification builds and measures both intended images. Keep the same compiler, native target, flags, fixed cells and latency limits | Decided |
| R3-18 | Assemble sleeves above paged callbacks | Restore committed inactive-sleeve runtime through the existing paged callback replay. Strategy assembly currently rejects a valid retained queue before the engine can boot it | A portable valid-base regression fails on the prior assembly and passes afterward; all real original/converted quarantine bases boot with identical complete rotation state and venue actions. Preserve committed-state authority and pending callback recovery | Decided |
| R3-19 | Legacy FIFO replay across grid adoption | Reconcile legacy binary64 FIFO full-close quantities with the canonical inventory cut using retained provenance and the existing adoption rules. Exact metadata exposes both emergency FIFO rederivation and later legacy forced-fill failures. Preserve raw economics separately from normalized inventory; deploy compatible allocation readers before enabling the new runtime interpretation | Actual prior-code regressions reproduce the 0.1 full-close excess and pass after repair; native partial quantities remain exact, all seeded clean/light/heavy simulations reconcile and restart, and copied-WAL ownership/accounting remain unchanged except the specified legacy normalization | Decided |
| R3-20 | Deterministic simulation recovery order | Use the engine monotonic clock for portfolio retry deadlines; real `Instant` lets equal virtual inputs choose different emergency/quote order after cancel completion. Preserve delay values and event priority | The retained repeated seed reproduces differing decision/WAL order before the fix; corrected seeded runs reconcile and repeat byte-for-byte, including both crash cuts. No hash-field masking, skipped workload or changed fault rates | Decided |

### 4. Round-2 retained forks: recommendation per row

| Fork | Round 2 kept | Round 3 recommends | Trade-off |
| --- | --- | --- | --- |
| Strategy execution | isolated child processes | embedded (R3-01) | Loses hang and memory containment for strategy code the owner has declared trusted; gains one execution path everywhere and ~750 µs per quote |
| WAL format | framed JSON, seven segment readers | framed JSON, frozen record set, two readers (R3-07) | Keeps human-readable audit trail and every retained segment readable; drops readers for versions no host ever wrote |
| Barriers | three per order | one per order (R3-03) | Same recovery obligations, one sync; recovery code treats the prefix as atomic |
| Supervision | systemd, no `WatchdogSec` | systemd plus `WatchdogSec` (R3-09) | A hung engine is restarted in seconds instead of detected in minutes; costs a 30-line notify writer |
| Grafana renderer and dashboard | kept | owner's call | No performance or correctness bearing |
| Demo probe and disabled maker | kept as template blocks | keep the blocks for identity; the disabled maker subscribes nothing and runs no reducer | Preserves append-only sleeve IDs; removes depth traffic and per-tick work for a sleeve that cannot order |
| Research and rules packages | kept | keep, off the host (R3-12) | Research capability unchanged; trading host carries only what it runs |

### 5. Order of work

| Step | Items | Why this order |
| --- | --- | --- |
| 1 | R3-01, R3-02 | Largest lever; makes bench, sim, backtest and production one engine, which every later measurement depends on |
| 2 | R3-04, R3-08, R3-11, R3-12 | Mechanical and independent; can run in parallel with step 1 |
| 3 | R3-03 | Needs the embedded path so the barrier count is measured where it will run |
| 4 | R3-07, R3-05, R3-06 | Freeze the record set after the write sites have settled; conformance and budget lock the result in |
| 5 | R3-09, R3-10 | Owner decision on gates; tests accumulate from step 1 onward |

Each step ends with the bench recipe below run before and after, and the cell recorded in [execution-performance.md](execution-performance.md).

## Invariants

- Must boot one execution mode in production, bench, sim and backtest; a harness that boots a different mode qualifies nothing.
- Must not compile a venue into the default build unless its conformance suite is green under its feature.
- Must not write market data to the WAL on the callback path; the durable record of a callback is the strategy's state delta.
- Must measure before and after every item in §3 with the recipe below on the same box and record both cells.
- Must keep readers for every record kind and segment version that exists in a retained WAL family on the host; stop writing before deleting readers.
- Must keep this document present tense: a finished item is a deleted row; an improvement found along the way is added as a new R3 row with its acceptance before the work starts.

## Operational Recipes

```sh
# Pinned toolchain for every cargo command below.
export PATH="$(rustup run 1.90.0 rustc --print sysroot)/bin:$PATH"
```

```sh
# Before-and-after cell for any item in §3: unloaded, then 270 symbols at 200 Hz.
cargo build --manifest-path engine/Cargo.toml --release --locked -p engine-tools --bins
engine/target/release/engine-tools bench --events 2000 --rate 100 --every 20 --wal /tmp/r3-unloaded.wal
engine/target/release/engine-tools bench --events 12000 --rate 200 --every 20 \
  --symbols "$(printf 'S%03dUSDT,' $(seq 1 270) | sed 's/,$//')" --wal /tmp/r3-wide.wal
```

```sh
# One execution mode everywhere: this must list production and nothing else once R3-01 lands.
grep -rn 'boot_as_isolated' engine/*/src engine/*/tests
```

```sh
# Venue features (R3-04): default build carries no venue-only cryptography.
cargo tree --manifest-path engine/Cargo.toml -i k256 --prefix depth
cargo build --manifest-path engine/Cargo.toml --release --locked -p engine-core
cargo test --manifest-path engine/Cargo.toml -p engine-venue --features hyperliquid conformance
```

```sh
# Legacy exit (R3-08): inspect each hit's scope; inline test modules share production paths.
rg -n 'exact_terms: None' engine/engine-core/src engine/engine-tools/src --glob '*.rs'
```
