# Tier-1 audit, round 2

## Purpose

Record what the 2026-09-06 read-only audit of tree `6b5ed1d5` plus its uncommitted diff found: the rating against a Tier-1 bar, the order path as production runs it, what to delete, what to simplify, which Tier-1 criteria are unmet, and the condition under which each legacy shim can go.

## Spec Tables

### 1. Scope and method

| Item | Value |
| --- | --- |
| Tree | `6b5ed1d5` + uncommitted diff: 25 modified files (+1051/−178), 4 untracked under `engine/engine-core/src/` |
| Concurrency | Codex edited the checkout during the audit; at 11:20 UTC `engine/engine-core/src/attribution.rs:134` referenced an undefined `me` and the tree did not compile |
| Local runs | `engine bench` release on macOS (embedded path); `cargo test --workspace`: 21 of 27 binaries ran, 1265 passed, 0 failed, engine-core lib target failed to compile mid-run; pytest 1540 passed in 138 s; clippy failed on the same compile error |
| Evidence standard | Every load-bearing claim re-derived from source by the auditor; rows marked *agent* are sub-agent counts spot-checked, not re-derived |
| Line numbers | As of the audited tree; the tree was moving |
| Sibling documents | [tier1-round-handoff.md](tier1-round-handoff.md) is state; this file is findings. When a row here is fixed, delete the row |

### 2. Verdict

| Bar | Rating | Basis |
| --- | --- | --- |
| Tier-1 execution engine (Jump/HRT-class) | **3 / 10** | Production runs a strategy execution path that none of sim, backtest, bench or release qualification boots. Per-order floor is three fdatasync barriers and seven WAL records. A fifth of the Rust tree is dead venue code. No CI on push to the deployed branch |
| Careful prop-lite crypto bot | 6 / 10 | Exact rational accounting, checksummed durable log, seeded fault simulator, Bybit parser with no unwrap on venue input, 74 run-path unwraps in engine-core all of the checked-above kind |
| What Tier-1 means here | Correctness, determinism, operations | No live sleeve is latency-sensitive: carry, long and exodus run on daily signals; the quoter is disabled. Microsecond work is not the gap |

### 3. Measures

| Measure | Value | Source |
| --- | --- | --- |
| Rust LOC / crate age / rate | 205,665 / 24 days (created 2026-08-13) / ~8.5k per day | `wc`, `git log` |
| Bench, unloaded, embedded path: think / market-to-submit p50 / p99.9 | 1.6 µs / 8.9 ms / 33 ms; two disk waits of ~4 ms each | `engine bench --events 2000 --rate 100` |
| Bench at full rate, embedded path: market-to-submit p50 / p99.9 | 3.87 s / 7.74 s (barrier queue collapse; think stays 83 ns) | `engine bench` defaults |
| Production callback hop | child process, JSON over pipes, strategy state serialized 4× per callback, snapshot of every symbol built and serialized 2× per callback | §4 |
| Who boots the production path | `engine run` and one integration test file | `engine/engine-core/src/runner.rs:114`, `engine/engine-core/tests/integration/strategy_process.rs:619` |
| Who boots embedded | sim, backtest, bench, account soak | `engine/engine-core/src/sim/harness.rs:347`, `engine/engine-core/src/backtest/runner.rs:394`, `engine/engine-core/src/bench.rs:179`, `engine/engine-core/src/account_state_bench.rs:373` |
| WAL format | internally tagged JSON in `[u32 len][u32 crc32c][payload]` frames; `EWAL0001` magic | `engine/engine-types/src/wal.rs:24`, `engine/engine-wal/src/lib.rs:4` |
| WAL record kinds: deployed `cece1d9f` / this tree | 32 / 58 | `git show cece1d9f:engine/engine-types/src/wal.rs` |
| Barriers per order, production / embedded bench | 3 / 2 | §4 |
| Dead venue code | 25,428 LOC direct + ~970 registry arms; 19 crates; 2,193 LOC hand-rolled Poseidon2/Goldilocks/Schnorr | *agent*, spot-checked |
| Tooling compiled into the funded binary with no path from `engine run` | ~14,800 LOC (23% of engine-core non-test) | *agent*, spot-checked |
| Python that never loads on the host | 25,145 of 27,653 LOC (91%); host Python path imports no third-party package | *agent* |
| Signal worker | 23,439 LOC, 14,052 non-test, for 6 public endpoints and 7 record kinds; ~3,100 LOC job | *agent* |
| CHANGELOG.md | 409 KB, 5,892 lines, 161 dated entries, 2026-08-24 to 2026-09-06 | `wc`, `grep` |
| STATE.md | 66 lines, 59 KB; one table cell of 48,862 characters | `awk` |
| Audit artifacts | `docs/tier1-audit.md` 30 KB + `docs/tier1-round-evidence.json` 834 KB + `docs/tier1-deployment-evidence.json` 67 KB; 273 sha256 entries all under `/tmp` on one laptop | `ls`, `jq` |
| Last 300 commits touching only CHANGELOG/STATE | 98 (33%) | `git log --name-only` |
| Commits by identity `Test` in the last 450 | 78 | `git log --format=%an` |
| CI triggers | `pull_request` (PRs are forbidden by policy) and `workflow_dispatch`; no `push` | `.github/workflows/vps-deploy.yml:5` |
| Run-path `unwrap`/`expect` in engine-core | 74 (≈66 reachable); zero `panic!`; 16 `unreachable!` after a local check | *agent*, method: count before the inline `#[cfg(test)] mod` |
| Venue JSON reaching an unwrap in the Bybit parsers | 0 | *agent* |
| Order structs / stores written per fill / "flat" implementations / quantity representations | 8 / 7 / 5 / 4 | §6 |
| Copies of the ~20-arm `select!` | 3 | `engine/engine-core/src/engine.rs:747`, `:817`, `:893` |
| `#[test]` attributes in the engine / Python tests | 1,976 / 1,540 | *agent*, `pytest --co` |
| proptest, quickcheck, arbitrary, cargo-fuzz, criterion | 0 | all `Cargo.toml` |

### 4. The order path as production runs it

Runtime is tokio current-thread. Production boots `CallbackExecution::Isolated`; the in-process branch at `engine/engine-core/src/ctx.rs:229` is dead in production.

| Hop | Where | Cost |
| --- | --- | --- |
| Quote parsed and coalesced per symbol | `engine/engine-marketdata/src/bybit/feed.rs:161` | borrowed serde, one mutex |
| Market event routed to each subscribed sleeve; all four live sleeves subscribe Quote+Ticker | `engine/engine-core/src/engine/scheduling.rs:498`, `engine/engine-core/src/strategy_process/host.rs:276` | a quote is dropped only while a prior callback for that sleeve is pending |
| Snapshot built for the callback: every symbol in the table, depth levels, owned orders, each row serialized twice for byte accounting | `engine/engine-core/src/strategy_process/snapshot.rs:11` | O(symbols) allocation and JSON per quote |
| Request assembled: strategy state (≤4 MB) cloned, snapshot cloned, JSON-encoded into 64 KiB frames, written to the child's stdin from a dedicated OS thread | `engine/engine-core/src/strategy_process/host.rs:466`, `engine/engine-core/src/strategy_process/mod.rs:47`, `engine/engine-core/src/strategy_process/wire.rs:68` | ≥3 `write(2)` per direction; `sync_channel(1)` + oneshot + `tokio::spawn` |
| Child deserializes the whole strategy, runs it, re-serializes the whole strategy | `engine/engine-core/src/strategy_process/worker.rs:17`, `engine/engine-strategies/src/runtime.rs:14`, `:34` | 2 full state (de)serializations |
| Parent deserializes the returned state twice | `engine/engine-core/src/engine/strategy_callbacks.rs:467`, `:798` | 2 more |
| Unchanged state and no actions: nothing written. Changed state: `StrategyCallbackQueued`, `StrategyCallbackPrepared` (embeds the full snapshot), `StrategyProcessTransitionQueued` | `engine/engine-core/src/engine/strategy_callbacks.rs:555`, `:578`, `:581`, `:639` | **barrier 1** at `:651`, even with no order |
| Intent admitted: `Intent`, `Verdict`, `OrderSent` | `engine/engine-core/src/engine/intent_admission.rs:481`, `:626`, `:1168` | buffered |
| Dispatch queued | `engine/engine-core/src/engine/order_dispatch.rs:36` | **barrier 2** |
| Dispatch attempted: `OrderDispatchAttempted` | `engine/engine-core/src/engine/order_dispatch.rs:176`, `:182` | **barrier 3** |
| Venue task sends; mainnet over trade WebSocket, demo over REST | `engine/engine-core/src/venue_runtime.rs:435` | venue is an enum, no dyn dispatch |

| Fact | Value |
| --- | --- |
| Record kinds written between "place" and wire | 7 |
| fdatasync barriers, each a `spawn_blocking` → std channel → sync thread → std channel → select wake | 3 |
| WAL appends whose payload contains `null` are re-parsed in full before the checksum | `engine/engine-wal/src/lib.rs:674` |
| Hottest record (`OrderUpdate`) goes through a `serde_json::Value` tree to rewrite its tag for old readers | `engine/engine-wal/src/lib.rs:154` |
| The 09-05 mechanism (incident `mainnet-4117d27a32d02421`) | this hop table without the unchanged-state short-circuit: snapshot + payload persisted per quote |
| Test of the short-circuit | one, embedded path only: `engine/engine-core/src/tests/order_path.rs:409`. No isolated-path test counts WAL records per quote |

### 5. Delete

Ranked by lines and risk removed. Nothing listed is reachable from a deployed unit.

| # | Delete | LOC | Prerequisite / note |
| --- | --- | --- | --- |
| 1 | binance, hyperliquid, lighter, mexc, variational under `engine/engine-venue/src/venues/`, `engine/engine-public/src/venues/`, `engine/engine-marketdata/src/`; collapse the 6-arm registry matches in `engine/engine-venue/src/registry.rs` | ~26,400 | Keep the signal worker's plain-HTTP Binance ratio fetch; it does not use this code. `engine/engine-venue/tests/venue/dormant_venues.rs` already declares the eight realms dormant |
| 2 | sim, backtest, bench, canary, takeover, clear, flatness, legacy_signals, timing, the fills report and the replay printer out of engine-core into a tools crate | ~14,800 | Not a deletion; the funded binary stops carrying it. `engine/engine-core/src/account_state_bench.rs` has one caller, an example |
| 3 | Python-sleeve state import: `engine/engine-strategies/src/native_long/state_import.rs`, `engine/engine-strategies/src/native_carry/state_import.rs`, `engine/engine-strategies/src/native_exodus/state_import.rs`, the import half of `engine/engine-core/src/takeover.rs`, `scripts/deploy_vps_live.sh:973-1067` | ~3,000 | Import is complete on both realms; the deploy branch is never reached. Keep `verify-native-strategy-state` and `initialize-native-strategy-state` |
| 4 | `engine/market-tape/` | 857 | Built, installed on the host, never run. Hardcodes the Bybit feed regardless of config (`engine/market-tape/src/main.rs:92`). The Python `market_tape/` is the live recorder |
| 5 | `docs/tier1-round-evidence.json`, `docs/tier1-deployment-evidence.json`, `docs/tier1-audit.md` | 931 KB | Replace with a 20-row table in the handoff: commit, CI run URL, counts, seed range, toolchain |
| 6 | `liquidity_migration/research/lab/` | 1,390 | Imported wholesale on 2026-09-02; referenced only by its own tests |
| 7 | `quoter` from `deploy/engine.mainnet.toml.template`; `probe` from `deploy/engine.demo.toml.template`; both plugs to the tools crate | 4,473 | Quoter is disabled yet subscribes AGIUSDT depth and runs its reducer per tick (`engine/engine-strategies/src/quoter/plug.rs:742`, `engine/engine-strategies/src/quoter/plan.rs:594`) |
| 8 | `configs/lane2_carry_hold_v1.json` through `v5` to `tests/fixtures/` | 55 KB | 62% of `configs/` by bytes, zero live references. Rust tests read them from disk, so move rather than delete |
| 9 | `deploy/grafana/` | 79 KB | Only consumer is a self-consistency test; a `.pyc` is committed inside |
| 10 | `liquidity_migration/data/universe.py`, `scripts/runtime/pack_market_tape.py`, `stop_fleet()` in `scripts/deploy_vps_live.sh` | ~200 | Orphans; a test pins the dead function's text |
| 11 | dev and plotting packages from the host install | n/a | `requirements.lock` puts mypy, pytest, ruff, matplotlib, Pillow, pyarrow and pybit on the trading VPS; the host Python path is stdlib only |

Deletable Rust before touching anything the engine uses: roughly 45k lines, over a fifth of the workspace.

### 6. Simplify

| # | Target | Now | Proposed | Blocker |
| --- | --- | --- | --- | --- |
| 1 | Execution model | isolated child process per sleeve; §4 | embedded strategies on the loop thread; delete `engine/engine-core/src/strategy_process/` (3,223 LOC); bench, sim, backtest and production become one engine by construction | If isolation is kept, sim and bench must boot isolated and one test must count WAL records per quote on that path |
| 2 | Order, position, quantity | 8 order structs; one fill writes 7 stores under 3 key schemes in 2 numeric types; `OrderRec` carries f64 `filled_qty` beside exact `fill_quantity` (`engine/engine-core/src/inflight.rs:26`); 5 "flat" functions; f64, `Exact`, `ExactNumber`, `OrderFillQuantity` | one exact quantity type; delete the f64 twins; one inventory with a derived physical net | §9 row 1: four constructors still send orders with no exact terms |
| 3 | Select loop | three copies of ~20 arms for halt, drain, normal | one arm list with mode predicates | none |
| 4 | WAL encoding | JSON via `serde_json::Value` for legacy tags; per-append `null` re-parse; 7 segment-base versions readable, 2 ever written on the host | freeze the record set (§10), fixed binary frames, one JSON dump tool; decide whether three barriers per order is a requirement | v1 readers stay until the host WAL is rotated past |
| 5 | Signal worker | 14,052 non-test LOC; second Bybit WebSocket client with the same URL literal and constants as `engine/engine-marketdata/src/bybit/feed.rs` (`engine/signal-worker/src/bybit_ws.rs:23`); five on-disk artifacts per batch; lanes with every chunk size hard-coded to 1; 10 published fields and 2 payload kinds no consumer reads; 16 of 81 config keys pinned to one literal | reuse the engine's public feed, one checkpoint file, sequential fetch, drop dead arms; ~3,100 LOC | none |
| 6 | Liveness | 8 independent heartbeat parsers in bash, Python and YAML; 12 unit files for 7 Python entry points under one user; no `WatchdogSec` anywhere | one supervisor process, one parser, `WatchdogSec` on the engine units | none |
| 7 | Deploy script | 1,342 lines, 91% inside a quoted heredoc that shellcheck does not lint; 260 LOC copy a binary and restart; 27 of 48 functions have one caller | a linted remote script file; core plus rollback | `scripts/vps/print_vps_recovery_command.sh:46` and `scripts/vps/vps_rescue_restore_ssh_access.sh:109` print modes `install` and `activate` that do not exist |
| 8 | STATE.md | 59 KB, one 48,862-char cell, 124 clock stamps | 60 lines of values: fleet, units, sleeves, dials, open faults, links | none |
| 9 | CHANGELOG.md | 409 KB in 13 days; 3 entries for one incident; 31 entries recording a refused deploy | one paragraph per matter, updated in place; archive before 2026-09-01; the on-call routine commits nothing on a refusal | none |
| 10 | Audit documents | 3 files + 900 KB JSON | handoff (state) + this file (findings) | none |

### 7. Tier-1 criteria

| Criterion | Status | Evidence |
| --- | --- | --- |
| Qualified path equals production path | Not met | §3 boot rows |
| CI on every commit to the deployed branch | Not met | no `push` trigger; `scripts/git-hooks/pre-push` is the only gate |
| Soak on demo before funded handover | Not met | 9 s between demo and mainnet handover on 2026-09-05 |
| Alert on the fault, not the stop | Not met | 15 MB/s WAL and 40 errors/s for 4 min; pages fired on the manual stop |
| Rollback under a minute, one command | Not met | ~6 min with hand quarantine of WAL and spool |
| Panic behaviour | Weak | main-loop panic exits 101 with no flatten; `Restart=always`, `RestartSec=5`, no start limit: an unbounded crash loop |
| Recorded venue frames through the real parser in CI | Not met | zero private-stream fixture files; 5 public frames captured once on 2026-08-13 |
| Property or fuzz tests on WAL and parsers | Not met | no proptest, arbitrary or fuzz target |
| Published latency with a CI budget | Partial | 60 s HDR ledger and per-command `VenueTiming` exist; not one number in any doc |
| Zero unwrap on venue input | Met | §3 |
| Deterministic replay of a production day off-host | Partial | sim is byte-identical on synthetic markets; the real-WAL fixture test is `#[ignore]` and its bundle is not on any machine checked |
| Resource envelope enforced | Partial | `MemoryMax=2G` held; no disk or I/O bound; 7 GB written in 4 min |
| State snapshot one screen, present tense | Not met | STATE.md row in §3 |
| One audit document with reproducible receipts | Not met | §3 audit artifacts row |

### 8. Untested venue-boundary scenarios

| Scenario | Status | Evidence |
| --- | --- | --- |
| Send gets no answer, then the fill arrives later on the private stream | Absent | the no-answer test stops at "in flight" |
| Clock skew, `recv_window`, Bybit error 10002 | Absent | zero tests reference any of them |
| Rate limit reaching engine-core | Absent | `record_quota_hold` in `engine/engine-core/src/engine/venue_completion.rs` has no test; adapters test 10006 only |
| Partial fill racing a cancel, on Bybit, through the engine | Absent | exists for Binance request shapes only |
| Private `position` topic | Absent | no test carries that frame |
| HTTP timeout with an order in flight | Absent as such | `VenueError` has no timeout variant; a transport reset stands in |
| Isolated-path WAL volume per quote | Absent | §4 last row |
| 108 "fail-before/pass-after cases" | Mutation-kill, not fault injection | regex edits to source, run once on one laptop; 78 distinct killer tests; 107 of 108 in accounting, WAL and recovery code, 1 in venue code, 0 in marketdata |
| Direct tests on the fill handler, intent admission and scheduler | 0, 0, 2 | `engine/engine-core/src/engine/venue_completion.rs`, `engine/engine-core/src/engine/intent_admission.rs`, `engine/engine-core/src/engine/scheduling.rs`; exercised only end to end through one scripted `MockVenue` |
| Wall-clock async tests with real timeouts of 100 ms to 5 s | ~170 | flake surface, not determinism |

### 9. Legacy shims: what each reads, when it can go

| Shim | Where | Reads | Deletable when |
| --- | --- | --- | --- |
| f64 → `Exact` bridge (`from_legacy_f64`, `LegacyBinary64`, `OrderFillQuantity`) | `engine/engine-types/src/numeric.rs:274`, `engine/engine-types/src/wal.rs:852` | every f64 quantity in the host WAL, which is 100% v1 | **Still written.** Four production constructors send orders with no exact terms: `engine/engine-core/src/order_dispatch.rs:223`, `engine/engine-core/src/working.rs:184`, `engine/engine-core/src/portfolio_protection.rs:201`, `engine/engine-core/src/canary.rs:382`. Fix those, then rotate both realms past the last f64 row |
| Quantity-grid adoption (`LegacyQuantityGridAdopted`) | `engine/engine-core/src/legacy_quantity.rs`, `engine/engine-core/src/legacy_quantity/` (untracked) | f64-accumulated sleeve positions built by `cece1d9f` | one-time boot migration; after both realms boot the candidate once and rotate to a v7 base; blocked by the row above |
| Legacy signal-source retirement (`retire-legacy-signal-sources`) | `engine/engine-core/src/legacy_signals.rs` | three stopped sources per realm with no terminal frontier | one-time; after execution and v7 rotation; the record variant stays readable |
| Unmanaged signal-source identity (`legacy_signal_lane`, `ReadinessRequest::Legacy`) | `engine/engine-types/src/signal_lifecycle.rs:148`, `engine/signal-worker/src/worker/lifecycle.rs:18` | the fleet's current worker output | **Not a shim.** It is production format until the managed worker is deployed on both realms |
| Python-sleeve import codecs | §5 row 3 | retired Python sleeve files | now |
| v1 record-tag aliases and required-field checks | `engine/engine-wal/src/lib.rs:87-480` | the whole host WAL | v1 readers stay while the host WAL is read. Readers for `segment_base_v2` through `v6` guard files that never existed on the host (v5 only in quarantine) and can go now |
| Retired shapes (`ControlAnchor`, `TargetBookLatch`, `ClaimsDropped`) | `engine/engine-types/src/wal.rs:334`, `:426`, `:419` | archive segments | keep as tolerant readers or drop with `replay_chain` |
| `Names` → `IdentityState` | `engine/engine-core/src/identities.rs:31` | WAL with `Names` only | after v7 rotation on both realms; then stop writing both at boot |
| Worker coverage "legacy pair" | `engine/signal-worker/src/history.rs:5` | the fleet worker's checkpoint | after the worker deploy and one checkpoint cycle, if rollback to `cece1d9f` is abandoned |
| Precision marker `ExecutionPrecisionV1` | `engine/engine-core/src/engine/boot_recovery.rs:321` | appended unconditionally at boot so the deployed reader refuses the log | a one-way door: the first candidate boot ends rollback to the fleet binary without hand quarantine |

### 10. WAL record set

| Fact | Detail |
| --- | --- |
| Variants | 58; 26 added since `cece1d9f`, 0 removed |
| Zero production writers | `ControlAnchor`, `TargetBookLatch`, `ClaimsDropped` |
| Tooling-only writers | `LatchCleared` (`reconcile-clear`), `LegacySignalSourceRetired` (CLI) |
| Exact duplicate | `FastExecution` has the same ten fields as `OrderUpdate::FastFill` and is written instead of it (`engine/engine-core/src/engine/venue_completion.rs:1355`) |
| One order's request serialized | up to 4× across `Intent`, `OrderDispatchQueued`, `OrderSent`, `OrderLineageRestored`, then in `SegmentBase` |
| Both still written | `StopSet` and `SleeveStopSet`; `Names` and `IdentityState` |
| Same input written per phase | `StrategyCallbackQueued` and `StrategyCallbackPrepared` carry the same `StrategyCallbackInput`; `StrategyTransitionQueued` ⊂ `StrategyProcessTransitionQueued` |
| Segment-base versions readable / ever written on the host | 7 / 1 |
| Boot parse cost | every host fill and order record parsed twice: once to the struct, once to a JSON tree for compatibility rewrites (`engine/engine-wal/src/lib.rs:221-308`) |
| Frame overhead | ~170-250 B of JSON scaffolding around ~70 B of data per fill; `Exact` as `{"n","d"}` strings |

### 11. Periphery

| Area | Finding | Where |
| --- | --- | --- |
| Deploy pipeline | 26 workflow steps, one deploys; the 17-line SSH-fingerprint block is byte-identical in three jobs; mode validated three ways | `.github/workflows/vps-deploy.yml` |
| Deploy script | `stop_fleet()` dead, pinned by a text-slicing test; recovery scripts print nonexistent modes | `scripts/deploy_vps_live.sh:521`, §6 row 7 |
| Units | `deploy/systemd/README.md` says 5 independent families, `deploy/fleet_manifest.tsv` marks 6; equity-recorder absent from the README | |
| Config renderer | Rust `render-native-config` generates the deployed `config_json`; Python only reads it back. Strategy decisions live in Rust; `liquidity_migration/rules/` (2,724 LOC) constructs features and asserts parity, and is loaded by no host process | |
| Feature duplication | LONG/CARRY feature formulas exist in the worker (`engine/signal-worker/src/features.rs`) and in Python research; the depth-ladder weight is executable Rust and a Python docstring, untested against each other | |
| Env toggles | 31 keys, all read somewhere; `scripts/devtools/repo_doctor.py` already enforces this | |
| Python tests | 32.5k LOC, not copy-paste; effort inverted: `liquidity_migration/core/` (host runtime) has the lowest test ratio, `liquidity_migration/data/` (never on a unit) the largest test directory | |
| Clock | `engine/engine-venue/src/lease.rs:446` calls `SystemTime::now()` directly, bypassing the virtual clock | |
| Doc contradictions | repository private vs public; heartbeat cadence 5 s vs 30 s vs 60 s; "required GitHub checks" that do not exist on push | `docs/operations.md`, `docs/engine.md`, `docs/observability.md`, STATE.md |

## Invariants

- Must qualify the binary configuration that trades: any harness that claims to qualify the engine boots the same `CallbackExecution` as `engine run`.
- Must not add a `WalRecord` variant without a construction site reachable from `engine run` and a reader test on the previous segment version.
- Must fix the four exact-terms-free order constructors before deleting any f64 shim; must not delete the unmanaged signal-source path until both realms run the managed worker.
- Must not count a source mutation as a fault test, and must not count a test that runs on one laptop as evidence.
- Must keep this document present tense: a fixed finding is a deleted row, not an annotated one.
- Must not cite a repository path here that does not exist; `tests/repo/test_docs_links.py` enforces it.

## Operational Recipes

```sh
# Pinned toolchain for every cargo command below.
export PATH="$(rustup run 1.90.0 rustc --print sysroot)/bin:$PATH"
```

```sh
# Which boot path each harness uses (production is boot_as_isolated).
grep -n 'Engine::boot_as_isolated\|Engine::boot_as(\|Engine::boot(' \
  engine/engine-core/src/sim/*.rs engine/engine-core/src/backtest/*.rs \
  engine/engine-core/src/bench.rs engine/engine-core/src/runner.rs \
  engine/engine-core/src/account_state_bench.rs engine/engine-core/tests/integration/*.rs
```

```sh
# Per-order cost on this box, embedded path, unloaded and at full rate.
cd engine && cargo build --release --locked --bin engine
./target/release/engine bench --events 2000 --rate 100 --every 20 --wal /tmp/bench-unloaded.wal
./target/release/engine bench --wal /tmp/bench-loaded.wal
```

```sh
# WAL record kinds, and how many the deployed commit had.
awk '/^pub enum WalRecord/,/^}/' engine/engine-types/src/wal.rs | grep -cE '^    [A-Z][A-Za-z]+'
git show cece1d9f:engine/engine-types/src/wal.rs | awk '/^pub enum WalRecord/,/^}/' | grep -cE '^    [A-Z][A-Za-z]+'
```

```sh
# Dead venue reach: the only venue strings outside engine/.
grep -rn 'venue = "' deploy/*.template
cd engine && cargo tree -i k256 --prefix depth && cargo tree -i sha3 --prefix depth
```

```sh
# CI trigger, run-path unwraps, STATE.md cell size, changelog density.
sed -n 5,10p .github/workflows/vps-deploy.yml
for f in $(find engine/engine-core/src -name '*.rs' -not -path '*tests*' -not -path '*/sim/*' -not -path '*/backtest/*' -not -name 'bench.rs' -not -name 'account_state_bench.rs'); do awk '/^#\[cfg\(test\)\]/{exit} {print}' "$f" | grep -c '\.unwrap()\|\.expect(' ; done | paste -sd+ | bc
awk '{print length}' STATE.md | sort -rn | head -1
grep -c '^- \*\*20' CHANGELOG.md
```
