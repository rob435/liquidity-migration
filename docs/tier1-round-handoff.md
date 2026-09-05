# Tier-1 restart handoff

## Purpose

Resume the paused local audit implementation without confusing targeted regression evidence with complete qualification.

## Spec Tables

| State | Value |
| --- | --- |
| Work | Paused at the owner's request; the complete audit remains open |
| Branch | Local `main`; checkpoint is the commit containing this handoff |
| Evidence | [Round archive index](tier1-round-evidence.json), [portfolio progress](tier1-portfolio-progress.json), [canonical audit](tier1-audit.md) |
| Previous foundation | `efb658b3ff62395d5501100b94b2e64e458ccbad`; its passing suites do not qualify this checkpoint |
| Authority | Local redesign, shared/opposing same-ticker sleeves, regression tests and local checkpoints; no funded deployment, capital, credentials or live-state changes |
| Qualification | Final formatting and strict workspace/all-target Clippy pass; final integrated debug/release/developer suites remain outstanding |
| Evidence interpretation | Archives retain failed diagnostics as well as valid semantic failures and passing checks; agent manifests state source and mutation boundaries |

| Area | Current implementation | Principal source ownership | Evidence inside archives |
| --- | --- | --- | --- |
| Shared sleeves | Configured sleeve keys map to durable IDs; independent same-ticker and opposing inventory, logical stops and exact execution accounting; portfolio risk translates virtual intent to physical direction | `engine/engine-core/src/portfolio_protection.rs`, `engine/engine-core/src/identities.rs`, `engine/engine-types/src/portfolio.rs`, `engine/engine-risk/src/kernel/portfolio.rs` | Root shared/portfolio logs; identities proof manifests |
| Durable exits | Exact remaining target survives refusal; fair service, paced retries and durable emergency phases; native net closure precedes internal offset settlement | `engine/engine-core/src/portfolio_control.rs`, `engine/engine-core/src/engine/portfolio_runtime.rs`, `engine/engine-core/src/engine/intent_admission.rs` | Root retained-exit, fairness, frozen-owner, causal-settlement and offset-replay proofs |
| Aggregate parent | Engine-owned reduction combines fragments below individual venue minimums; canonical quantity survives decimal projection and respects market maximum chunks; actual partial fills/fees allocate once in stable sleeve-key order | `engine/engine-types/src/orders.rs`, `engine/engine-types/src/order_terms.rs`, `engine/engine-core/src/portfolio_allocation.rs`, `engine/engine-core/src/attribution/allocated.rs`, `engine/engine-core/src/reconcile.rs` | Root fragmented-emergency, aggregate-wire-partials, emergency-quantity and frozen-owner logs |
| Stops and risk | Per-sleeve stop ownership, exact native terms, durable asynchronous stop mutation, replacement before cancellation; causal margin reservations and exact order remainder | `engine/engine-core/src/engine/stop_runtime.rs`, `engine/engine-risk/src/margin.rs`, `engine/engine-venue/src/account_stops.rs`, `engine/engine-strategies/src/quoter/plug.rs` | Venue stop/remainder, risk/catalog and account-stop/analytics manifests |
| Recovery | Independent account/history clients; causal account results, timeout, retry ownership, 32 history rows per service turn and asynchronous checkpoint durability; recovered callback ownership is atomic | `engine/engine-core/src/engine/account_recovery.rs`, `engine/engine-core/src/engine/history_recovery.rs`, `engine/engine-core/src/engine/boot_recovery.rs`, `engine/engine-venue/src/account_recovery.rs` | Effects recovery manifest; recovery-client and benchmark manifests |
| Simulated recovery | Backtest, HTTP benchmark and process benchmark adapters implement independent reads while retaining timing/account semantics | `engine/engine-core/src/backtest/venue.rs`, `engine/engine-core/src/bench.rs`, `engine/engine-core/src/account_state_bench.rs`, `engine/engine-core/tests/strategy_process.rs` | Root unchanged deterministic backtest fails before and passes after; benchmark three failures and three passes |
| Callback lifecycle | Registered child processes, bounded protocol/state, paged inactive payloads, durable per-sleeve source frontiers and delivered-input markers; unresolved callbacks retain WAL sources | `engine/engine-core/src/strategy_process/`, `engine/engine-wal/src/callback_reader.rs` | Effects paging/recovery manifests, process and compatibility logs |
| Inputs and instruments | Durable producer epochs and terminal consumption; append-only identities, durable metadata and canonical inventory-driven Quote/Depth leases, including inactive net-zero holdings | `engine/engine-core/src/engine/portfolio_routes.rs`, `engine/engine-core/src/engine/symbol_admission.rs`, `engine/engine-venue/src/catalog_checkpoint.rs`, `engine/signal-worker/src/worker/identities.rs` | Identities, physical, portfolio-route and recovery-cache manifests |
| Analytic quantity | Exact lot quantity prevents premature closure and lost tiny holdings; malformed unallocated emergency parents have no fabricated sleeve owner | `engine/engine-core/src/execution.rs`, `engine/engine-core/src/execution/roundtrip.rs`, `engine/engine-core/src/execution/tests.rs` | Venue account-stop/analytics manifest |

| Verification boundary | Result | Limit |
| --- | --- | --- |
| Frozen combined source | Root formatting and strict workspace/all-target Clippy pass | Runtime suites below are separately scoped |
| Venue adapter graph | 602 debug and 602 release tests pass at the final venue boundary | Does not qualify the full final workspace |
| Recovery and processes | 36 affected recovery tests and 10 registered process integration tests pass | Full resource/overload qualification remains open |
| Routes and physical replay | 10 route, 18 gap-recovery and 30 physical rotation tests pass at their boundaries | Final integrated graph still needs execution |
| Root integration | Frozen source: 4 shared-sleeve, 8 portfolio-runtime, 1 recovered-parent restart and 1 history-batch restart tests pass; unchanged deterministic backtest passes after its recovery fix | Full failure matrix remains required |
| WAL compatibility | Current reader accepts new recovered ownership; actual previous v4 reader refuses it without truncation; torn-frame recovery is exercised | New-format WAL is not downgrade-readable; legacy reads remain covered separately |
| Intermediate broad diagnostic | Contains failures and an interrupted obsolete history test; retained in the root archive | Several causes have targeted fixes, but no subsequent final broad green run exists |

| Resume order | Required work | Completion evidence |
| --- | --- | --- |
| 1 | Run final integrated workspace debug/release all-targets and doctests, then `scripts/dev.sh check`; investigate every failure without removing assertions | Same frozen source, pinned compiler, complete logs and source hashes |
| 2 | Exercise existing opt-in Linux resource/process and worker overload/outage tests; rerun affected suites if fixes follow | Final Linux source and explicit workload results; preserve existing ordinary-suite ignores |
| 3 | Complete aggregate-parent rejection/restart/failure cuts and exact target sizing for general sleeve exits | Semantic failing mutation and restored passing behavior for each additional bug fix |
| 4 | Review global recovery-response memory and remaining projected account/risk arithmetic | Tested root-cause changes or an exact remaining boundary |
| 5 | Rewrite each canonical finding against final source and consolidate evidence | Current resolution, tests and limitations for every finding; no cosmetic architecture claims |

| Remaining boundary | Current limit |
| --- | --- |
| General sleeve exits | Ordinary intent quantities still pass through `f64`; canonical emergency-parent sizing alone does not close this boundary |
| Account values | Account quantity/equity retain explicit legacy binary64 projections; exact execution/accounting and physical WAL totals do not make every risk calculation exact |
| Analytic money | Analytic monetary outputs remain float projections; exact per-asset accounting is separate |
| History memory | Application yields after 32 rows; aggregate execution-history response allocation is not globally bounded |
| Retry timing | Monotonic backoff is runtime-only; restart permits one immediate retry while retaining durable obligations and ambiguous-send ownership |
| Allocation policy | Versioned `EmergencyNetFifo` means stable sleeve-key contributor ordering, not arrival-time FIFO; do not silently change replay semantics |
| Recovery availability | Unsupported Binance history, unknown manual fills, missing legacy cost/assets and erased producer tails remain explicit |
| Process resources | Linux has OS memory/CPU/FD/fork limits; macOS has process/protocol/time limits without equivalent OS memory/fork guarantees |
| WAL source retention | External pruning must preserve segments referenced by unresolved callback cursors |
| Operational evidence | No live venue/account parity, funded deployment or production-readiness claim |
| Local Linux guest | Task-created `audit-linux` guest is stopped; its files and build target remain available |

## Invariants

- Must preserve unrelated work, protective stops, reductions, reconciliation, exact allocation ownership and explicit legacy WAL handling.
- Must prove every additional bug fix fails without its fix and passes with it; unchanged happy paths alone are insufficient.
- Must keep the audit incomplete until final implementation, failure coverage and integrated suites support every resolution.
- Must treat archived source hashes as the evidence boundary; formatting and later integration can change hashes after individual agent checks.
- Must touch extracted Rust sources before reusing a Cargo target for an isolated snapshot proof; stale mtimes can reuse unrelated compiled artifacts.
- Must never push or deploy funded changes, alter credentials/capital or touch live state under this local implementation authority.

## Operational Recipes

```sh
cd /Users/jhbvdnsbkvnsd/Desktop/liquidity-migration
git status --short
git log -3 --oneline

audit_rust_bin="$(dirname "$(rustup which --toolchain 1.90.0 rustc)")"
export PATH="$audit_rust_bin:$PATH"
export RUSTC="$audit_rust_bin/rustc" RUSTDOC="$audit_rust_bin/rustdoc"
export CARGO_INCREMENTAL=0

cargo fmt --manifest-path engine/Cargo.toml --all -- --check
cargo clippy --manifest-path engine/Cargo.toml --workspace --all-targets --locked --offline -j 2 -- -D warnings
cargo test --manifest-path engine/Cargo.toml --workspace --all-targets --locked --offline -j 2
cargo test --manifest-path engine/Cargo.toml --workspace --doc --locked --offline -j 2
cargo test --manifest-path engine/Cargo.toml --workspace --all-targets --release --locked --offline -j 2
cargo test --manifest-path engine/Cargo.toml --workspace --doc --release --locked --offline -j 2
scripts/dev.sh check
```

```sh
mkdir -p /tmp/tier1-round-evidence-restored
for archive in docs/evidence/tier1-round/*.tar.gz; do
  tar -xzf "$archive" -C /tmp/tier1-round-evidence-restored
done
# Each archive keeps its original /tmp directory name and includes member hashes.
find /tmp/tier1-round-evidence-restored -type f -name '*.rs' -exec touch {} +
```

```sh
LIMA_HOME=/tmp/tier1-next-effects/lima /opt/homebrew/bin/limactl start audit-linux
LIMA_HOME=/tmp/tier1-next-effects/lima /opt/homebrew/bin/limactl shell audit-linux bash -lc 'rustc --version'
```
