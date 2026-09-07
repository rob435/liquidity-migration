# Tier-1 implementation and qualification

## Purpose

Define the execution ownership model, the current qualification checkpoint and the operational boundaries of the deployed engine.

## Spec Tables

| Contract | Authority |
| --- | --- |
| Open work and retained decisions | [tier1-audit-round-3.md](tier1-audit-round-3.md) supersedes the named Round-2 forks; Step 0 corrective deployment is verified |
| Baseline audit and qualification | Git revision `2422be0d`: original audit and regression evidence; [deployment and observed-fault qualification at `16689a98`](https://github.com/rob435/liquidity-migration/blob/16689a981c100632a1277567fb312e89a49c5309/docs/tier1-deployment-evidence.json); historical counts do not qualify current source |
| Round-2 audit input | Git revision `1b742d06`, retained by tag `codex/round2-audit-input`: original Claude audit, including disputed claims |
| Runtime design | [engine.md](engine.md); implemented code and tests take precedence |
| Deployment state | [STATE.md](../STATE.md); local qualification does not update the funded host |
| History | [CHANGELOG.md](../CHANGELOG.md); accepted baseline evidence remains in Git |
| Toolchain | Rust 1.90.0, with `PATH`, `RUSTC` and `RUSTDOC` pointing to its sysroot; repository `.venv` for Python |

| Owner | Implemented behavior | Main source |
| --- | --- | --- |
| Strategy callbacks | Production, bench, simulation and backtest use `CallbackExecution::Embedded`. Trusted reducers run on the loop thread inside `catch_unwind`; a panic faults the sleeve, discards its unfinished actions and cancels its own open orders. Changed checkpoints and ordered effects persist; unchanged state writes no callback WAL. Retained process records remain readable. | `engine/engine-core/src/ctx.rs`, `engine/engine-core/src/callback_recovery/`, `engine/engine-core/src/engine/strategy_callbacks.rs` |
| Dispatch durability | Checkpoint, intent, verdict, order and attempted-send record share one barrier before external order dispatch. Uncached leverage mutation first flushes its dependent checkpoint. Cancel and restart retain unsent reductions and ambiguous-send ownership. | `engine/engine-core/src/engine/order_dispatch.rs`, `engine/engine-core/src/engine/strategy_effects.rs` |
| WAL schema | Fifty current wire kinds; eight retained kinds are readable but cannot be appended. Current fee and checkpoint tags encode directly. Each JSON payload is parsed once; boot reuses the trusted scan. Segment v1 through v7 readers remain because host archives still require them. | `engine/engine-types/src/wal.rs`, `engine/engine-wal/src/lib.rs` |
| Venue build | Default features include Bybit. Binance, Hyperliquid, Lighter, MEXC and Variational remain optional, with per-feature conformance and CI builds. | `engine/engine-venue/Cargo.toml`, `.github/workflows/venue-features.yml` |
| Candidate handover | Demo must pass a 300-second soak before mainnet handover. Incumbent mainnet binaries and configuration stay pinned during demo evaluation. Runtime Python synchronization removes research packages; heartbeat sends systemd watchdog notifications. | `scripts/vps/deploy_remote.sh`, `scripts/runtime/check_fleet_liveness.py`, `engine/engine-core/src/heartbeat/notify.rs` |
| Effect scheduler | Ordered durable suffixes yield without losing reductions; caller identity, state, timers, placement IDs and complete outbox acceptance remain bound through replay. | `engine/engine-core/src/engine/strategy_effects.rs`, `engine/engine-core/src/engine/scheduling.rs` |
| Sleeve inventory | Durable sleeve keys own independent exact inventory, basis and stops, including opposing same-ticker holdings. Physical-net translation and stable sleeve-key allocation belong to the engine. LONG fill and retirement state follows executed sleeve membership; pending quantities affect planning separately. | `engine/engine-core/src/attribution.rs`, `engine/engine-core/src/portfolio_allocation.rs`, `engine/engine-core/src/portfolio_protection.rs` |
| General exits | Canonical quantities survive ordinary intents, full native exits, partial exits, market maximum chunks, pending targets and restart. A legacy full-holding projection resolves to the exact owned quantity; an explicit canonical partial exit remains partial. | `engine/engine-core/src/engine/intent_admission.rs`, `engine/engine-core/src/engine/portfolio_runtime.rs`, `engine/engine-strategies/src/native_common/mod.rs` |
| Emergency exits | Known terminal attempts release their attempt ID; ambiguous sends retain ownership. Exact net closure precedes durable internal offset settlement. Rejection, late fills, fees and each restart cut preserve the same obligation. | `engine/engine-core/src/portfolio_control.rs`, `engine/engine-core/src/engine/portfolio_runtime.rs` |
| Orders and lineage | Abandoned incomplete rotation files follow the same trust rule as normal boot; corruption after a committed restatement remains an error. One canonical order representation serves live and recent terminal state. The terminal cache holds at most 256 rows / 4 MiB; live reductions remain resident. One cancellable archive reader owns one pending private event, then restores the order durably before its late fill. New IDs use a durable monotone epoch and reversible numeric counter. | `engine/engine-core/src/engine/order_lineage.rs`, `engine/engine-core/src/engine/order_epoch.rs`, `engine/engine-core/src/inflight.rs`, `engine/engine-wal/src/order_lineage.rs` |
| Account and risk | Native lexical quantities, equity and balance remain canonical. Exact quantity, known price/range, exposure, margin, capital and loss arithmetic authorize decisions; display projections do not authorize them. | `engine/engine-types/src/orders.rs`, `engine/engine-types/src/risk.rs`, `engine/engine-risk/src/`, `engine/engine-venue/src/account_numbers.rs` |
| Stops | Exact native terms preserve closing side, remaining quantity, unique identities and full-size trigger coverage. Logical sleeve protection and native physical protection retain separate ownership. Same-direction sleeve stops update replayed repair intent; native repair omits StopSet only when that durable intent and an exact sleeve stop cover the requested level. Allocated fills record the exact opening stop before callbacks. A missing canonical stop can recover its binary64 same-base witness only for one owner and an explicit matching side; known/shared/opposed stops remain unchanged. | `engine/engine-venue/src/account_stops.rs`, `engine/engine-core/src/engine/stop_runtime.rs` |
| History acquisition | Native pages feed a stable disk merge ordered by `(venue_ts_ms, ordinal)`: 256 KiB runs, bounded rows, cancellation and progress. Iteration does not accumulate the complete response. Runtime applies 32 rows per turn; boot folds rows directly into canonical books and WAL. | `engine/engine-types/src/execution_history.rs`, `engine/engine-core/src/engine/history_recovery.rs`, `engine/engine-core/src/engine/boot_recovery.rs` |
| History completeness | A checkpoint advances only after authenticated exact physical agreement and resolution of orders, dispatch and reconciliation. Empty or unrelated net-neutral pages cannot erase known missing fills. Progress timeout releases stalled readers; healthy idle observations keep retention current. | `engine/engine-core/src/engine/account_recovery.rs`, `engine/engine-core/src/engine/history_recovery.rs` |
| Money and restart | Exact lot quantities, entry/exit values, cash and fees survive partial fills, reversals and rotation. Segment v7 requires explicit legacy retirement state and the cost-basis array and validates its owner/quantity linkage and cash equation. The precision marker makes predecessor readers refuse before ignoring canonical fields. | `engine/engine-core/src/execution.rs`, `engine/engine-core/src/execution/roundtrip.rs`, `engine/engine-types/src/trade.rs`, `engine/engine-wal/src/lib.rs` |
| Input lifecycle | Durable readiness, generation grants/seals, terminal consumption and retired floors retain missing prefixes. Append-only identities and retained native metadata support reordered registrations and exits during catalog outage. | `engine/engine-core/src/signal_state/`, `engine/engine-core/src/signals/readiness.rs`, `engine/engine-core/src/identities.rs`, `engine/engine-core/src/engine/symbol_admission.rs` |

| Decision | Selected policy | Alternative and reason |
| --- | --- | --- |
| Shared ownership | Exact virtual sleeves plus one physical account authority | Exclusive symbols cannot represent required opposing sleeves; a second account ledger adds competing ownership |
| Late terminal events | Reuse the canonical order from the retained WAL family | A separate tombstone ledger duplicates allocation/replay; eviction without recovery loses late fills |
| Market callbacks | Run trusted strategy code embedded; persist changed checkpoints and ordered effects | Process isolation is outside the owner-selected contract; panic survival remains required |
| Numeric boundary | Exact canonical values with validated legacy projections | Removing legacy fields breaks old WAL and adapters; converting canonical values through float loses valid quantity and price distinctions |
| Legacy accounting migration | Durable grid context normalizes eligible legacy units before canonical fill reducers; validated automatic FIFO and internal full-close allocations depending on those units are reconstructed with the same native totals, fees, stable policy and internal settlement price/time | Preserving faulty derived slices or permitting partial internal settlement creates microscopic opposing holdings; canonical-only allocations and explicit native amounts remain authoritative |
| Unknown valuation | Retain typed settlement/fee valuation debt in the rolling-loss window; preserve reductions | Assuming USDC or another asset equals USDT invents unavailable conversion evidence |

| Boundary | Explicit behavior |
| --- | --- |
| Legacy values | Eligible legacy quantity contributions resolve to a unique native grid point within 64 binary64 ULPs per input, durably before missed-fill allocation. Canonical suffixes and monetary values stay exact; old missing cost basis stays unpriced. Strategy scalar intent follows the outbound decimal policy. |
| Reporting | Money projections may underflow to zero or saturate at finite signed `f64::MAX`; canonical accounting and risk values remain exact. |
| Archive retention | Order lineage requires the retained WAL family from segment 1. External pruning must also preserve unresolved callback sources. Missing archive data is an unresolved recovery condition. |
| Storage and process limits | History resident memory is bounded by run/row contracts; disk usage scales with retained history. Embedded strategies share the engine process and have panic containment only. |
| Recovery availability | Unsupported history, manual fills without engine lineage, unknown asset conversion and erased producer tails stay explicit. |
| Retry timing | Durable obligations and ambiguous-send ownership survive restart; runtime backoff restarts and may allow one immediate retry. |
| Allocation | `EmergencyNetFifo` is versioned stable sleeve-key contributor order, not arrival-time FIFO. |
| Operational scope | No funded deploy, live account parity, capital change or strategy promotion follows from the local test results. |

| Qualification | Result and scope |
| --- | --- |
| Current deployed follow-up | Completed generation `905c10d3` deploys through [run 34085705580](https://github.com/rob435/liquidity-migration/actions/runs/34085705580) after 300 healthy demo seconds through 05:26:22 UTC. Identical runtime inputs leave mainnet on its `fc2ad99c` engine and PID. The 05:31:53 native/host read after the selected-pair drill verifies demo restoration, twelve exact full-size stops and zero restarts/OOMs |
| Local Round-3 checkpoint | The `6de33fa3` developer gate passes 1,962 Rust tests (zero failed, seven ignored) and 1,691 Python tests, with formatting and strict default workspace Clippy (`/tmp/r3-paired-source-estimator-push.log`). The paired-source follow-up also passes the independent 79 qualifier tests and three doc-link tests (`/tmp/r3-paired-source-root-focused.log`). Its normal hosted checks pass 1,964 Rust and 1,691 Python tests (`/tmp/r3-6de33fa3-hosted-{debug,python}.log`). These estimators change no engine runtime inputs. Release all-target tests pass 1,961, zero failed, seven ignored (`/tmp/r3-latency-followup-release-tests.log`). Six heavy fault seeds replay identically after two crashes each (`/tmp/r3-latency-followup-heavy-sim.log`). Both ignored copied-WAL fixtures pass separately in release (`/tmp/r3-latency-followup-boot-fixtures.log`). |
| Venue features | All six individual feature builds pass; venue/public/market-data tests pass 353 / 218 / 230 / 266 / 190 / 134 for Bybit / Binance / Hyperliquid / Lighter / MEXC / Variational respectively. The combined-feature suite passes 810 tests, two ignored; strict all-feature workspace Clippy passes. The default graph excludes k256 and sha3. `/tmp/r3-feature-*-{build,tests}.log`, `/tmp/r3-feature-qualification-all.log`, `/tmp/r3-all-feature-clippy-final.log`. |
| Hosted qualification | Fresh paired run [34093133061](https://github.com/rob435/liquidity-migration/actions/runs/34093133061), source `6de33fa3`, passes 1,962 release tests, zero failed, seven ignored, plus account workloads. Eight fixed A/B cells complete all 800 orders with one barrier each and zero failures. B medians 10.5 µs decision p99 / 1.25 ms submit p50 pass the relative gate against A 13.65 µs / 1.24 ms and also pass the absolute median limits. Three individual decision cells fail the absolute limit; all raw tails and prior failed jobs remain in [execution-performance.md](execution-performance.md). The downloaded candidate archive, hashes and embedded log verify. Normal hosted checks pass 1,964 Rust and 1,691 Python tests |
| Current-family rehearsal | Both complete retained families through the pinned 00:56 UTC current prefix pass the production exact-instrument embedded boot entry point, full-prefix reboot and real WAL rotation with exact ownership, accounting and lots intact. All 12 deliberately removed native stops are repaired, with zero opening/reduction orders. Bybit decodes its persisted catalog; transport, risk and collateral remain mocked. No future executions are supplied. `/tmp/r3-latency-followup-boot-fixtures.log`; originals and captured hashes under `/tmp/r3-current-wal-20260907T005622Z`. The same run also passes the older fixture on the final helpers. |
| Point latency acceptance | Current converter image passes both fixed-order narrow B cells: decision p50 7.751 / 8.631 µs, submit p50 4.718591 / 4.968447 ms. Its preceding wide decision p99 is 23.711 µs. All orders complete with one barrier and zero failures. The same control returns the old image below 10 µs; source attribution for the earlier 17 µs cells is unestablished. [Execution-performance.md](execution-performance.md) retains all passing/failing cells and frozen hashes; no stable bound is established |
| Open acceptance | R3-06 local correction enforces absolute and relative verdicts; 81 qualifier tests plus three doc-link tests and recorded-log controls pass. Fresh corrected-source hosted qualification remains pending. R3-07 offline converter is implemented and passes eight fixtures, 19 WAL unit tests, both CLI entry points and scoped strict Clippy; copied-family CRC/state and all 27 affected segment fills comparisons pass. R3-08 removal remains conditional on retained data. Current converter image passes both fixed-control narrow cells (decision 7.751 / 8.631 µs; submit 4.718591 / 4.968447 ms) and the preceding wide decision p99 23.711 µs; every earlier miss remains recorded. No host WAL conversion or reader deletion is complete |
| Selected-pair acceptance | The deployed helper activates reviewed `32858587` and restores `905c10d3` from 05:29:01 to 05:31:12 UTC (`/tmp/r3-demo-selected-pair-run.log`, `/tmp/r3-demo-selected-pair-journal.log`). Both legs verify loaded images and fresh account readiness. Current/previous markers and mainnet PIDs remain unchanged; all WAL files survive without shrinking. Default weekly behavior retains source equality. The 29 focused tests include stale-current refusal and restoration after predecessor failure. This qualifies the reviewed pair, not arbitrary future format changes |
| Accounting baseline `2422be0d` | 2,333 release tests, six ignored, six repeated heavy fault seeds and real copied-WAL accounting fixtures; full historical evidence is retained in that Git revision |
| Deployed corrective baseline | 2,426 debug and 2,426 release tests pass; six ignored in each profile. Strict Clippy, both doctest profiles, six repeated heavy fault seeds and copied-WAL boot/rotation/reboot pass. Source manifest covers 662 files. The required push gate passes. Corrected timer fixtures pass the Linux run: 2,430 tests, zero failed, six ignored; corrective handover succeeds on `bb4bc3d3`. [Evidence](tier1-round2-evidence.json) |
| Previous `af09aab5` release | 2,404 release tests pass with strict Clippy, both doctest profiles, six repeated heavy fault seeds, copied-WAL boot/rotation/reboot and three measured process workloads. The mandatory pre-push developer gate passes 2,403 regular debug tests and 1,608 Python tests on `af09aab5`. [Evidence](tier1-round2-evidence.json) |
| Regression controls | Actual assertion failures exist for callback timing/freshness, nonfinite fee preservation, canary exact terms, malformed private stream recovery and broken recovery CLI verbs; setup and compile failures add no count |
| Local execution mode | Production, bench, simulation and backtest use the same embedded callback implementation; both deployed engines have zero strategy children |
| Host evidence | [STATE.md](../STATE.md) owns the 08:24:41 UTC observation: completed generation `905c10d3`, unchanged demo engine and original mainnet `fc2ad99c` PID/image. All four processes are active with fresh heartbeats, ready workers and zero restarts/OOMs. Each realm has seven positions and seven exact full-size native stops, including WLD. Engine watchdogs are active; host Python contains only pip and websocket-client. No host WAL conversion is performed |

| Conditional retirement | Current evidence and required boundary |
| --- | --- |
| R3-07 segment readers | First-frame inventory of 28 quarantined files (7,531,896,053 bytes) finds 27 v5 bases and one v1 base: demo segments 26–39 and mainnet 25–38. `/tmp/r3-quarantine-head-inventory.jsonl` records bounded header reads, not full checksum validation. Captured current families contain v1 and v7 bases. Offline conversion writes a separate complete family, preserves records and sequence identity, restates v5 holdings with existing unpriced-lot semantics and relocates callback offsets. Missing lineage source segments remain a recovery error; copied-family CRC/state and fills replay comparisons pass; callback retrieval and archived-order lookup have focused-fixture evidence, with copied production boot and host conversion still pending. Keep needed readers and uncovered `StopSet` writes |
| Quarantine family inputs | Verified capture `/tmp/r3-quarantined-wals-20260907.tar.gz` retains all 28 suffix files plus the demo rollback tail and note. Local demo input uses current segments 1–24, segment 25 prefix `[0:175205844]` plus the saved 94,803,437-byte tail, then suffix 26–39. Mainnet uses current 1–23, segment 24 prefix `[0:268473255]` before the post-rollback Boot, then suffix 25–38. These exact append boundaries derive from retained frames and historical boot ordering; no original whole-family checksum is available. `/tmp/r3-quarantine-assembly.log` records the two reconstructed file hashes |
| Copied conversion checks | Demo 39 segments / 2,253,351 records / 14 upgraded bases; mainnet 38 / 851,734 / 13. Independent CRC and captured/spliced hash checks pass for every frame/file; all other payloads are byte-identical, and changed bases preserve state and exact owned quantities with unpriced lots. All 27 affected segments produce identical original/converted fills reports after path normalization. `/tmp/r3-quarantine-{demo,mainnet}-{convert,independent,fills-replay-comparison}.log`. Callback retrieval and archived-order lookup are covered by focused fixtures. No host family is converted; local copied data is not live-account authority |
| R3-08 legacy order terms | The demo segment captured at 04:44:33 UTC has 53 retained orders, including three zero-filled cancelled requests without exact terms: `eng-1788685989000-{8,9,10}`. `retained_since_ms=1788702340615`; seven-day plus two-minute retention requires both wall time and complete history strictly beyond 2026-09-13 13:47:40.615 UTC before natural expiry. The pinned 00:56 UTC mainnet base has six requests, all exact. Expiry does not rewrite retained archives. Deployment still calls legacy source retirement and strategy-state import; current native protection does not retire those contracts |

## Invariants

- Must preserve shared and opposing sleeve ownership, WAL compatibility, reconciliation, protective stops and reductions.
- Must publish neither a partial callback proposal nor an unjournaled order effect.
- Must preserve exact canonical quantity and known monetary terms through risk, dispatch, fill allocation and restart.
- Must retain unresolved orders, history and source prefixes until evidence resolves them; must never infer completeness from an empty page alone.
- Must count a bug regression only when an assertion fails with the actual prior implementation and passes with the fix; source mutations and compile errors are not proof.
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
cargo build --release --locked -p engine-tools --bins
./target/release/engine-tools sim --seed 1 --seconds 300 --symbols 2 --crashes 0 --faults none --twice
./target/release/engine-tools sim --seed 1 --seeds 6 --seconds 300 --symbols 2 --faults light --twice
./target/release/engine-tools sim --seed 7 --seconds 300 --symbols 2 --crashes 2 --faults heavy --twice
./target/release/engine-tools sim --seed 1 --seeds 40 --seconds 300 --symbols 2 --crashes 2 --faults heavy --twice --keep --out /tmp/tier1-sim-heavy
```

```sh
# Linux, with the same pinned compiler.
cd engine
cargo test --locked --release -p engine-core callback_recovery -- --nocapture
cargo test --locked --release -p signal-worker full_population_outage_resource_envelope_is_bounded -- --ignored --nocapture
```
