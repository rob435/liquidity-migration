# Tier-1 audit, round 2

## Purpose

Define retained architecture decisions and the remaining qualification scope for the deployed Round-2 engine.

## Spec Tables

### Scope and authority

| Item | Contract |
| --- | --- |
| Product | Durable execution, exact accounting, deterministic strategy decisions, independent sleeves and pluggable venues |
| Working tree | `93404ff6` integrates Round-2 cleanup with the funded repairs; [deployment run 34043450919](https://github.com/rob435/liquidity-migration/actions/runs/34043450919) succeeds |
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

### Open qualification

| ID | Required outcome | Current evidence and remaining work |
| --- | --- | --- |
| R2-20 | Verify the corrective deployment and naturally scheduled probe after ordinary-input and spool cancellation repairs | Eight actual baseline assertion failures now pass; integrated release, replay and workload qualification pass. Deployment and authenticated observation remain pending. |
| R2-15 | Replay a sanitized, complete recorded production day with matched input and accounting scope | Copied private-prefix boot/rotation/reboot passes; the 123-second public tape replays byte-identically with zero fills. Neither establishes a complete production day. Full retained-family acquisition and replay remain incomplete qualification. |

### Implemented contracts

| Area | Current contract | Evidence authority |
| --- | --- | --- |
| Callback execution | Real isolated children run production and the benchmark. Unchanged proposals avoid WAL amplification; changed state decodes once. Busy callbacks defer source-owned work without false strategy failures. | [Engine ownership](engine.md); [combined qualification](tier1-round2-evidence.json) |
| Clocks | Current admission time judges account and quote freshness. Durable decision identity remains unchanged; process-local optional timing prevents fabricated replay latency. | Five admission controls and three failing-before replay timing assertions in [regression evidence](tier1-round2-evidence.json) |
| WAL and quantities | Borrowed encoding retains semantic validation and supported readers; one exact in-flight frontier preserves compatibility snapshots. Canary entry/cleanup keep canonical wire terms. | WAL nonfinite/ordinal controls, canary regression and byte-identical snapshot comparisons in [evidence](tier1-round2-evidence.json) |
| Runtime and tools | `engine` owns runtime and child protocol; `engine-tools` owns operational tools, benchmark, simulation and backtest. The release installs both with `signal-worker`. | Exact installed and loaded hashes in [STATE](../STATE.md); [release workflow](https://github.com/rob435/liquidity-migration/actions/runs/34043450919) |
| Worker and dependencies | One public HTTP request budget and shared endpoint implementation retain gap repair and persistence. The runtime Python lock includes the actual websocket-client consumer. | Combined Rust/Python checks; isolated service import test; deployed recorder and worker observations |
| Supervision | systemd owns services and timers; engine/worker start limits are five per 300 seconds. Stale observations publish unavailable health; sleeve failures are independent of entry permission; disk-floor forecasts use measured growth. | Behavioral script regressions and dated unit/liveness observations in [STATE](../STATE.md) |
| Recovery | Tool instructions use supported verbs. Compatible state can use retained recovery paths; incompatible predecessors require forward repair. | Failing-before CLI controls, copied-WAL rehearsal and the executed exact-SHA forward handover |
| Venue ownership | Real local HTTP/WebSocket fixtures exercise signed clock/quota rejection, timeout then late fill, cancel/fill ordering and malformed account envelopes. | Current Linux suite and the test-only socket scheduling control in [evidence](tier1-round2-evidence.json) |
| Cleanup | Unused Rust recorder, redundant Python current-universe builder, orphan pack wrapper and dead deploy function are absent. Research, all venues, registered configs, Grafana and the enabled demo probe retain their consumers. | Source at `93404ff6`; original audit retained by tag `codex/round2-audit-input` |

### Evidence boundaries

| Boundary | Supported conclusion |
| --- | --- |
| R2-05 durability cost | Callback, queued-dispatch and attempted-send barriers serve distinct recovery obligations. Measured workloads report each cost and zero barrier failures; no redundant obligation is demonstrated. |
| R2-16 capacity | [Three 60-second workloads](execution-performance.md) report platform, quantiles, sampled CPU/RSS and WAL bytes. One child over 270 symbols is not 270 workers; no-fill growth is not steady-state disk usage. Missing source opportunities remain explicit. No universal memory ceiling, many-worker capacity or unloaded latency SLO follows. |
| Production evidence | Last deployed Linux checks pass 2,399 tests on93404ff6; current local macOS release passes 2,404. Authenticated snapshots establish the dated account/protection state, not a full-day execution or latency claim. |
| Constructed venue fixtures | Signed protocol responses establish engine handling; they are not authenticated private-stream captures. Position topics remain separate from the authenticated snapshot/history accounting authority. |
| Research parity | Research and live-worker populations are independently constructed; shared protocol code does not establish full cross-environment feature parity. |
| Operational fault exercise | A successful handover and advancing health establish the observed path. Unperformed funded rollback, crash/stall injections and external on-call delivery remain unverified. |

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
