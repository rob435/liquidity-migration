# Trading-platform audit

## Purpose

Record the audit baseline and each finding’s local resolution, verified evidence and remaining limitations.

## Spec Tables

### Decision index

| Order | Finding | Resolution | Remaining boundary |
| --- | --- | --- | --- |
| 1 | **A-001: discarded exits** | Retained cooperative draining, explicit opening refusals, durable state/effect transitions and restart suffix recovery | Trusted synchronous callbacks cannot be preempted; callback output allocation is not globally bounded |
| 2 | A-002: input lifecycle | Explicit consumed/rejected/retained outcomes, bounded payload ownership, per-consumer backpressure and prefix recovery | Historical source identity metadata remains retained; a no-op is pending, never falsely consumed |
| 3 | A-003: startup readiness | Fresh nonce exchange, generation frontier catch-up and request-time rewind checks govern growth and existing entries | No producer response keeps growth suspended; erased legacy history is not reconstructable |
| 4 | A-004: ownership | Cancel/amend/stop effects carry caller identity; ordered stateful effects survive rotation/restart | Exclusive symbol ownership and trusted native plugs remain deliberate policies |
| 5 | CL-01–CL-15 | Typed boundaries, public/private crate separation and explicit admission/completion/worker/reducer phases | Wire rows outside selected envelopes remain dynamic; file movement alone is not an architecture fix |
| 6 | CL-16–CL-22 | Shared manifest versions, measured pure-risk target consolidation and ownership maps | Process isolation and real I/O tests retained; no unsupported latency or allocation claim |
| Retain | Prefix/availability, foreign-owner admission, timers, latency and candle coverage | Existing regressions remain enabled | Local tests do not establish live account parity |
| Decided | Portfolio allocation, stable identity migration and exact accounting units | Retain current exclusive ownership, durable dense ordering and validated numeric semantics | Revisit only with a concrete adapter/strategy requirement and a complete fee/stop/legacy migration contract |

### Evidence boundary

| Item | Scope |
| --- | --- |
| Accepted baseline | Local `main`, `f69a5fbf63afe11da78dde8bcf06a0ab6ba75046`; includes Claude's module extraction and the integrated timer, availability, p99.9 and candle-coverage changes |
| Local implementation | The source hashes and implementation checkpoint in [resolution evidence](tier1-audit-resolution.json) identify the candidate; the baseline remains a separate evidence boundary |
| Inputs | Original handoff's 30 `LM-T1` tickets, the existing audit and Claude's 22 pasted findings; each maps to a row below |
| Regression boundary | Accepted audit baseline is retained in [verification results](tier1-audit-verification.json). Fail-before probes and integrated candidate checks are recorded in [resolution results](tier1-audit-resolution.json); new-boundary probes use the candidate with the relevant fix absent |
| Integrated checks | [Resolution results](tier1-audit-resolution.json) records candidate debug/release suites, strict Clippy, formatting, Python fixture consumers, failure probes and comparable build measurements |
| Compiler | Actual Rust 1.90.0, `aarch64-apple-darwin`; `PATH`, `RUSTC` and `RUSTDOC` explicitly select the repository pin |
| Lint scope | Separate opt-in Clippy inventory uses `--workspace --lib --bins`; counts exclude test builds and do not establish a performance regression or fault count |
| Performance scope | Compiler sizes and source structure are verified; Claude's build CPU-seconds, serial-test timings and allocation/speedup estimates have no supplied reproducible timing artifact and are not adopted |
| Production / GitHub | This audit does not inspect the host, accounts, live WAL or current GitHub settings. Local integration and passing tests do not establish deployed behavior; the handoff's branch-protection/signature claims are not current evidence |
| Existing qualification | The [qualified local artifact](tier1-release-qualification.json) names `15c60924abfb9f5c7848b7ee7b4c5853f2b932d3`, Rust 1.90/macOS ARM and its workloads; it does not qualify the later merged revision or cross-version WAL compatibility |
| Authority | Code/tests establish implemented behavior; [AGENTS.md](../AGENTS.md) and the owner's current request govern work. The owner authorizes local implementation and architecture decisions; funded deployment, capital, credentials and live-state changes remain unauthorized |
| Path notation | Rust crate paths below are relative to `engine/`; function names identify the source more reliably than old line numbers |

### Execution defects and contract resolutions

| ID | Implemented resolution | Verified regression evidence | Remaining limitations |
| --- | --- | --- | --- |
| **A-001** | `scheduling::drain` retains ordered work across cooperative slices instead of clearing the queue; oversized openings receive explicit refusals. Stateful callback effects are journaled together before checkpoint/input commit, with placement IDs and completion indexes restored before boot callbacks | Flood sizes 68/255/256/257/1024 retain exits; another sleeve's exit survives. `strategy_checkpoints` covers order/checkpoint ordering, checkpoint-write and order-barrier failure, checkpoint/send crash cuts and the ten-second stalled recovery deadline. `scheduler_fairness` covers 1,024 durable effects, private input, rotation and restart. Fail-before results are in the resolution artifact | Plain order-only callbacks preserve optimistic WAL submission and existing crash semantics. Synchronous callback execution and output allocation remain trusted; the slice bounds dispatch, not arbitrary plug code |
| **A-002** | `SignalState` owns accepted payload capacity using the existing 256-row/64-MiB envelope plus one missing-prefix slot. Ordinary no-op delivery backpressures its destination. LONG/CARRY malformed inputs emit explicit durable rejection; terminal outcomes release payloads only after a successful barrier. Admission rechecks capacity immediately before WAL append | No-op backlog fails on baseline; rejection regression fails before implementation. Durable-signal tests cover independent consumers, full capacity, prefix recovery, failed rejection barrier, rotation/restart and distinct terminal outcomes | Historical cursor/route/subscription identities remain retained. Consumer no-op is pending work; no deadline fabricates consumption. The byte limit estimates retained observation allocations; collection overhead and historical identity metadata are outside it |
| **A-003** | Required producer readiness uses fresh boot nonce, exact source generation and published frontier; missing rows remain durable gaps. Rewind compares the request-time accepted prefix, allowing concurrent newer acceptance. Missing/malformed/I/O-failed handshake keeps growth suspended and retries; existing opening orders cancel while reductions/stops remain available | Baseline restored-opening probe fails before readiness. Added failing probes cover the existing-entry cancellation shortcut, true rewind, malformed-response abort and request100/accepted101/response100 race. Tests also cover absent producer with attributed exits/stops, cancellation-safe challenge exchange, catch-up and restart | Producer-declared frontiers cannot prove erased legacy data or omitted retired-generation tails. Availability and venue reconciliation remain separate checks. No live producer or WAL migration is performed |
| **A-004** | `PendingAction` binds every callback effect to its emitting strategy across immediate/deferred execution. Cancel/amend validates owned order plus symbol; stop dispatch rechecks attributed owner. Durable transition state and explicit completion replace separable checkpoint/effect ownership | Foreign cancel/amend/stop regressions fail before the fix; positive owner fixtures retain their original assertions using real WAL/order/attribution seeds. Private-update phase tests preserve WAL, risk, delivery and dedup through failure/late fills/restart | No hard callback isolation, account-wide virtual allocation or new capital policy. Engine-owned maintenance remains explicitly unscoped. Dispatch binding does not create shared-symbol portfolio semantics |

### Claude findings: all 22 reviewed

| ID / kind | Accepted baseline finding | Local resolution, evidence and remaining limits |
| --- | --- | --- |
| CL-01 / maintenance | JSON response parsing uses dynamic `Value` across **six** venue modules; `engine-venue/src` has no `Deserialize` derives. This is maintenance/allocation scope, not proof every reply is wrong. Manual helpers already report some field errors; the supplied 254-reference count has no defined scope  | Implemented typed Bybit/Binance acknowledgement/error envelopes and Bybit/Binance/Hyperliquid private envelopes in venue `parse.rs`/`ws.rs` modules; `engine-venue/src/wire.rs` owns shared optional-field and ID decoding. Three golden malformed/escaped/duplicate-key contract matrices pass on baseline and candidate; local reconnect/expiry/reset/dedup tests pass. Dynamic account/order rows remain; no allocation or latency gain claimed. LM-T1-207 |
| CL-02 / performance candidate | Compiler confirms `MarketEvent` is **1,648 bytes** and `on_market` takes it by value. The four helpers at Bybit state lines 345–366 are **test-only**. Actual production `replace_side`/`apply_side` each take an 808-byte `Levels` value. Futures are 20,504 bytes in public Binance and 19,616 in worker main  | Bybit L50 mutation helpers now borrow `Levels`; existing depth/reset tests pass. Keep inline L50 and ownership transfer into the core event loop. No future boxing or event-layout migration without workload evidence; no speedup claim. LM-T1-201/206 |
| CL-03 / maintenance | Heartbeat and lease notes use manual object assembly with serde string escaping. The merged heartbeat has **63 top-level fields**, not 17; nulls, integer-looking floats, two-decimal bps and four-decimal shares are intentional output details  | Typed `HeartbeatOutput` and `LeaseNote` replace manual object assembly. Heartbeat differential test compares exact old/new bytes across missing, zero, non-finite, extreme and escaped values; lease byte fixtures preserve optional funded fields, spaces and newline. Atomic publication/locking are unchanged |
| CL-04 / maintenance | `WorkerError.category` is a private string and the lane-local decision matches `input`/`network`. Current production source has **47** `EngineError::State(...)` expressions, **24** immediately wrapping `format!`; supplied 45 is stale  | `WorkerErrorCategory` enumerates config/input/state/network/I/O/JSON outcomes; classification/display regression covers every variant. Input/network remain lane-local. Engine invariant diagnostics retain descriptive `State(String)` where no caller branches on text; a blanket error rewrite is not adopted |
| CL-05 / maintenance | Independent gateway/public/private symbol maps are confirmed: **12** exact poisoned-read expressions and **8** dense-ID construction expressions across current production source; supplied 14/7 differs  | `engine-public::symbols::SymbolCatalog` owns five gateway forward/reverse maps through one append operation; Variational retains its ordered vector; shared private-feed learning preserves dense order, case, duplicates and overflow. Admission still checks separate core/public/private/gateway IDs. No namespaced WAL identity migration. LM-T1-005/207 |
| CL-06 / transition complexity | `take_venue_completion` spans 360 physical lines; `take_update` 306; `prepare_intent` 344. Repeated matching and mixed accounting/transport phases are present; the supplied first-function boundary/343 count is inaccurate  | Typed `RiskApprovedIntent`, `LegalOrder` and `ProtectedOrder` enforce admission phase order. Pure `CompletedMutation::bind` matches venue commands; private validation/journaling has narrow state access and produces the token required for accounting. Exact partial/cancel/late-fill ordering and failed-append retry tests pass on baseline and candidate; integrated suites verify the combined result. Later accounting/completion handlers still receive mutable Engine access |
| CL-07 / recovery complexity | `boot_as` spans **612** physical lines; Clippy counts 518 non-comment/nonblank lines and cognitive complexity 39/25  | Boot configuration/checkpoint validation, durable input restoration/routing and recovered reservations return typed outputs under `engine-core/src/engine/boot_recovery/`. Venue reconciliation remains centralized. Legacy-WAL, recovery, restart and input regressions remain enabled; no parallel authority or generic recovery framework |
| CL-08 / duplication | Bybit/Binance/Hyperliquid repeat stream startup, receive, reconnect/backoff and acknowledgement bookkeeping. Bybit/Binance `run`, `next_update`, reconnect and backoff bodies match after whitespace/comments/literals are removed. HMAC-SHA256 helpers repeat in Bybit/Binance/MEXC; 609 duplicate lines is not independently established  | Shared HMAC primitive and stream cancellation/backoff/acknowledgement memory remove duplicate mutable ownership. Independent signing vectors and a real local HTTP/WebSocket failure/expiry/reset/dedup/drop test pass. Authentication, listen keys, snapshots and subscriptions stay venue-specific; no generic all-venue reconnect actor claimed |
| CL-09 / dependency boundary | `engine-marketdata` directly depends on `engine-venue` for realms, public parsers/markets and **`VariationalGateway` for polling**. Extracting realm enums alone cannot remove this dependency; 12.5-second build timing is unverified  | `engine-public` owns nonsecret realm metadata, public catalog/REST clients and HTTP/TLS transport; `engine-marketdata` no longer depends on private adapters. Explicit venue `RealmCredentials` provides secret reads. All repository callers, realm fences and adapter registry tests updated; no live venue access |
| CL-10 / worker complexity | `Worker::apply` spans **464** physical lines; `handle_lane_completion` **278**, with existing adjacent test modules. Responsibilities include replay-sensitive coverage and publication, not just dispatch syntax  | Worker dispatch delegates typed history, funding, instruments, ticker, universe, gate and coverage phases; live lane completions have separate handlers. A newly exposed rejected-batch defect is fixed by preparing candidate state before durable commit. Regression fails on baseline (memory advances without journal), then passes with restart/retry; existing empty-frontier and pending-transaction failures remain covered |
| CL-11 / file organization | `signal-worker/src/live.rs` is **4,037 lines**, the largest current Rust source file; scheduling, heartbeat, lane creation and fetch/repair code are visible regions  | `live/lanes.rs` owns completion phases; `live/acquisition.rs` returns bounded fetched inputs without mutating the live runner. `LaneContext` groups current lane/stream state. File movement is navigation work; explicit commit/result boundaries provide the ownership change. Serialized `WorkerState` is unchanged |
| CL-12 / reducer complexity | Pinned Clippy confirms CARRY **57/25**, LONG **43/25**, Exodus **39/25** in the cited reducers  | CARRY/LONG/Exodus reducers have explicit lifecycle phases. A 42-test corpus captures 62 complete serialized outputs before/after: checkpoint bytes, numeric values and ordered effects match exactly. Actual Python/Rust fixture consumers pass with a freshly rebuilt binary. No strategy policy, fingerprint or serialized layout change |
| CL-13 / operator-tool complexity | Canary `cleanup` spans **246** physical lines and complexity **57/25**. It is an operator demo tool with late-fill/ambiguous-submit cleanup behavior  | Canary cleanup uses `OriginalDisposition` and `RecoveryClose` enums instead of independent contradictory flags; receipt and inventory phases have narrow owners. Fifteen tests pass on baseline and candidate, including late fills, ambiguous close-once, missing receipt and stale consecutive scans; no live submission |
| CL-14 / corrected false alarm | `native_exodus/state_import.rs` is 1,213 lines, but **`parse_legacy_tape_object` is 17 lines (650–666), not 566**. A `'{'` literal breaks naive brace counting. Translation is used through the native takeover/state-initialization tooling  | Retain the importer and its compatibility tests. The alleged giant parser is a corrected false alarm; historical import/takeover remain supported. No retirement based only on checkpoint existence |
| CL-15 / CLI maintenance | `replay::one_line` spans **384** physical lines; core `main::dispatch` **339**. There are no inline argument-parser unit tests in `main.rs`; binary smoke checks exist in release qualification  | CLI dispatch delegates command handlers; backtest/benchmark argument parsing returns typed options before any runtime or file access. Three argument/routing/error tests pass. Keep the pure exhaustive WAL display match: splitting it into fallible family wrappers would add states without removing execution coupling |
| CL-16 / test organization | Large inline `#[cfg(test)]` modules exist in venue/core/strategy files. Raw source size includes test code; the supplied aggregate inline-line counts are not reproduced with a documented method  | Heartbeat tests move to sibling modules with the complete legacy renderer retained test-only for byte comparison. Existing private test access and discovery are preserved. File organization is not a runtime or compile-time performance claim |
| CL-17 / build candidate | Cargo metadata confirms **33 test-enabled targets**, including **19 integration targets**; venue/risk/WAL contribute **10/6/2**. The supplied 151/325 CPU-seconds and 14.2-second request-shape timing are unverified  | Six pure risk integration targets consolidate into one namespaced target; exact discovery retains 97 integration tests plus two unit tests. Comparable same-source cached-dependency measurements are in the resolution artifact. All ten venue integration targets, including environment-mutating tests, retain process isolation |
| CL-18 / test timing | Real sleeps and zero `tokio::time::pause`/`start_paused` uses are confirmed; the supplied 54-call and 17.6-second serial totals are not adopted. Engine clocks use `std::time` with an existing thread-local virtual override  | Retain real I/O/thread synchronization and engine virtual-clock tests; use the existing ten-second mutation-drain deadline for stalled recovery. No blanket Tokio pause conversion or ignored correctness coverage; measured timing belongs in the resolution evidence |
| CL-19 / dependency maintenance | Repeated manifests are maintenance scope; the baseline unused-dependency claim is contradicted by compilation and `engine-types/src/lib.rs` plus venue implementations  | Centralized workspace versions preserve per-consumer features; resolved external package IDs and features match exactly. Remove redundant private-crate transport declarations now owned by `engine-public`. Correction from compilation: `async-trait` is actively reexported/used by the pinned venue trait and implementations, so it is retained; syn 3 remains |
| CL-20 / optional lints | Production-only pinned-Clippy counts appear below and differ from several pasted figures. Long functions and lint warnings are maintenance signals  | Keep the existing lint policy; fix affected strict-Clippy diagnostics individually. Do not turn optional counts, string spelling or function length into a new workspace gate or a performance claim |
| CL-21 / allowance inventory | The named categories total **26**: 12 `too_many_arguments`, 8 `large_enum_variant`, 6 `async_fn_in_trait`; **three additional production allowances** cover partial-order comparisons/range loops. `live.rs` has four `too_many_arguments` sites  | Use `HistoryBatch` and `LaneContext` for real shared worker inputs; retain intentional fixed-layout enums and async-trait allowances. No blanket lint suppression removal. Remaining broad adapter/acquisition signatures are not mislabeled execution faults |
| CL-22 / documentation | `docs/engine.md` has a detailed engine-core module map but no equivalent worker/venue maps  | Added worker and venue/public ownership tables to `docs/engine.md`, including durable effect ownership and current input lifecycle. Navigation evidence only. LM-T1-208/209/308 |

### Pinned-Clippy inventory

| Opt-in lint | Production diagnostics at the accepted baseline |
| --- | ---: |
| `or_fun_call` | 26 |
| `redundant_clone` | 5 |
| `format_push_string` | 32 |
| `large_types_passed_by_value` | 3 |
| `needless_pass_by_value` | 24 |
| `str_to_string` | 526 |
| `unreadable_literal` | 140 |
| `too_many_lines` | 73 |
| `cognitive_complexity` | 11 |
| `large_futures` | 2 |

### Other pasted qualifications

| Claim | Disposition |
| --- | --- |
| `embedded_code_msg` is seven lines | Confirmed in Binance parsing; do not restore the discarded 1,179-line alarm |
| Lighter crypto vectors and signal delivery tests are test-only | Confirmed at their `#[cfg(test)]` module declarations |
| Keep `WorkerState`'s serialized field layout | Preserve the wire/checkpoint contract; an internal refactor can use an explicit serialization DTO. The claim that serde makes all restructuring impossible is not established |
| Timers belong to the other branch | Timer fixes are integrated at the verified revision; superseded rearm nodes are removed and at most 64 due keys are dispatched per turn. Distinct active timer IDs remain uncapped |
| 144 unwrap/expect sites are mostly justified | The aggregate count/justification is not adopted without site-level review; no blanket unwrap/expect lint is proposed |
| Start with test binaries, sleeps and dependency work | Revised priority: A-001 first, then lifecycle contracts; take cheap build improvements where measured and behavior-preserving |

### Original handoff: every ticket retained

| Ticket | Resolution / decision | Evidence and remaining boundary |
| --- | --- | --- |
| LM-T1-001 | Resolved input lifecycle and readiness; known-prefix behavior retained | A-002/A-003; bounded acceptance, explicit terminal outcomes, nonce/frontier tests and independent-consumer recovery |
| LM-T1-002 | Retained verified fix | `ledger.rs::Segment::ALL`; fresh-window regression remains enabled |
| LM-T1-003 | Decision: retain current local Git workflow | No remote-policy change is justified by this audit; unsigned/branch-protection assertions are not verified current facts. Local checkpoints do not push |
| LM-T1-004 | Local compatibility expanded; deployment qualification unchanged | Legacy/v2 reads, mandatory v3 state, checksum-valid unknown-record refusal without truncation, rotation and restart tests. The older release artifact does not qualify these bytes or rollback readers |
| LM-T1-005 | Decision: retain durable dense ordering; catalog ownership simplified | CL-05 centralizes forward/reverse mutation. Boot/dynamic admission keep identity agreement and Names prefix. Stable namespaced keys add a migration without a current reorder requirement |
| LM-T1-006 | Stateful effects require durability before send | A-001 transition WAL and persisted placement IDs; async-barrier failure and restart regressions. Ordinary order-only callbacks retain optimistic send; completed-fsync latency is not newly measured |
| LM-T1-007 | Decision: retain current risk configuration and capital values | Existing kernel capital/margin/stop/freshness/loss/pending-exposure tests remain. No audit defect establishes values for new concentration/turnover/rate limits |
| LM-T1-101 | Retained static plug registration and checkpoint identity | `engine-strategies/src/lib.rs::PLUGS`, strategy contracts and boot identity tests; no duplicate manifest registry |
| LM-T1-102 | Decision: retain current action API with durable transition ownership | A-001/A-004 solve state/effect ordering directly. Portfolio contribution replacement awaits a concrete shared-symbol strategy |
| LM-T1-103 | Decision: retain exclusive symbol allocation | Current owner admission and attributed stops remain. Shared-symbol netting must combine partial-fill allocation, reservations, expiry, fees and portfolio stops; none is introduced piecemeal |
| LM-T1-104 | Decision: retain exclusive attribution ledger | Current strategy plus unknown/foreign quantities reconcile to venue authority. No virtual ledger with invented fee/quantity policy |
| LM-T1-105 | Decision: retain existing working-order ownership | Existing cancel/replace ambiguity, inventory, expiry, reductions and protective stops remain covered; central quoting is not needed for the retained allocation model |
| LM-T1-106 | Resolved dispatch loss/fairness and caller binding | A-001/A-004; timer replacement/64-key fairness retained. Trusted synchronous plug code cannot be forcibly preempted |
| LM-T1-201 | Decision: retain descriptive benchmark without inventing budgets | Named local workloads remain citable below. No production latency/capacity target is inferred from allow-all risk and localhost I/O |
| LM-T1-202 | Retained measured p99.9 propagation | Ledger/benchmark/heartbeat/WAL/replay/sampler preserve unavailable versus measured zero. Typed heartbeat matches legacy bytes; no deeper latency attribution claimed |
| LM-T1-203 | Decision: retain validated quantization semantics | Current floats and passive/downward rounding remain covered. Exact legal units require venue decimal/tick/lot contracts and a legacy migration; no numerical defect is reproduced |
| LM-T1-204 | Decision: retain accounting units and settlement semantics | Risk reservations/fees/fills retain current behavior. A universal micro-unit does not represent every asset exactly; no arbitrary fixed precision conversion |
| LM-T1-205 | Decision: retain current instrument rules | Current tick/step/minimum quantity/notional fields and adapter legality checks remain. Add multipliers/maxima/settlement capabilities only for a concrete adapter requirement |
| LM-T1-206 | Accepted payload and dispatch ownership resolved; limits explicit | A-001/A-002 bound per-turn work and accepted payloads while preserving prefix recovery. Callback allocation, historical identities, distinct timers and several subscription/deferred queues remain unbounded; no total-memory claim |
| LM-T1-207 | Implemented typed/shared adapter boundaries | CL-01/05/08/09; public-data crate removes private dependency. Selected typed envelopes, shared HMAC/ack memory and local failure/reconnect tests preserve venue-specific protocols; private adapters still share one crate |
| LM-T1-208 | Implemented transition ownership | CL-06/07/12 plus durable callback transitions; typed admission/journal tokens and narrow boot outputs. One deterministic core remains authoritative |
| LM-T1-209 | Implemented worker event/batch/lane ownership | CL-04/10/11; rejected unjournaled batch cannot advance memory, replay/retry agrees. Coverage revocation remains unchanged. Candidate cloning once per durable batch is not a performance improvement claim |
| LM-T1-301 | Decision: retain sanctioned deployment packaging | Artifact-only rollout/least-privilege changes require operational validation beyond this local scope. No deployment configuration, production permissions or source-token workflow is changed |
| LM-T1-302 | Dependency ownership simplified; no supply-chain incident claimed | CL-19 preserves exact external package IDs/features. Additional advisory/license/provenance infrastructure is not justified by a demonstrated audit fault |
| LM-T1-303 | Expanded targeted failure/replay campaign | Regression matrix covers floods, failed WAL/barriers, partial/late fills, no-op/rejected consumers, producer failures, restart, rotation and local reconnect. No blanket fuzz-completeness claim |
| LM-T1-304 | Local restart/reader coverage expanded; host drill excluded | Old-reader refusal, v3 required state and real stalled recovery deadline tested. No archive restore, funded restart or live rollback compatibility claim |
| LM-T1-305 | Decision: retain single account authority and reconciliation | An independent observer may report later; this audit supplies no need for another trading/reconstruction process. Existing unknown/foreign reconciliation remains enabled |
| LM-T1-306 | Verified complete reducer outputs and actual fixture consumers | 62 exact serialized outputs from 42 lifecycle tests plus rebuilt Rust/Python fixtures. Engineering equivalence is bounded by covered cases, not venue/account parity or profitability |
| LM-T1-307 | Retain Progressive Evidence Model | [Research governance](research/governance.md) remains authority for shaped/graded evidence and promotion. Engineering tests do not promote a strategy or authorize money |
| LM-T1-308 | Completed worker/venue/public ownership maps | CL-22 and `docs/engine.md`; executable fleet/config inventory remains authority. Dated implementation/check evidence lives in CHANGELOG |

### Architecture decisions

| Boundary | Chosen implementation | Alternative and reconsideration trigger |
| --- | --- | --- |
| State/effects | Ordered durable stateful transitions with retained cooperative dispatch | Raising an action cap leaves the same loss mechanism; hard isolation is warranted only for untrusted plugs |
| Inputs | Explicit terminal outcomes, backpressure and fresh producer frontier | Time-based deletion can claim work completed without execution; historical identity retirement needs a durable retirement contract |
| Execution | One core owner, typed admission/journal/completion phases and public/private capability separation | Generic all-venue actors conceal different authentication/reset rules; share only protocol-independent state |
| Portfolio/numbers | Exclusive symbols, durable dense ordering and current quantization/accounting | Shared symbols or exact units become worthwhile with a concrete strategy/adapter requirement and coherent allocation/fee/stop/legacy migration |
| Operations | Local checkpoints and complete engineering checks | Funded deployment, credentials, capital or live-state migration require explicit authorization |

### Change acceptance matrix

| Change family | Behavioral evidence required |
| --- | --- |
| Effect/drain fix | Reproduce the 256-opening/two-exit failure on the old behavior; preserve reductions, another sleeve's work, bounded dispatch turns and checkpoint/consume ordering on the fix |
| Signal lifecycle | Duplicate/conflict, 9→11→10 catch-up, future availability, full recovery slot, generation changes, consumer rejection, interrupted acknowledgement, restart/rotation and pending reductions |
| Boot/order state refactor | Exact discrete decisions, order/ledger identities, partial fills, duplicate/late news, stop ownership, ambiguous cancel/amend, barrier ordering and old WAL fixtures |
| Reducer refactor | Identical effect order and checkpoint bytes for identical inputs; exact membership/deadlines/actions and declared float tolerances with matching NaN positions. Run the fixture consumer rather than only hashing fixture files |
| Parser/shared-stream refactor | Known wire fixtures, malformed/missing fields, escaped strings, venue errors, auth/subscription/reset, deduplication and independent signing vectors; local mock lifecycle tests |
| Test/build cleanup | Same tests and process-sensitive behavior, compatible feature resolution, pinned compiler and a comparable measured build/test workload |
| Numeric/portfolio change | Treat changed decisions as a strategy/accounting change, document units/policy, preserve legacy readers or explicit refusal, and reconcile partial/unknown fills and settlement assets |

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
| Replay | The producer's immutable spool retains deferred payloads; WAL retains accepted inputs, cursors, routes and gap high-water marks. `segment_base_v3` requires gap and strategy-effect state; v2 retains its mandatory gap state; cursor/route/subscription inconsistencies fail boot. Existing callbacks are at-least-once across crashes |
| Compatibility | New readers accept legacy WAL. Older readers must refuse a new required gap/inbox record or versioned rotation, rather than ignore optional state. The current WAL decoder already rejects checksum-valid unknown records without truncating them |
| Remaining limits | Accepted payloads have consumed/rejected/retained outcomes and reused count/byte backpressure, with a prefix recovery slot. A no-op keeps one unfinished ordinary delivery; no timeout falsely marks it consumed. Growth after boot requires fresh producer participation and catch-up through its declared frontier. Historical source identities are retained; erased legacy history cannot be reconstructed. No live-state migration or automatic gap waiver |

## Invariants

- Must keep Rust as the live account/order/risk/durability authority and preserve one deterministic core while changing internal ownership.
- Must preserve WAL/replay compatibility, sequence/generation identity, venue reconciliation, exclusive stop/position ownership and reductions.
- Must distinguish missing/unavailable/foreign exposure from explicit flat state; source acceptance is not consumer completion.
- Must compare discrete decisions, ledger keys, effects and checkpoint bytes exactly for a refactor; continuous outputs require declared tolerances and matching NaN positions.
- Must not remove an importer from checkpoint existence alone, ignore correctness tests to shorten CI, or equate unchanged fixture files with unchanged behavior.
- Must preserve process-sensitive tests when consolidating binaries and coordinate the engine's clock with any Tokio clock control.
- Must treat performance estimates as hypotheses until measured on named comparable workloads; lint counts and source lines are not bug counts.
- Must keep the chosen exclusive-symbol, capital, accounting and trusted native-callback policies; new engineering ownership does not authorize funded deployment or live-state changes.
- Must keep dated change/validation history in `CHANGELOG.md`; each active finding must state its current disposition and evidence boundary.

## Operational Recipes

Run from the repository root. Compiler selection matters because the shell's default Rust can differ from the pin.

```bash
git status --short --branch
git rev-parse HEAD
audit_rust_bin="$(dirname "$(rustup which --toolchain 1.90.0 rustc)")"
export PATH="$audit_rust_bin:$PATH"
export RUSTC="$audit_rust_bin/rustc"
export RUSTDOC="$audit_rust_bin/rustdoc"
rustc -Vv
cargo fmt --manifest-path engine/Cargo.toml --all -- --check
cargo clippy --manifest-path engine/Cargo.toml --workspace --all-targets --locked -- -D warnings
cargo test --manifest-path engine/Cargo.toml --workspace --all-targets --locked
cargo test --manifest-path engine/Cargo.toml --workspace --doc --locked
cargo test --manifest-path engine/Cargo.toml --workspace --all-targets --release --locked
cargo test --manifest-path engine/Cargo.toml --workspace --doc --release --locked
scripts/dev.sh check
.venv/bin/python -m pytest -q tests/repo/test_docs_links.py
```

Reproduce the optional production lint inventory without changing the lint policy:

```bash
cargo clippy --manifest-path engine/Cargo.toml --workspace --lib --bins --locked \
  --message-format=json -- \
  -W clippy::or_fun_call -W clippy::redundant_clone \
  -W clippy::format_push_string -W clippy::large_types_passed_by_value \
  -W clippy::needless_pass_by_value -W clippy::str_to_string \
  -W clippy::unreadable_literal -W clippy::too_many_lines \
  -W clippy::cognitive_complexity -W clippy::large_futures
```

Reproduce A-001 in a disposable source export; this deliberately produces a failing test on the audited revision and never changes the working checkout. Use the compiler environment above.

```bash
audit_repro_dir=$(mktemp -d /tmp/liquidity-audit-exit.XXXXXX)
git archive f69a5fbf63afe11da78dde8bcf06a0ab6ba75046 | tar -x -C "$audit_repro_dir"
python3 - "$audit_repro_dir" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1]) / 'engine/engine-core/src/tests/order_path.rs'
text = path.read_text()
start = text.index('async fn a_flooded_wake_drops_entries_but_never_exits()')
end = text.index('\n}\n', start) + 3
body = text[start:end]
assert body.count('entries: 68,') == 1
path.write_text(text[:start] + body.replace('entries: 68,', 'entries: 256,') + text[end:])
PY
cargo test --manifest-path "$audit_repro_dir/engine/Cargo.toml" \
  --target-dir "$audit_repro_dir/target" -p engine-core --lib --locked \
  a_flooded_wake_drops_entries_but_never_exits -- --nocapture
```

The expected failure is `both exits reach the venue`, `left: 0`, `right: 2`. This establishes the synthetic drain defect, not a funded loss or its live frequency.

Use named local benchmark profiles with a fresh WAL per run. Existing benchmark venue/risk limitations still apply.

```bash
audit_bench_dir=$(mktemp -d /tmp/liquidity-audit-bench.XXXXXX)
cargo run --manifest-path engine/Cargo.toml --release --locked -p engine-core --bin engine -- bench \
  --events 4000 --rate 2000 --every 20 --symbols BTCUSDT \
  --wal "$audit_bench_dir/run.wal"
```
