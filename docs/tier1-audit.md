# Trading-platform audit

## Purpose

Record the verified execution boundaries, unresolved defects, and implementation priorities for incremental improvements to the trading platform.

## Spec Tables

### Evidence scope

| Item | Evidence boundary |
| --- | --- |
| Source baseline | GitHub `main`, `e2345ca450d03a3d58ff19b9d2b436e9b84cfbb4`; 53 commits after the handoff's `ca41a7931fadccd43c0a0e62a8a7f1d2bee9052f` |
| Handoff | Context and hypotheses; its ticket order and proposed types are not implementation requirements |
| Baseline checks | Repository doctor ready; 510 engine-core library tests pass |
| Compiler | Repository pin: Rust 1.90.0. Shell default: Homebrew Rust 1.97.1. Here `rustup run 1.90.0 cargo` still discovers Homebrew `rustc` through PATH; set the toolchain bin directory and `RUSTC` explicitly, and verify `target/.rustc_info.json` |
| Remote metadata | GitHub branch API reports `protected=false`; current head signature `valid`; handoff head `unsigned`; ruleset API returns HTTP 403 requiring a plan upgrade/public repository |
| Production | No host, account, live WAL, credentials, or running deployment inspected or changed; `STATE.md` remains a historical operational snapshot, not evidence from this audit |
| Checkpoints | Local commits only; validation receipts belong in `CHANGELOG.md` |
| Current working scope | The timer, p99.9 and signal-availability fixes share the module and state ownership changes through `efb5a9a5`; signal admission lives in `engine/engine-core/src/engine/signal_intake.rs` and timer callbacks use `StrategyHost`. Combined validation is recorded in `CHANGELOG.md` |
| Qualified local artifact | `15c60924abfb9f5c7848b7ee7b4c5853f2b932d3`; actual Rust 1.90/macOS ARM release tests, soak, local benchmark, smoke checks and unchanged-byte packaging pass; [hashes and workload results](tier1-release-qualification.json). Subsequent changes require their own verification |
| Source notation | Rust crate paths below are relative to `engine/` |

### Verified findings

| Priority / handoff claim | Current finding | Primary source / consequence |
| --- | --- | --- |
| P0 / additional ownership defect | Native planning retains an explicit foreign-owner fact. Omitting another sleeve's holding while retaining its price/rules converts unavailable exposure into a flat position. Central admission also excludes another sleeve's live opening orders. | `engine-strategies/src/native_common/mod.rs::planner_facts`, `engine-strategies/src/position_plan.rs::plan`, `engine-core/src/engine/intent_admission.rs::prepare_intent`; dynamically admitted symbols bypass static config overlap checks |
| P0 / 001 signal gaps | Contiguous acceptance, explicit acknowledgement, durable scoped gaps and bounded catch-up are implemented. With accepted 9 and observed 11, the cursor stays at 9; 10 then 11 reach the reducer in order. Other inputs to affected destinations wait, including new generations; independent destinations remain usable. | `engine-core/src/signal_state.rs`, `engine-core/src/signals/`, `engine-core/src/engine/signal_intake.rs`; restart/rotation, failed barriers, saturated catch-up, dependent entry/cancel/amend and exit tests. Unseen startup gaps and legacy skipped history remain evidence limits |
| P0 / 002 latency windows | Reset/rendering enumerate `Segment::ALL`; every histogram starts a new window. The regression compares every quantile and WAL/text output against a fresh ledger. | `engine-core/src/ledger.rs`; no metric names or WAL fields change |
| P1 / 003 source governance | Unprotected branch confirmed; unsigned **current** head is false. A blanket signed-commit requirement is not an execution-correctness fix. | GitHub branch/commit APIs at the source boundary above; no repository settings changed |
| P1 / 004 release qualification | Deploy and qualify package the same optimized bytes after release tests, bounded local workloads and binary smoke checks. Commit/compiler/platform/log and byte hashes bind the artifact. Deployment verifies candidate and retained incumbent artifacts before checkout; host compilation is disabled. | `.github/workflows/vps-deploy.yml`, `scripts/release_artifact.py`, `scripts/deploy_vps_live.sh`; qualification does not establish cross-version WAL compatibility |
| P1 / 005 identities | Positional `u16` handles confirmed. Production boot preserves the WAL strategy prefix, refuses duplicate sleeves, seeds symbols from WAL order, and checks dynamic core/feed/venue/private IDs. Reordering already fails closed. | `engine-types/src/ids.rs`; `engine-core/src/assembly.rs::symbol_order`; `engine-core/src/engine/boot_recovery.rs`; `engine-core/src/engine/signal_intake.rs::admit_wanted` |
| P1 / 006 durability | Order groups append, start a barrier, and dispatch before disk completion; dependent result handling settles the barrier. `Segment::Durable` is measured at barrier **start**, not completed fsync; ledger and benchmark explanations state that boundary. | `engine-core/src/engine/intent_admission.rs::process_intents`; `engine-core/src/engine/venue_completion.rs::settle_barrier`; `engine-types/src/wal.rs::PendingBarrier` |
| P1 / 007 risk | Capital, margin, stop, stale-account, stale-quote, rolling-loss and pending-exposure controls exist. Central child-notional/lot, aggregate order-rate, turnover and concentration limits are not all represented. | `engine-risk/src/config.rs::KernelConfig`, `kernel.rs::evaluate`; `engine-core/src/engine/intent_admission.rs`; new limit values require an explicit policy decision |
| P1 / 101 strategy modularity | Static registration already exists: a plug is added in `engine-strategies/src/lib.rs::PLUGS`, without engine-core editing. Typed schema/fingerprint contracts also exist. A manifest can collect metadata; it does not create modularity by itself. | `engine-strategies/src/lib.rs`, `engine-types/src/strategy.rs::StrategyCheckpointIdentity`, `engine-core/src/assembly.rs` |
| P1 / 102–105 composition | Raw `Place`/`Cancel`/`Amend` effects and one account position/stop per symbol remain. Fill attribution exists, but deterministic netted contributions and virtual fill-allocation policy do not. | `engine-types/src/orders.rs::Action`; `engine-core/src/attribution.rs::forced_close_owner` refuses ambiguous ownership; `assembly.rs::one_owner_per_symbol` |
| P1 / 106 callbacks | Timer dispatch snapshots at most 64 due keys per turn and yields to the executor after draining; replacement timers wait for the next turn and can supersede another snapshotted key. Callbacks remain synchronous. `Ctx::emit` appends before the 64-action drain limit; durable checkpoint/event/consume actions bypass both drain caps. Dropping effects would separate native state, checkpoints and orders. | `engine-core/src/ctx.rs::{StrategyHost::feed,Ctx::emit,Timers}`, `engine-core/src/engine/scheduling.rs::{drain,on_timers}`; durable-action fairness, checked whole-batch output and hard callback isolation remain open |
| P1 / 201–202 latency evidence | Existing histograms now carry p99.9 through benchmark JSON/table, heartbeat, optional WAL summaries, replay, sampler and dashboard. Older or empty p99.9 remains unavailable; measured zero is retained. The separate per-command WAL timing tool already reconstructs exact p99.9. No stable-host workload regression threshold is established; the benchmark uses allow-all risk and localhost HTTP. | `engine-core/src/{bench,ledger,heartbeat,replay,timing}.rs`; `scripts/runtime/record_equity.py`; benchmark JSON is retained in its WAL `Note`, while CLI displays a table |
| P1 / 203–205 exact values | Canonical order/position/fill/fee/risk values use `f64`; shared quantization already rounds passively/downward and rejects invalid numbers. `InstrumentRule` has only tick, step, minimum quantity and minimum notional. | `engine-types/src/orders.rs`, `quantize.rs`; `engine-risk/src/kernel.rs`; exact numeric migration must preserve boundary tests and legacy replay |
| P1 / 206 resource bounds | Venue command/completion channels have 4,096 slots. Signal channel has 256 ordinary rows/64 MiB plus one requested-prefix recovery slot (17 MiB envelope maximum); spool scanning uses 64-path pages and 4,096 metadata entries. Timer storage has one ordered node per active key; obsolete rearm nodes are removed. Accepted unconsumed signals, callback/deferred output, distinct active timer IDs and subscription/sync queues still lack intrinsic caps. | `engine-core/src/{venue_runtime,signal_state,ctx}.rs`, `engine-core/src/signals/`; `engine-marketdata/src/bybit/feed.rs`; `engine-wal/src/lib.rs::SyncThread` |
| P2 / 207 venue modularity | Shared gateway/capability traits and per-venue modules already exist. All adapters/cryptography compile into one venue crate. Core batch ceilings still explicitly use Bybit's sizes. | `engine-venue/Cargo.toml`, `engine-venue/src/venues/`, `engine-types/src/lib.rs`, `engine-core/src/engine.rs::MAX_ORDERS_PER_BATCH` |
| P2 / 208–209 component ownership | Core is child modules under `engine/` on one mutable `Engine` whose strategy-facing state is `Books` and `StrategyHost`; the worker's three source pipelines share one history module. Native LONG/CARRY/Exodus share `SleeveCore` bookkeeping while their reducers and serialized states remain separate. `live.rs` still combines lane scheduling, repair and publication. File size establishes maintenance scope, not a latency bottleneck. | `engine-core/src/engine/`, `engine-core/src/ctx.rs`; `signal-worker/src/history.rs`, `live.rs`; `engine-strategies/src/native_common/sleeve.rs`; extract state ownership before adding threads |
| P2 / 301 deployment privilege | Qualified binaries replace host compilation and stale extraction reuse. Routine deploy still fetches source, forwards a temporary GitHub token and defaults to root; privilege and immutable release-directory work remain open. | `scripts/deploy_vps_live.sh::{fetch_exact_commit,build_engine,install_release}`; `.github/workflows/vps-deploy.yml` |
| P1 / capability boundary | Context rewrites placement ownership, but cancel/amend/stop effects are not all bound to the calling sleeve. Reduce-only sizing is account-level. This checkpoint does not establish isolation from arbitrary or malicious strategy implementations. | `engine-core/src/ctx.rs::emit`; `engine-risk/src/kernel.rs::evaluate`; trusted native plugs still supply their own order IDs and attributed quantities |
| P2 / 302–305 operational evidence | Checksummed WAL frames, torn-tail detection, atomic rotation, account leases, reconciliation and recovery tests are implemented. Full process/host-death crash matrix, independent observer, and off-host restore drills are not established by this local audit. | `engine-wal/src/lib.rs`; `engine-core/src/tests/{rotation,gap_recovery,reconciliation,order_path}.rs`; `engine-venue/src/lease.rs` |
| P2 / 306–308 research parity | A Python-to-Rust contract harness and immutable LONG/CARRY/Exodus fixtures already exist. They pin local reducer output; they do not prove real venue execution, economic profitability, or unseen forward performance. | `tests/fixtures/*native_replay*`, `tests/fixtures/exodus_live_contract_replay_v1.json`; `tests/research/backtest/test_native_directional_contract.py`; `engine-strategies/src/bin/strategy_contract.rs` |

### Architecture decisions and order

| Order | Decision | Alternative / what changes the decision |
| --- | --- | --- |
| 1 | Correct foreign-owner planning and enforce existing exclusive ownership centrally using current attribution/live-order indexes. Keep actual reductions and protective actions available. | Removing exclusivity now creates ambiguous stops and forced-fill attribution; only a tested portfolio/virtual ledger justifies that change |
| 2 | Migrate signal delivery as one explicit acknowledgement/inbox contract, including bounded catch-up and replay. | A log-then-skip patch trades on missing history; error-and-restart wedges on the same spool row; neither is acceptable |
| 3 | Correct durability metric semantics, add named local workload evidence, and tie release qualification to the exact artifact without rebuilding the same graph repeatedly. | Absolute latency thresholds are not justified by this laptop or a noisy hosted runner; stable-host evidence can establish them |
| 4 | Introduce stable registry keys beside dense handles. Reuse canonical unique sleeve names where possible; instruments need venue/realm/product/symbol identity. Retain append-only legacy mappings during migration. | UUIDs for everything and live WAL rewriting add cost before demonstrating benefit; renaming requirements may justify a separate stable key |
| 5 | Add target contributions and deterministic virtual allocation in a compatibility path, preserving current one-sleeve decisions before enabling shared symbols. Portfolio/OMS owns stops and child orders. | Allowing multiple raw-order owners before allocation/recovery is defined is not strategy composition |
| 6 | Migrate legal decimals/ticks/lots at venue boundaries, then reservations/fees/accounting; use checked arithmetic and explicit overflow. | Floating statistical features can remain floats; fixed `MoneyMicros` is not universally exact across settlement assets |
| 7 | Bound callback output at emission and measure callback duration. Move slow work outside the decision callback; extract state-owned components incrementally. | Measuring an overrun after a synchronous callback cannot preempt an infinite callback. A hard isolation guarantee needs an execution boundary, not a timer alone |
| 8 | Split venue/catalog dependencies and feature-select builds when measured build/runtime cost or adapter testing warrants it. | A crate per venue is optional packaging, not a prerequisite for correcting execution ownership |

### Local performance evidence

| Parameter | Scope |
| --- | --- |
| Primary artifact | [All 24 measured Rust 1.90 runs, profiles, hashes, compiler and machine metadata](tier1-local-benchmark.json) |
| Supplemental artifacts | [24 runs with Homebrew Cargo/Rust 1.97](tier1-local-benchmark-1.97.json); [24 runs with Cargo 1.90 still discovering Homebrew Rust 1.97](tier1-local-benchmark-1.97-cargo1.90.json); these do not qualify the compiler pin |
| Source comparison | `e2345ca4` baseline versus `c517bab0` candidate |
| Machine / compiler | Apple M4, 16 GiB, macOS Darwin 24.6.0, explicit Rust 1.90.0; both build-cache compiler records verified before measurement; no concurrent builds from this audit |
| Method | One warm-up per binary/profile, four alternating run pairs, new real WAL per run, localhost HTTP venue, no fills, benchmark allow-all risk |
| Timing boundary | Market event to handling the submit result; the subsequent disk-barrier wait is separate |
| Stage limitation | `write it down` spans decision timestamp to barrier request, including prior queueing/admission; it does not isolate ownership lookup, WAL append, or fsync cost |

| Profile | Events / quotes per second / every-Nth order / symbols | Orders per run | Baseline median run p99 (range), ms | Candidate median run p99 (range), ms |
| --- | --- | --- | --- | --- |
| Paced, one symbol | 4,000 / 2,000 / 20 / 1 | 200 | 0.434 (0.415–0.526) | 0.467 (0.411–0.562) |
| Paced, 100 symbols with unfilled orders | 10,000 / 2,000 / 19 / 100 | 526 | 0.605 (0.503–0.719) | 0.588 (0.525–0.739) |
| Saturation, one symbol | 20,000 / unlimited / 20 / 1 | 1,000 | 3,705.668 (3,621.782–3,774.874) | 3,702.522 (3,619.684–3,852.468) |

All profiles send the same order counts before and after. The one-symbol
paced median is higher; the other two are slightly lower. All run ranges
overlap. These samples do not isolate a reproducible regression or establish
a speedup. The saturated run exposes seconds of backlog and cannot justify a
nominal latency budget. This single-strategy local harness does not measure
production risk, many-strategy callback cost, real venue latency, or p99.9.
Supplemental compiler series remain separate because their binaries differ;
the Cargo 1.90/Rust 1.97 series also has a higher candidate saturation median.

### Signal migration requirements

| Boundary | Required behavior |
| --- | --- |
| Feed acknowledgement | A returned spool row is retired only after explicit durable-acceptance/duplicate acknowledgement. Deferral keeps it on disk; cancellation of reads and deletion preserves their handles |
| Prefix | The accepted cursor advances only through a contiguous source/generation prefix; buffered rows never become accepted merely through replay |
| Saturation | Missing prefix rows remain retrievable when count/byte limits are full; restarting a full durable inbox is not a recovery mechanism |
| Availability | Live channel, spool and replay wait for `available_wall_ts_ms`; ready destinations remain selectable. Future rows cannot borrow the channel's sole recovery slot. Cancelled waits retain ownership, scans carry the engine clock explicitly, and core rechecks availability before WAL acceptance |
| Scope | Named strategy input dependencies resolve once at boot and cover entry placement, opening amend, existing opening orders, queued actions and other source/generation delivery. Native Exodus declares its CARRY dependency; independent destinations remain usable |
| Generation | A new source generation must not silently clear an older known gap for its dependent strategy; legacy accepted-gap cursors cannot reconstruct erased history |
| Exits | Private updates, account recovery, stop maintenance, cancel and genuine reductions remain serviceable during a gap |
| Replay | The producer's immutable spool retains deferred payloads; WAL retains accepted inputs, cursors, routes and gap high-water marks. `segment_base_v2` requires gap state; cursor/route/subscription inconsistencies fail boot. Existing callbacks are at-least-once across crashes |
| Compatibility | New readers accept legacy WAL. Older readers must refuse a new required gap/inbox record or versioned rotation, rather than ignore optional state. The current WAL decoder already rejects checksum-valid unknown records without truncating them |
| Remaining limits | Known gaps and future-availability boundaries are enforced. A malformed native payload or a no-op consumer can still leave an accepted observation retained; consumer-fault handling, accepted-input backpressure and a producer-readiness handshake before boot-restored actions remain open. No automatic waiver or live state migration is implemented |

## Invariants

- Must preserve the WAL, reconciliation, risk-reducing order checks, account leases, and strategy checkpoint/effect ordering.
- Must distinguish missing/unavailable state from explicit flat state; must never infer authority from the absence of a row.
- Must retain exclusive execution ownership until portfolio stops, allocation and recovery are implemented together.
- Must report local test/benchmark evidence as local evidence; must never label this repository Tier 1 from the handoff or passing unit tests.
- Must obtain human approval before deployment, real-money arming, capital-limit changes, live WAL migration, credential rotation or irreversible production actions.
- Must keep live configuration and production state untouched by local audit and validation commands.

## Operational Recipes

Run from the repository root; these commands test local code only.

```bash
scripts/dev.sh doctor --json
scripts/dev.sh lint
scripts/dev.sh shellcheck
scripts/dev.sh types
scripts/dev.sh test
tier1_rust_bin="$(dirname "$(rustup which --toolchain 1.90.0 rustc)")"
export PATH="$tier1_rust_bin:$PATH"
export RUSTC="$tier1_rust_bin/rustc"
export RUSTDOC="$tier1_rust_bin/rustdoc"
rustc -Vv
cargo fmt --manifest-path engine/Cargo.toml --all -- --check
cargo clippy --manifest-path engine/Cargo.toml --workspace --all-targets --locked -- -D warnings
cargo test --manifest-path engine/Cargo.toml --workspace --all-targets --locked
cargo test --manifest-path engine/Cargo.toml --workspace --all-targets --release --locked
```

For each benchmark run use a new temporary WAL and the same source, machine,
compiler and workload metadata as its comparison; the existing benchmark uses
only its internally created localhost venue.

```bash
bench_dir=$(mktemp -d /tmp/liquidity-local-bench.XXXXXX)
cargo run --manifest-path engine/Cargo.toml --release --locked -p engine-core --bin engine -- bench \
  --events 4000 --rate 2000 --every 20 --symbols BTCUSDT \
  --wal "$bench_dir/run.wal"
```

Qualify one clean local commit with the pinned compiler environment above.
The artifact targets this machine's OS/architecture; this command does not
deploy it or assess cross-version WAL compatibility.

```bash
qualification_checkout=$(mktemp -d /tmp/liquidity-qualification.XXXXXX)
qualification_commit=$(git rev-parse HEAD)
git worktree add --detach "$qualification_checkout" "$qualification_commit"
python3 "$qualification_checkout/scripts/release_artifact.py" qualify \
  --repo "$qualification_checkout" --commit "$qualification_commit" \
  --output "$qualification_checkout.tar.gz"
```
