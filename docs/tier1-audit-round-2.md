# Tier-1 audit, round 2

## Purpose

Define the remaining Round-2 qualification and migration conditions retained by Round 3.

## Spec Tables

### Scope and authority

| Item | Contract |
| --- | --- |
| Product | Durable execution, exact accounting, deterministic strategy decisions, independent sleeves and pluggable venues |
| Current plan | [Round 3](tier1-audit-round-3.md) owns callback execution, venue features, WAL write kinds, order durability and supervision |
| Runtime specification | [engine.md](engine.md); source and behavior tests take precedence over ratings or audit assertions |
| Implementation checkpoint | [tier1-round-handoff.md](tier1-round-handoff.md); [compact qualification evidence](tier1-round2-evidence.json) |
| Operational authority | [STATE.md](../STATE.md) records a dated host observation; local tests do not update that observation |
| History | [CHANGELOG.md](../CHANGELOG.md); resolved findings leave this table after integration and verification |
| Qualification language | Local execution, historical child-process fixtures and authenticated venue evidence have distinct scopes; only the current production callback mode qualifies new execution claims |

### Retained architecture

| Area | Decision | Alternative and reason |
| --- | --- | --- |
| Venues | Retain Bybit, Binance, Hyperliquid, Lighter, MEXC and Variational implementations and their typed registry | Deleting adapters because their realms are dormant removes required product capability; dormant is a live-evidence status, not dead code |
| Account ownership | Exact sleeve inventories determine one physical net; display/legacy projections have no admission authority | Merging virtual ownership with physical net loses opposing sleeves and their independent stops |
| Research | Keep reusable research/data capabilities; remove only proven redundant implementations and stale interfaces | Being absent from a trading unit is expected for a research tool and is insufficient deletion evidence |
| Grafana | Retain the dashboard renderer and published dashboard definition | The recorder, observability runbook and user workflow consume them; a self-consistency test is not their only consumer |
| Demo probe and disabled maker | Preserve stable strategy identity and the enabled demo measurement probe; suppress unnecessary work only when state/holdings permit | Deleting template blocks changes persisted IDs and removes an enabled demo function |
| Legacy state | Remove import/migration writers only after both realms and the retained rollback/replay contract no longer need them | A local migration test does not establish completed host migration |

### Open qualification

| ID | Required outcome | Current evidence and remaining work |
| --- | --- | --- |
| R2-15 | Replay a sanitized, complete recorded production day with matched input and accounting scope | Copied private-prefix boot/rotation/reboot passes; the 123-second public tape replays byte-identically with zero fills. Neither establishes a complete production day. Full retained-family acquisition and replay remain incomplete qualification. |

### Evidence boundaries

| Boundary | Contract |
| --- | --- |
| Implementation | [Engine ownership](engine.md) and [current qualification](tier1-round-handoff.md) define the implemented callback, risk, durability and recovery paths |
| Workload measurements | [Execution measurements](execution-performance.md) retain platform, source boundary, all opportunities, quantiles and resource limits; a short synthetic workload does not establish full-day accounting or steady-state capacity |
| Production evidence | [STATE.md](../STATE.md) records authenticated account/protection and loaded-image observations; local checks do not update the host |
| Venue fixtures | Constructed protocol fixtures and captured demo frames have separate provenance; neither replaces authenticated account/history reconciliation |
| Research parity | Independently constructed research and worker populations require matching input and accounting scope |
| Operations | Soak and rollback outcomes require actual host execution; local script tests establish only the exercised fixture behavior |

### Legacy removal conditions

| Interface | Removal condition |
| --- | --- |
| Legacy binary64 quantities and grid adoption | Both realms have booted canonical migration and no retained WAL family/rollback reader depends on the writer; exact accounting and normalized legacy contributions remain distinguishable |
| Legacy signal-source retirement command | Planned stopped-source suffixes are retired on both realms and retained WAL state carries their final frontier |
| Unmanaged producer identity and legacy worker checkpoint pair | Both realm workers publish the managed format and the retained rollback contract no longer needs the previous checkpoint |
| Python-sleeve import writers | Authenticated deployment verifies canonical native state on both realms; initialization/verification commands remain |
| Legacy Names reader | Every retained Names-only family remains readable until its replay/rollback requirement is retired; current writes use append-only IdentityState |
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
# Current production callback mode; synthetic market and venue.
cargo build --manifest-path engine/Cargo.toml --release --locked -p engine-tools --bins
engine/target/release/engine-tools bench --events 2000 --rate 100 --every 20 --wal /tmp/tier1-round2-bench.wal
```
