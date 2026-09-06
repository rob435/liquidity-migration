# Tier-1 implementation and qualification

## Purpose

Define the completed Tier-1 ownership model, its local qualification evidence and the operational boundaries of the candidate engine.

## Spec Tables

| Contract | Authority |
| --- | --- |
| Accepted findings | [tier1-audit.md](tier1-audit.md): all 56 accepted IDs, implementation references and retained decisions |
| Qualification | [tier1-round-evidence.json](tier1-round-evidence.json): commands, counts, source hashes, assertion failures and restored passes |
| Deployment supplement | [tier1-deployment-evidence.json](tier1-deployment-evidence.json): integrated deployment repairs, retirement rehearsal and host observations; the original qualification remains bound to its recorded source hashes |
| Runtime design | [engine.md](engine.md); implemented code and tests take precedence |
| Deployment state | [STATE.md](../STATE.md); local qualification does not update the funded host |
| History | [CHANGELOG.md](../CHANGELOG.md); accepted baseline evidence remains in Git |
| Toolchain | Rust 1.90.0, with `PATH`, `RUSTC` and `RUSTDOC` pointing to its sysroot; repository `.venv` for Python |

| Owner | Implemented behavior | Main source |
| --- | --- | --- |
| Strategy callbacks | Market inputs coalesce while a callback is pending. Unchanged state/checkpoint/timer/subscription proposals write no WAL. A changed proposal becomes a durable input, prepared snapshot and complete transition before any effect. Registered children retain process, protocol, time and Linux OS limits. | `engine/engine-core/src/engine/strategy_callbacks.rs`, `engine/engine-core/src/strategy_process/host.rs` |
| Effect scheduler | Ordered durable suffixes yield without losing reductions; caller identity, state, timers, placement IDs and complete outbox acceptance remain bound through replay. | `engine/engine-core/src/engine/strategy_effects.rs`, `engine/engine-core/src/engine/scheduling.rs` |
| Sleeve inventory | Durable sleeve keys own independent exact inventory, basis and stops, including opposing same-ticker holdings. Physical-net translation and stable sleeve-key allocation belong to the engine. | `engine/engine-core/src/attribution.rs`, `engine/engine-core/src/portfolio_allocation.rs`, `engine/engine-core/src/portfolio_protection.rs` |
| General exits | Canonical quantities survive ordinary intents, full native exits, partial exits, market maximum chunks, pending targets and restart. A legacy full-holding projection resolves to the exact owned quantity; an explicit canonical partial exit remains partial. | `engine/engine-core/src/engine/intent_admission.rs`, `engine/engine-core/src/engine/portfolio_runtime.rs`, `engine/engine-strategies/src/native_common/mod.rs` |
| Emergency exits | Known terminal attempts release their attempt ID; ambiguous sends retain ownership. Exact net closure precedes durable internal offset settlement. Rejection, late fills, fees and each restart cut preserve the same obligation. | `engine/engine-core/src/portfolio_control.rs`, `engine/engine-core/src/engine/portfolio_runtime.rs` |
| Orders and lineage | Abandoned incomplete rotation files follow the same trust rule as normal boot; corruption after a committed restatement remains an error. One canonical order representation serves live and recent terminal state. The terminal cache holds at most 256 rows / 4 MiB; live reductions remain resident. One cancellable archive reader owns one pending private event, then restores the order durably before its late fill. New IDs use a durable monotone epoch and reversible numeric counter. | `engine/engine-core/src/engine/order_lineage.rs`, `engine/engine-core/src/engine/order_epoch.rs`, `engine/engine-core/src/inflight.rs`, `engine/engine-wal/src/order_lineage.rs` |
| Account and risk | Native lexical quantities, equity and balance remain canonical. Exact quantity, known price/range, exposure, margin, capital and loss arithmetic authorize decisions; display projections do not authorize them. | `engine/engine-types/src/orders.rs`, `engine/engine-types/src/risk.rs`, `engine/engine-risk/src/`, `engine/engine-venue/src/account_numbers.rs` |
| Stops | Exact native terms preserve closing side, remaining quantity, unique identities and full-size trigger coverage. Logical sleeve protection and native physical protection retain separate ownership. | `engine/engine-venue/src/account_stops.rs`, `engine/engine-core/src/engine/stop_runtime.rs` |
| History acquisition | Native pages feed a stable disk merge ordered by `(venue_ts_ms, ordinal)`: 256 KiB runs, bounded rows, cancellation and progress. Iteration does not accumulate the complete response. Runtime applies 32 rows per turn; boot folds rows directly into canonical books and WAL. | `engine/engine-types/src/execution_history.rs`, `engine/engine-core/src/engine/history_recovery.rs`, `engine/engine-core/src/engine/boot_recovery.rs` |
| History completeness | A checkpoint advances only after authenticated exact physical agreement and resolution of orders, dispatch and reconciliation. Empty or unrelated net-neutral pages cannot erase known missing fills. Progress timeout releases stalled readers; healthy idle observations keep retention current. | `engine/engine-core/src/engine/account_recovery.rs`, `engine/engine-core/src/engine/history_recovery.rs` |
| Money and restart | Exact lot quantities, entry/exit values, cash and fees survive partial fills, reversals and rotation. Segment v7 requires explicit legacy retirement state and the cost-basis array and validates its owner/quantity linkage and cash equation. The precision marker makes predecessor readers refuse before ignoring canonical fields. | `engine/engine-core/src/execution.rs`, `engine/engine-core/src/execution/roundtrip.rs`, `engine/engine-types/src/trade.rs`, `engine/engine-wal/src/lib.rs` |
| Input lifecycle | Durable readiness, generation grants/seals, terminal consumption and retired floors retain missing prefixes. Append-only identities and retained native metadata support reordered registrations and exits during catalog outage. | `engine/engine-core/src/signal_state/`, `engine/engine-core/src/signals/readiness.rs`, `engine/engine-core/src/identities.rs`, `engine/engine-core/src/engine/symbol_admission.rs` |

| Decision | Selected policy | Alternative and reason |
| --- | --- | --- |
| Shared ownership | Exact virtual sleeves plus one physical account authority | Exclusive symbols cannot represent required opposing sleeves; a second account ledger adds competing ownership |
| Late terminal events | Reuse the canonical order from the retained WAL family | A separate tombstone ledger duplicates allocation/replay; eviction without recovery loses late fills |
| Market callbacks | Coalesce transient inputs; persist only proposals that change behavior | Embedded-only production avoids the observed write load but gives up process isolation; persisting every unchanged quote repeats the resource defect |
| Numeric boundary | Exact canonical values with validated legacy projections | Removing legacy fields breaks old WAL and adapters; converting canonical values through float loses valid quantity and price distinctions |
| Legacy accounting migration | Durable grid context normalizes eligible legacy units before canonical fill reducers; validated automatic FIFO and internal full-close allocations depending on those units are reconstructed with the same native totals, fees, stable policy and internal settlement price/time | Preserving faulty derived slices or permitting partial internal settlement creates microscopic opposing holdings; canonical-only allocations and explicit native amounts remain authoritative |
| Unknown valuation | Retain typed settlement/fee valuation debt in the rolling-loss window; preserve reductions | Assuming USDC or another asset equals USDT invents unavailable conversion evidence |

| Boundary | Explicit behavior |
| --- | --- |
| Legacy values | Eligible legacy quantity contributions resolve to a unique native grid point within 64 binary64 ULPs per input, durably before missed-fill allocation. Canonical suffixes and monetary values stay exact; old missing cost basis stays unpriced. Strategy scalar intent follows the outbound decimal policy. |
| Reporting | Money projections may underflow to zero or saturate at finite signed `f64::MAX`; canonical accounting and risk values remain exact. |
| Archive retention | Order lineage requires the retained WAL family from segment 1. External pruning must also preserve unresolved callback sources. Missing archive data is an unresolved recovery condition. |
| Storage and process limits | History resident memory is bounded by run/row contracts, not total history length; disk usage scales with retained history. Linux resource tests qualify the stated workload, not a universal RAM ceiling. macOS lacks equivalent OS memory/fork enforcement. |
| Recovery availability | Unsupported history, manual fills without engine lineage, unknown asset conversion and erased producer tails stay explicit. |
| Retry timing | Durable obligations and ambiguous-send ownership survive restart; runtime backoff restarts and may allow one immediate retry. |
| Allocation | `EmergencyNetFifo` is versioned stable sleeve-key contributor order, not arrival-time FIFO. |
| Operational scope | No funded deploy, live account parity, capital change or strategy promotion follows from the local test results. |

| Qualification | Result |
| --- | --- |
| Final integrated debug / release | Each: 27 binaries, 2,300 passed, 0 failed, 5 expected ignores; all 108 fault-case tests pass in both profiles |
| Strict Clippy, format and developer gate | Rust 1.90.0 clean; debug/release doctests pass (empty targets). Doctor, Ruff, ShellCheck and mypy over 100 files pass; 1,514 pytest pass. The final Rust suites cover the two fixes after the developer-gate run. |
| Fail-before / pass-after regressions | 108 isolated assertion-failure/restored-pass cases, 106 distinct controls; 273 retained artifacts rehashed independently. Earlier bundles and failed setup runs add no proof count. |
| Linux process and worker envelopes | 13 process tests passed; 270-symbol worker cold start, 12-hour outage frontier, overload and restart passed |
| Deterministic fault simulation and reader probes | 48 seed runs, each repeated: faultless 1, light 1–6, heavy 7 and heavy 1–40; all evaluated checks pass and every WAL replay is identical. Cash and closed-ledger comparisons run for the 36 flat endings; 12 open endings explicitly leave those comparisons unjudged. Old reader refuses the precision marker; candidate reads legacy and marked fixtures without byte changes. |

## Invariants

- Must preserve shared and opposing sleeve ownership, WAL compatibility, reconciliation, protective stops and reductions.
- Must publish neither a partial callback proposal nor an unjournaled order effect.
- Must preserve exact canonical quantity and known monetary terms through risk, dispatch, fill allocation and restart.
- Must retain unresolved orders, history and source prefixes until evidence resolves them; must never infer completeness from an empty page alone.
- Must count a bug regression only when an actual assertion fails with its fault introduced and passes after restoration; compile errors and mutation survivors are not proof.
- Must keep source qualification separate from the last verified operational snapshot.

## Operational Recipes

```sh
# Run from the repository root; pin the compiler as well as cargo.
TASK_SYSROOT="$(rustup run 1.90.0 rustc --print sysroot)"
export PATH="$TASK_SYSROOT/bin:$PATH" RUSTC="$TASK_SYSROOT/bin/rustc" RUSTDOC="$TASK_SYSROOT/bin/rustdoc"
(cd engine && cargo fmt --all -- --check)
(cd engine && cargo clippy --workspace --all-targets --locked -- -D warnings)
(cd engine && cargo test --workspace --all-targets --locked --no-fail-fast)
(cd engine && cargo test --workspace --all-targets --release --locked --no-fail-fast)
(cd engine && cargo test --workspace --doc --locked)
(cd engine && cargo test --workspace --doc --release --locked)
scripts/dev.sh check
```

```sh
cd engine
cargo run --bin engine --release --locked -- sim --seed 1 --seconds 300 --symbols 2 --crashes 0 --faults none --twice
cargo run --bin engine --release --locked -- sim --seed 1 --seeds 6 --seconds 300 --symbols 2 --faults light --twice
cargo run --bin engine --release --locked -- sim --seed 7 --seconds 300 --symbols 2 --crashes 2 --faults heavy --twice
cargo run --bin engine --release --locked -- sim --seed 1 --seeds 40 --seconds 300 --symbols 2 --crashes 2 --faults heavy --twice --keep --out /tmp/tier1-sim-heavy
```

```sh
# Linux, with the same pinned compiler.
cd engine
cargo test --locked --release -p engine-core --test integration strategy_process -- --nocapture
cargo test --locked --release -p signal-worker full_population_outage_resource_envelope_is_bounded -- --ignored --nocapture
```
