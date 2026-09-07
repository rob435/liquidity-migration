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
| R3-07 | WAL record set | Stop writing `FastExecution` (write `OrderUpdate::FastFill`), `StopSet` where `SleeveStopSet` covers it, and `Names` once `IdentityState` is on both realms. Remove the `serde_json::to_value` rewrites in `engine/engine-wal/src/lib.rs` by writing current tags and keeping v1 readers. `reads_back` runs in debug builds only. Delete readers for `segment_base_v2` through `v6` after the quarantined v5 segments are converted or discarded. Binary framing deferred: barriers, not encoding, hold the milliseconds | 58 variants become ≤ 50 with zero unwritten ones; boot parses each host record once | Decided |
| R3-08 | Legacy exit | Give exact terms to the four order constructors that lack them: `engine/engine-core/src/order_dispatch.rs:254`, `engine/engine-core/src/working.rs:184`, `engine/engine-core/src/portfolio_protection.rs:201`, `engine/engine-tools/src/canary.rs:382`. After both realms rotate past the last f64 row, remove the f64 writers, grid adoption and the Python-sleeve import codecs under the conditions in [tier1-audit-round-2.md](tier1-audit-round-2.md) | `grep 'exact_terms: None'` in non-test engine-core is zero; the legacy modules are gone | Decided; mechanical first, removals conditional on rotation |
| R3-06 | Latency budget in CI | The fixed same-worker comparison finds two failures in four runs of the identical baseline archive. Set decision reference to the A-only median run-level p99, 9,300 ns; keep the original submit reference and 1.5× rule. Retain every failure, including B at 18,900 ns above the revised limit | One fresh real qualification passes after the reference commit; doubling either selected histogram fails. No retry-to-green or claim of a universal Linux bound | Decided; fresh qualification pending |
| R3-09 | Rollback across changed runtime source | The completed demo rollback drill covers identical-runtime `a4189a48`/`32858587`. The current `fc2ad99c`/`32858587` pair is refused by the runtime-source equality check; qualify compatible changed-runtime rollback without rewinding WAL/configuration or accepting unsupported formats | The sanctioned drill activates the compatible predecessor and restores current loaded images with fresh account readiness; mainnet stays untouched; unsupported predecessor formats remain refused | Decided; remaining operational acceptance |

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
# Legacy exit (R3-08): zero means every production order carries exact terms.
grep -rn 'exact_terms: None' engine/engine-core/src engine/engine-tools/src --include='*.rs' | grep -v tests | wc -l
```
