# Tier-1 round handoff

## Purpose

The one document for the engine audit round: what is implemented on this tree, what the gate verifies on it, and what remains open for whoever continues.

## Spec Tables

### State

| Item | Value |
| --- | --- |
| Tree | Local `main`, the commit carrying this document: Codex's shared-sleeve checkpoint, Claude's tier-0 batch and the on-call routine's signal-worker fixes, merged |
| Authority | Local implementation, tests and merges. Funded deployment, capital, credentials and live-state changes need the owner: [STATE.md](../STATE.md), [operations.md](operations.md) |
| Evidence | The tests named below, run with the recipes at the end; dated receipts in [CHANGELOG.md](../CHANGELOG.md). There is no archive tree |
| Toolchain | Rust 1.90.0 from `rust-toolchain.toml` on `aarch64-apple-darwin`; the repository `.venv` for Python. Homebrew cargo ignores the pin |
| Related specs | [engine.md](engine.md) §3 exit classes, §10 `engine sim`; [architecture.md](architecture.md) |

### Implemented

| Area | Behaviour | Source |
| --- | --- | --- |
| Shared sleeves | Configured sleeve keys map to durable IDs; independent same-ticker and opposing inventory, logical stops and exact execution accounting; portfolio risk translates virtual intent to physical direction | `engine/engine-core/src/portfolio_protection.rs`, `engine/engine-core/src/identities.rs`, `engine/engine-types/src/portfolio.rs`, `engine/engine-risk/src/kernel/portfolio.rs` |
| Durable exits | Exact remaining target survives refusal; fair service, paced retries and durable emergency phases; native net closure precedes internal offset settlement | `engine/engine-core/src/portfolio_control.rs`, `engine/engine-core/src/engine/portfolio_runtime.rs`, `engine/engine-core/src/engine/intent_admission.rs` |
| Aggregate parent | Engine-owned reduction combines fragments below individual venue minimums; canonical quantity survives decimal projection and respects market maximum chunks; partial fills and fees allocate once in stable sleeve-key order | `engine/engine-types/src/orders.rs`, `engine/engine-types/src/order_terms.rs`, `engine/engine-core/src/portfolio_allocation.rs`, `engine/engine-core/src/attribution/allocated.rs`, `engine/engine-core/src/reconcile.rs` |
| Stops and risk | Per-sleeve stop ownership, exact native terms, durable asynchronous stop mutation, replacement before cancellation; causal margin reservations in an ordered book and exact order remainder | `engine/engine-core/src/engine/stop_runtime.rs`, `engine/engine-risk/src/margin.rs`, `engine/engine-venue/src/account_stops.rs`, `engine/engine-strategies/src/quoter/plug.rs` |
| Recovery | Independent account and history clients; causal account results, timeout, retry ownership, 32 history rows per service turn and asynchronous checkpoint durability; recovered callback ownership is atomic | `engine/engine-core/src/engine/account_recovery.rs`, `engine/engine-core/src/engine/history_recovery.rs`, `engine/engine-core/src/engine/boot_recovery.rs`, `engine/engine-venue/src/account_recovery.rs` |
| Simulated recovery | Backtest, HTTP benchmark and process benchmark adapters implement independent reads with timing and account semantics; the simulated venue's recovery client serves the venue's own fill history | `engine/engine-core/src/backtest/venue.rs`, `engine/engine-core/src/bench.rs`, `engine/engine-core/src/account_state_bench.rs`, `engine/engine-core/tests/integration/strategy_process.rs` |
| Callback lifecycle | Registered child processes, bounded protocol and state, paged inactive payloads, durable per-sleeve source frontiers and delivered-input markers; unresolved callbacks retain WAL sources | `engine/engine-core/src/strategy_process/`, `engine/engine-wal/src/callback_reader.rs` |
| Inputs and instruments | Durable producer epochs and terminal consumption; append-only identities, durable metadata and canonical inventory-driven Quote/Depth leases, including inactive net-zero holdings | `engine/engine-core/src/engine/portfolio_routes.rs`, `engine/engine-core/src/engine/symbol_admission.rs`, `engine/engine-venue/src/catalog_checkpoint.rs`, `engine/signal-worker/src/worker/identities.rs` |
| Analytic quantity | Exact lot quantity prevents premature closure and lost tiny holdings; malformed unallocated emergency parents have no fabricated sleeve owner | `engine/engine-core/src/execution.rs`, `engine/engine-core/src/execution/roundtrip.rs`, `engine/engine-core/src/execution/tests.rs` |
| Deterministic core | Anything the engine iterates that reaches the log is ordered (`BTreeMap`); one seed replays byte for byte under `engine sim --twice` | `engine/engine-core/src/sim/`, `engine/engine-risk/src/margin.rs`, `engine/engine-core/tests/integration/sim.rs` |
| Fault simulator | The live loop on a seeded synthetic market against the simulated venue, with venue refusals, lost requests and replies, dropped and duplicated private updates, feed resets and process deaths; eight invariants judge each run | `engine/engine-core/src/sim/harness.rs`, `engine/engine-core/src/sim/faults.rs`, `engine/engine-core/src/sim/market.rs`, `engine/engine-core/src/sim/invariants.rs`; [engine.md](engine.md) §10 |
| Exit classes | `EngineError::Wal`, `Venue`, `Boot`, `TaskStopped`, `TimedOut`, `Reconcile`, `State`; the supervisor restarts on any of them and the class says what a restart can settle | `engine/engine-core/src/engine.rs`; [engine.md](engine.md) §3 |
| Market events by reference | `on_market` and `on_market_feed` borrow the 1,648-byte `MarketEvent`; the market-turn future is 4,624 bytes | `engine/engine-core/src/engine/scheduling.rs`, `engine/engine-core/src/engine.rs` |
| One symbol lookup | Every adapter resolves and interns symbol IDs through `engine_public::symbols`; Variational holds a `SymbolCatalog` like the other gateways | `engine/engine-public/src/symbols.rs`, `engine/engine-marketdata/src/symbols.rs`, `engine/engine-venue/src/venues/variational/gateway.rs` |
| Paused-clock tests | `engine-core` tokio tests start with the clock paused; the tests on the wall clock are the ones with real sockets or engine timer waits, and the module doc names them | `engine/engine-core/src/tests.rs`, `engine/engine-core/Cargo.toml` (`tokio/test-util`) |
| Lint deny table | `or_fun_call`, `redundant_clone`, `format_push_string` and `large_types_passed_by_value` are denied workspace-wide with zero sites; `needless_pass_by_value` stays at its default | `engine/Cargo.toml` `[workspace.lints.clippy]` |
| One test binary per crate | Integration tests compile as `tests/<name>/main.rs` per crate; a new test file is a `mod` line there. `engine-risk` sets `autotests = false` and silently drops a stray file | `engine/engine-core/tests/integration/main.rs`, `engine/engine-risk/tests/contracts/main.rs`, `engine/engine-venue/tests/venue/main.rs`, `engine/engine-wal/tests/wal/main.rs` |
| Settled funding identity | A settled funding row is (symbol, settlement, rate); `funding_interval_min` is instrument metadata stamped at fetch time, kept as first observed, never part of the rewrite check. Rewrite errors name the symbol | `engine/signal-worker/src/history.rs`, `engine/signal-worker/src/live.rs` |
| Stream continuity | A universe refresh's replacement stream carries the outgoing stream's epoch, gap flag and stamp, reconnect and fault counts through `StreamContinuity` | `engine/signal-worker/src/bybit_ws.rs`, `engine/signal-worker/src/live.rs` |
| Halt cancel settlement | An opening order an account-level halt pulls has 5 s from the first cancel reply to end. A cancel refused or unanswered, or accepted and unconfirmed by the private stream for half that window, is followed by a status read on the shared order-lookup lane: a working order is cancelled again, an ended one with every fill in the log is recorded as ended, one with unseen fills triggers execution-history recovery first. The run ends with `Reconcile` only when the window closes on a live order | `engine/engine-core/src/engine/scheduling.rs` (`HaltCancelState`, `apply_halt_lookup`), `engine/engine-core/src/engine/venue_completion.rs`, `engine/engine-core/src/tests/halt_cancels.rs` |
| Simulator clock steps | The idle pump moves the clock one tape row or one waiter at a time; a loop turn that is busy in a `select!` branch ahead of the market feed can no longer be mistaken for an idle loop and carried whole seconds forward. The hiccup pause after a feed error is on the loop's timer, so it is virtual under the simulator | `engine/engine-core/src/backtest/feed.rs`, `engine/engine-core/src/engine.rs` (`HICCUP_PAUSE`) |

### Verification on this tree

| Check | Result | Scope |
| --- | --- | --- |
| `cargo fmt --all -- --check` | clean | Rust 1.90.0 |
| `cargo clippy --workspace --all-targets --locked -- -D warnings` | clean | every crate, the deny table included |
| `cargo test --workspace --all-targets --locked --no-fail-fast` | 27 binaries: 2,207 passed, 0 failed, 5 ignored | debug profile after `cargo clean`; the ignores are live-socket feed tests and the Linux opt-ins of resume item 3 |
| `cargo test --workspace --doc --locked` | no doctests in the workspace; every doctest target is empty | |
| `scripts/dev.sh check`, Python half | doctor ready; Ruff, ShellCheck and mypy over 100 files clean; 1,517 pytest passed | repository `.venv` |
| `engine sim`, faultless seed 1, light seeds 1–6, heavy seed 7 and heavy seeds 1–40, all `--twice` | every check holds and every replay is identical; seed 7 heavy has no reconciliation exit (nine before this tree); the heavy sweep has none on any of its 40 seeds, and its 19 restarts on 15 seeds are all boots whose simulated account read failed | debug binary; 300 s tapes, 2 symbols, two deaths per heavy seed |
| Release-profile suites and Linux resource/process opt-in tests | not run on this tree | resume items 2 and 3 |

### Open findings

| Finding | Reproduction | Decision needed |
| --- | --- | --- |
| Isolated strategy processes on the fleet | Deploy `80dc5c69` (run `33996136208`): both engines wrote `strategy_callback_queued`, `strategy_callback_prepared` and `strategy_process_transition_queued` for every quote, ~180 KB each, 15 MB/s per realm, and logged `strategy callback has a prior durable input` at ERROR 40 times a second; demo reached its 2 GiB memory cap in four minutes. Rolled back to `cece1d9f` ([CHANGELOG.md](../CHANGELOG.md) 2026-09-05 22:50 UTC) | Owner: boot production embedded (`Engine::boot_as`) until the isolated path coalesces quotes and stops persisting the runtime per callback, or rework the isolated path first. No engine deploy from this tree until then |
| History checkpoint on an empty page | `history_recovery` advances the history checkpoint when the recovery client's `executions` page is empty for the window. The simulator's client serves the venue's history so the simulator no longer hides it; a live endpoint answering empty for a window loses the fill the same way | Whether an empty window may advance the checkpoint |

### Remaining boundaries

| Boundary | Current limit |
| --- | --- |
| General sleeve exits | Ordinary intent quantities pass through `f64`; canonical emergency-parent sizing alone does not close this boundary |
| Account values | Account quantity and equity keep explicit legacy binary64 projections; exact execution accounting does not make every risk calculation exact |
| Analytic money | Analytic monetary outputs are float projections; exact per-asset accounting is separate |
| History memory | Application yields after 32 rows; aggregate execution-history response allocation is not globally bounded |
| Retry timing | Monotonic backoff is runtime-only; a restart permits one immediate retry while keeping durable obligations and ambiguous-send ownership |
| Allocation policy | Versioned `EmergencyNetFifo` means stable sleeve-key contributor ordering, not arrival-time FIFO; replay semantics must not change silently |
| Recovery availability | Unsupported Binance history, unknown manual fills, missing legacy cost and asset values and erased producer tails stay explicit |
| Simulator load sensitivity | The engine's dispatch and drain deadlines read the wall clock, so `engine-core/tests/integration/sim.rs` runs its seeds one at a time |
| Process resources | Linux has OS memory, CPU, FD and fork limits; macOS has process, protocol and time limits without equivalent OS memory or fork guarantees |
| WAL source retention | External pruning must preserve segments referenced by unresolved callback cursors |
| Operational evidence | No live venue or account parity, funded deployment or production-readiness claim follows from this tree |

### Resume order

| Order | Work | Completion evidence |
| --- | --- | --- |
| 1 | Decide the empty-page finding above; fix it in code and prove the fix fails without it | The empty-page case goes green on the same recipe |
| 2 | Release-profile suites and doctests on this tree | `cargo test --workspace --all-targets --release --locked` and `--doc --release` |
| 3 | Linux resource/process and worker overload opt-in tests | Explicit workload results on a Linux host; ordinary-suite ignores preserved |
| 4 | Aggregate-parent rejection, restart and failure cuts; exact target sizing for general sleeve exits | A failing mutation and the restored passing behaviour per fix |
| 5 | Global recovery-response memory and remaining projected account and risk arithmetic | Tested root-cause changes or an exact remaining boundary |
| 6 | Decide the isolated-strategy-process finding above; until then the fleet stays on `cece1d9f` | [operations.md](operations.md) recipe, [STATE.md](../STATE.md) change point |

## Invariants

- Must preserve unrelated work, protective stops, reductions, reconciliation, exact allocation ownership and explicit legacy WAL handling.
- Must prove every bug fix fails without its fix and passes with it; unchanged happy paths alone are insufficient.
- Must keep the log independent of hash order: anything the engine iterates that reaches the log is a `BTreeMap`, and `engine sim --twice` is the check.
- Must never soften a failing simulator check. A real defect is reported with its seed; a simulator gap is fixed in the simulator.
- Must keep `engine-core` tokio tests on the paused clock except real sockets and engine timer waits, named in `engine/engine-core/src/tests.rs`.
- Must run the gate on the pinned toolchain; Homebrew cargo ignores `rust-toolchain.toml` and its clippy differs.
- Must add a new integration test file to its crate's `tests/<name>/main.rs`; `engine-risk` drops a stray file silently.
- Must never deploy, arm, alter credentials or capital, or touch live state from this document's authority; deploys go through `scripts/ops.sh deploy` on the owner's word.

## Operational Recipes

```sh
# The gate, on the pinned toolchain. Run from the repository root.
SYSROOT="$(rustup run 1.90.0 rustc --print sysroot)"; export PATH="$SYSROOT/bin:$PATH"
(cd engine && cargo fmt --all -- --check)
(cd engine && cargo clippy --workspace --all-targets --locked -- -D warnings)
(cd engine && cargo test --workspace --all-targets --locked)
(cd engine && cargo test --workspace --doc --locked)
scripts/dev.sh check
```

```sh
# Determinism and faults: a faultless seed, the light sweep, the heavy seed, and a heavy sweep.
cd engine
cargo run --release --locked -- sim --seed 1 --seconds 300 --symbols 2 --crashes 0 --faults none --twice
cargo run --release --locked -- sim --seed 1 --seeds 6 --seconds 300 --symbols 2 --twice
cargo run --release --locked -- sim --seed 7 --seconds 300 --symbols 2 --crashes 2 --faults heavy --twice
cargo run --release --locked -- sim --seed 1 --seeds 40 --seconds 300 --symbols 2 --crashes 2 --faults heavy --keep --out /tmp/sim-heavy
```

```sh
# The heavy-seed test and the halt-cancel tests, alone.
cd engine
cargo test -p engine-core --test integration --locked one_seed_replays_byte_for_byte_under_heavy_faults
cargo test -p engine-core --lib --locked halt_cancels
```
