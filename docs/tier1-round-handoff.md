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
| Current local follow-up | Borrowed risk prices, reused order-term projections, fresh subscription membership bitsets, exact storage bit bounds and a venue-actor yield pass final local qualification. The yield follows durable authorization and mutation registration. These changes are not yet deployed. Normal-release cells live in [execution-performance.md](execution-performance.md): all five final cells pass one barrier plus 1 ms; narrow submit meets 5 ms in two of three unchanged repeats. |
| Local Round-3 checkpoint | The developer gate passes 1,962 Rust tests (zero failed, seven ignored) and 1,646 Python tests, with formatting and strict default workspace Clippy (`/tmp/r3-latency-followup-developer-check.log`). Release all-target tests pass 1,961, zero failed, seven ignored (`/tmp/r3-latency-followup-release-tests.log`). Six heavy fault seeds replay identically after two crashes each (`/tmp/r3-latency-followup-heavy-sim.log`). Both ignored copied-WAL fixtures pass separately in release (`/tmp/r3-latency-followup-boot-fixtures.log`). |
| Venue features | All six individual feature builds pass; venue/public/market-data tests pass 353 / 218 / 230 / 266 / 190 / 134 for Bybit / Binance / Hyperliquid / Lighter / MEXC / Variational respectively. The combined-feature suite passes 810 tests, two ignored; strict all-feature workspace Clippy passes. The default graph excludes k256 and sha3. `/tmp/r3-feature-*-{build,tests}.log`, `/tmp/r3-feature-qualification-all.log`, `/tmp/r3-all-feature-clippy-final.log`. |
| Hosted qualification | [Run 34074530152](https://github.com/rob435/liquidity-migration/actions/runs/34074530152) passes 1,959 release tests, zero failed, seven ignored, account-state workloads and the latency check. Its separate native-target build measures decision p99 6.0 µs and submit p50 1.09 ms; these measured references set the Linux budget. The unchanged-runtime calibrated repeat [34076340582](https://github.com/rob435/liquidity-migration/actions/runs/34076340582) fails at decision p99 16.6 µs versus 9.0 µs; submit p50 1.51 ms passes. Calibration acceptance remains open; the limits are unchanged. [Execution measurements](execution-performance.md) identifies artifact and build scope. |
| Current-family rehearsal | Both complete retained families through the pinned 00:56 UTC current prefix pass the production exact-instrument embedded boot entry point, full-prefix reboot and real WAL rotation with exact ownership, accounting and lots intact. All 12 deliberately removed native stops are repaired, with zero opening/reduction orders. Bybit decodes its persisted catalog; transport, risk and collateral remain mocked. No future executions are supplied. `/tmp/r3-latency-followup-boot-fixtures.log`; originals and captured hashes under `/tmp/r3-current-wal-20260907T005622Z`. The same run also passes the older fixture on the final helpers. |
| Open acceptance | Submit latency; hosted latency calibration; archived segment-version conversion; legacy retirement. The live-network probe passes in a separate process; all async test attributes use paused clocks. The plan retains every unfinished row. |
| Accounting baseline `2422be0d` | 2,333 release tests, six ignored, six repeated heavy fault seeds and real copied-WAL accounting fixtures; full historical evidence is retained in that Git revision |
| Deployed corrective baseline | 2,426 debug and 2,426 release tests pass; six ignored in each profile. Strict Clippy, both doctest profiles, six repeated heavy fault seeds and copied-WAL boot/rotation/reboot pass. Source manifest covers 662 files. The required push gate passes. Corrected timer fixtures pass the Linux run: 2,430 tests, zero failed, six ignored; corrective handover succeeds on `bb4bc3d3`. [Evidence](tier1-round2-evidence.json) |
| Previous `af09aab5` release | 2,404 release tests pass with strict Clippy, both doctest profiles, six repeated heavy fault seeds, copied-WAL boot/rotation/reboot and three measured process workloads. The mandatory pre-push developer gate passes 2,403 regular debug tests and 1,608 Python tests on `af09aab5`. [Evidence](tier1-round2-evidence.json) |
| Regression controls | Actual assertion failures exist for callback timing/freshness, nonfinite fee preservation, canary exact terms, malformed private stream recovery and broken recovery CLI verbs; setup and compile failures add no count |
| Local execution mode | Production, bench, simulation and backtest use the same embedded callback implementation; both deployed engines have zero strategy children |
| Host evidence | [STATE.md](../STATE.md) owns dated observations. Workflow `34076341887` completes generation `32858587` after 300 healthy demo seconds; identical runtime inputs leave mainnet on its verified `a4189a48` image and PID. The sanctioned demo rollback drill passes predecessor/current readiness and loaded-image checks at 02:47:01 / 02:48:03 UTC without rewinding durable state. At 02:49:28 UTC both embedded engines and workers are healthy, six positions per realm have exact full-size native stops, and mainnet PIDs remain unchanged. Engine watchdogs are active; host Python contains only pip and websocket-client. |

| Conditional retirement | Current evidence and required boundary |
| --- | --- |
| R3-07 segment readers | Quarantined families remain under `/var/lib/liquidity-migration-wal-quarantine`; v5 conversion or retirement is uncompleted. Keep every reader needed by retained state |
| R3-08 legacy order terms | The pinned 00:56 UTC demo base retains three zero-filled cancelled probe requests without exact terms: `eng-1788685989000-{8,9,10}`. `retained_since_ms=1788702340615`; the seven-day plus two-minute retention requires both wall time and complete history strictly beyond 2026-09-13 13:47:40.615 UTC before natural expiry. Mainnet's six retained requests have exact terms. Rotation and archive/rollback dependencies still govern removal |

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
