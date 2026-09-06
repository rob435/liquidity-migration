# Tier-1 audit, round 2

## Purpose

Track verified gaps and architecture decisions for a modular execution engine with interchangeable strategy and venue implementations.

## Spec Tables

### Scope and authority

| Item | Contract |
| --- | --- |
| Product | Durable execution, exact accounting, deterministic strategy decisions, independent sleeves and pluggable venues |
| Working tree | Round-2 cleanup is integrated with the funded repairs through `16689a98`; combined qualification and deployment are in progress |
| Runtime specification | [engine.md](engine.md); source and behavior tests take precedence over ratings or audit assertions |
| Implementation checkpoint | [tier1-round-handoff.md](tier1-round-handoff.md); [compact qualification evidence](tier1-round2-evidence.json) |
| Operational authority | [STATE.md](../STATE.md) records a dated host observation; local tests do not update that observation |
| History | [CHANGELOG.md](../CHANGELOG.md); resolved findings leave this table after integration and verification |
| Qualification language | An embedded reducer simulation, a real child-process integration test and authenticated venue evidence establish different things |

### Retained architecture

| Area | Decision | Alternative and reason |
| --- | --- | --- |
| Venues | Retain Bybit, Binance, Hyperliquid, Lighter, MEXC and Variational implementations and their typed registry | Deleting adapters because their realms are dormant removes required product capability; dormant is a live-evidence status, not dead code |
| Strategy execution | Preserve process isolation and qualify its actual protocol, scheduling and durability path | Embedded-only production cannot terminate a stuck reducer on the current-thread runtime; it is appropriate for deterministic reducer simulation |
| WAL format | Preserve supported framed JSON readers and exact semantics; reduce encoding allocations and measure barriers before changing format | A binary-format migration or removal of v2-v6 readers is not justified by which versions one host happened to write; retained artifacts and recovery tools also read them |
| Account ownership | Exact sleeve inventories determine one physical net; display/legacy projections have no admission authority | Merging virtual ownership with physical net loses opposing sleeves and their independent stops |
| System supervision | Retain systemd ownership of independent services and timers | Replacing it with a new supervisor adds an owner without demonstrating a fault it fixes; WatchdogSec requires engine liveness notification before enabling it |
| Research | Keep reusable research/data capabilities; remove only proven redundant implementations and stale interfaces | Being absent from a trading unit is expected for a research tool and is insufficient deletion evidence |
| Grafana | Retain the dashboard renderer and published dashboard definition | The recorder, observability runbook and user workflow consume them; a self-consistency test is not their only consumer |
| Demo probe and disabled maker | Preserve stable strategy identity and the enabled demo measurement probe; suppress unnecessary work only when state/holdings permit | Deleting template blocks changes persisted IDs and removes an enabled demo function |
| Legacy state | Remove import/migration writers only after both realms and the retained rollback/replay contract no longer need them | A local migration test does not establish completed host migration |

### Verified open work

| ID | Area | Required outcome | Current scope |
| --- | --- | --- | --- |
| R2-01 | Production qualification | Bench and dedicated qualification exercise real child processes and disclose coalescing, samples, callback mode and clock | Bench uses the real child executable and risk kernel; engine-run regressions count WAL records for no-op callbacks. Sustained benchmark and combined Linux qualification remain required; sim/backtest are embedded reducer diagnostics |
| R2-02 | Callback timing | Decision and end-to-end timing include child execution and durability; admission uses current quote age | Deferred effects retain source and parent-completion clocks; unchanged-state and stale-quote regressions pass locally. Replayed effects carry no fabricated monotonic timing. Later exact physical-growth checks already use the current clock; a funded stale-wire bypass is not established |
| R2-03 | Callback cost | Remove redundant state decode and snapshot sizing while preserving atomic effects and bounds | Changed child state is decoded once and retained through commit; unchanged proposals skip restore. Snapshot byte sizing avoids a second full serialization. The complete declared context still crosses the process boundary |
| R2-04 | WAL encoding | Preserve record meaning, legacy tags and partial-frame recovery with fewer allocations | Nonfinite fees are refused before append; borrowed tag encoding and seeded semantic round-trip tests pass locally. Rotation/reopen also preserves ordinals above six digits and refuses exhaustion before writing. Supported record readers and unknown-fee meaning remain unchanged; integration is pending |
| R2-05 | Durability cost | Measure callback, dispatch and attempted-send barriers separately on the deployed execution mode; simplify only redundant obligations | Three barriers are a measured design question, not permission to publish unjournaled order effects |
| R2-06 | Runtime/tool boundary | Separate simulation, backtest, benchmark and operational CLI code from the funded runtime without changing command or recovery behavior | `engine-tools` owns tools and companion CLI; lean `engine` owns runtime and child protocol. Three-binary packaging, command forwarding and install tests pass locally; integrated deployment remains required |
| R2-07 | Exact quantity boundary | Verify all real wire constructors and replace redundant live quantity stores without weakening legacy readers | Canary entry/cleanup and terminal lookup use exact terms. `OrderRec` holds one fill frontier; compatibility snapshots remain byte-identical after scalar-cache removal. Sleeve ownership, physical baselines, reservations and cash accounting retain distinct responsibilities |
| R2-08 | Signal worker | Derive removals from actual consumers; consolidate duplicated protocol/feature code while preserving gap repair, generation and replay | HTTP job scheduling shares one request budget and endpoint implementation. Confirmed candles, ticker coverage and persistence formats remain. Research/worker feature populations are independently constructed; full cross-environment feature parity is not claimed |
| R2-09 | Deploy pipeline | One consistent artifact contract from build through local staging and remote install; lint the actual remote program | The extracted remote program is linted; SSH setup is shared; recovery verbs and three-binary staging pass local tests. Checksummed-only and optionally qualified artifacts remain valid |
| R2-10 | CI | Run ordinary checks on main pushes and keep costly release workloads explicit | Workflow and regression updated locally; final workflow validation and integration required |
| R2-11 | Runtime dependencies | Install only dependencies imported by deployed Python entrypoints | Python capture imports websocket-client; stdlib-only claim is false. Minimal runtime lock and clean-environment import test pass locally |
| R2-12 | Supervision and alerting | Exercise bounded restart behavior, stalled-loop detection and alerts for sustained WAL/error load through current owners | Engine/worker units allow five starts per 300 seconds. Sleeve errors page independently of admission; stale observer data emits `up=0`. Host liveness warns on projected disk-floor crossing within its next 195-second observation interval, with WAL metadata attribution. Host verification remains required |
| R2-13 | Recovery operations | Rehearse one-command recovery on compatible state and explicit forward repair across incompatible WAL changes | A predecessor refusing precision-era WAL cannot be made a valid rollback by renaming a command; demo/funded timing and host verification remain operational work |
| R2-14 | Venue boundaries | Test signed rejection, real timeout then late fill, cancel/fill race, quota accounting and malformed stream recovery through engine ownership | Constructed protocol fixtures exercise real local HTTP/WebSocket boundaries, including a delayed create with responsive cancellation. They are not authenticated private-stream captures |
| R2-15 | Recorded-day replay | Run off-host replay on a redistributable, sanitized real WAL/tape fixture with explicit accounting scope | The real private-prefix boot/rotation/reboot test runs locally. A 123-second public tape replays byte-identically with zero fills; neither establishes a complete production day. Full retained-family acquisition remains required |
| R2-16 | Resource and latency envelope | Publish workload, platform, sample count and measured quantiles; run declared CPU/memory/disk/IO workload through the production mode | Existing memory tests and embedded timings do not establish a universal resource ceiling or a latency SLO |
| R2-17 | Documentation | One compact host snapshot, one implementation handoff, one open audit; archive history and eliminate duplicated receipts | STATE is compact and dated; historical receipts and the original audit remain in Git; CHANGELOG history is archived. Final combined qualification and host observation remain pending |
| R2-18 | Peripheral cleanup | Remove unused Rust recorder and proven orphan wrappers, retain reusable research and dashboards, relocate historical config fixtures only after checking CLI consumers | The unused Rust recorder, old Python current-universe builder, orphan pack wrapper and dead deploy function are removed. Research lab, registered config files, all venues, Grafana and the enabled demo probe retain real consumers |
| R2-19 | Callback contention | Defer source-owned callbacks during a busy invocation without recording a strategy failure or losing order | Typed deferral preserves order events, timers and controls without a false fault; real callback failures retain their existing handling. Source regressions and the formerly failing 20-second release benchmark pass locally; combined qualification remains required |

### Boundary corrections that constrain the work

| Audit claim | Source-grounded constraint |
| --- | --- |
| Four production OrderRequest constructors lack exact terms | The cited order_dispatch and portfolio_protection constructors are test code; portfolio_protection applies exact terms. working constructs a price-only AmendSpec that admission quantizes. Canary entry and cleanup are the real tool defects |
| No isolated no-op WAL-volume test | Callback market tests already run the framed worker over 270-symbol native snapshots and assert no WAL growth. The missing combination is engine.run plus a real child executable plus a volume assertion |
| No clock-skew tests | Bybit gateway contains validate_server_clock and a signing-window boundary test. An actual signed 10002 response through core is separate missing coverage |
| No timeout representation | Public HTTP has a timeout error; the venue boundary classifies it as ambiguous Transport. The missing case is the late fill through the engine after that real timeout |
| No private-position test means missing account ownership | Position messages are deliberately not a second fill source; authenticated snapshots and execution history own reconciliation. Tests must preserve that distinction |
| No property tests because no proptest dependency | WAL tests already exercise every partial-frame and rotation cut. Dependency names do not determine whether a property is tested; parser and semantic round-trip coverage still need expansion |
| Every deletion candidate is unreachable from deployed units | Demo probe is enabled; Grafana has an operator consumer; Python capture requires websocket-client |
| Configs v1-v5 have no consumers | Python rule/backtest/scoring tests and research commands use these files; moving them needs matching callers and preservation of registered research inputs |
| Committed Grafana pyc | git ls-files reports no tracked pyc file in the baseline |

### Legacy removal conditions

| Interface | Removal condition |
| --- | --- |
| Legacy binary64 quantities and grid adoption | Both realms have booted canonical migration and no retained WAL family/rollback reader depends on the writer; exact accounting and normalized legacy contributions remain distinguishable |
| Legacy signal-source retirement command | Planned stopped-source suffixes are retired on both realms and retained WAL state carries their final frontier |
| Unmanaged producer identity and legacy worker checkpoint pair | Both realm workers publish the managed format and the retained rollback contract no longer needs the previous checkpoint |
| Python-sleeve import writers | Authenticated deployment verifies canonical native state on both realms; initialization/verification commands remain |
| Names alongside IdentityState | Old-reader and replay requirements are explicitly retired; append-only identity continues |
| ExecutionPrecisionV1 marker | Keep while any predecessor can otherwise misread canonical accounting; it is an incompatibility declaration, not an optional boot decoration |

## Invariants

- Must preserve pluggable strategies and all venue implementations; activity on one host does not define product scope.
- Must qualify the actual callback mode before making a production execution claim.
- Must preserve durable reduction/effect order, exact ownership, reconciliation and archive readability.
- Must distinguish constructed fixtures, source mutations, local executions and authenticated operational evidence.
- Must demonstrate a behavioral regression fails before a bug fix and passes afterward; compile failures do not count.
- Must not replace missing measurements with a subjective Tier-1 rating or a target line count.
- Must retain an open finding until the combined implementation and relevant checks establish its outcome.

## Operational Recipes

```sh
# Run from the repository root.
TASK_SYSROOT="$(rustup run 1.90.0 rustc --print sysroot)"
export PATH="$TASK_SYSROOT/bin:$PATH" RUSTC="$TASK_SYSROOT/bin/rustc" RUSTDOC="$TASK_SYSROOT/bin/rustdoc"
export CARGO_INCREMENTAL=0
cargo test --manifest-path engine/Cargo.toml -p engine-wal
cargo test --manifest-path engine/Cargo.toml -p engine-tools --test integration
cargo test --manifest-path engine/Cargo.toml -p engine-venue
scripts/dev.sh check
```

```sh
# Production-mode local benchmark; synthetic market and venue, real worker executable.
cargo build --manifest-path engine/Cargo.toml --release --locked -p engine-tools --bins
engine/target/release/engine-tools bench --events 2000 --rate 100 --every 20 --wal /tmp/tier1-round2-bench.wal
```
