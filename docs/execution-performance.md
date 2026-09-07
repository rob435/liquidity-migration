# Execution measurements

## Purpose

State measured execution workloads, timing boundaries and resource limits for the deployed baseline and the Round-3 candidate.

## Spec Tables

| Decision | Current evidence |
| --- | --- |
| Deployed baseline | `bb4bc3d3`; isolated strategy callbacks; dated host evidence lives in [STATE.md](../STATE.md) |
| Local candidate | One embedded callback mode, Bybit default features and one dispatch barrier per order; broader qualification and deployment remain pending |
| Target gaps | Latest uncontended cells pass narrow decision p50 and wide decision p99; submit p50 remains above target |
| Latest source boundary | Callback-buffer reuse, direct binary64 normalization, immutable envelope policy, reduced products, aggregate margin division, grouped pending risk and per-symbol pending quantity totals, covered native-stop writes, fresh priced-quantity grouping and batched exact sums; normal release builds carry no temporary profiling |

### Retained Round-2 baseline

| Field | Scope |
| --- | --- |
| Evidence boundary | 2026-09-06; Rust 1.90.0 release on Apple M4, 10 CPUs, 16 GiB RAM, macOS 15.7.2 |
| Execution | Real engine loop, one registered child strategy and `engine-risk`; synthetic market and venue |
| Source identity | Local binaries contain `f6c71460bbfa2b3fbe5a3a0d35e96a06776dcb50` code; all 662 qualified source files match that commit in [qualification evidence](tier1-round2-evidence.json) |
| Build label | The precommit binaries report `af09aab53fc13cf53f66c393931c0ecccd765598-dirty`; the source manifest and executable hashes bind these measurements to the source above |
| Input | Total rate across symbols; every twentieth global source sequence is an order opportunity |
| Outstanding orders | Synthetic accepts do not fill; growing outstanding-order snapshots increase CPU and WAL volume |
| Clock | Current-process source-to-decision and source-to-submit measurements include child execution and durability; prior-process source stamps do not enter new samples |
| Quantiles | Individual HDR samples; fewer than 1,000 samples place p99.9 at the observed maximum |
| Barriers | WAL request-to-observed-confirmation, including the benchmark observer thread/channel; not pure filesystem sync time |
| Resources | Parent, child and time wrapper sampled with a 50 ms sleep between process-tree scans; scan overhead, short peaks and final CPU increments limit these measurements |
| Disk | Complete final WAL with rotation disabled, including boot and report; not a steady-state byte rate or storage quota |
| Interference | No compiler processes observed during these cells; other desktop work is not excluded |
| Private evidence | `/tmp/tier1-round2-integration/measurements-callback-inventory/summary.json`; raw logs, WALs and hashes are retained beside this receipt |

| Executable | SHA256 |
| --- | --- |
| `engine` | `18ffa9406ed095833104b8c81618c0e6ae8defddcbc54891d2c4046eb22df311` |
| `engine-tools` | `e7718724a3d5c2dbe562f2759a6bd7940aa01d556dc898b9af998ff2756bdcbb` |

Latency cells are milliseconds, `p50 / p99 / p99.9`; MiB means 1,048,576 bytes.

| Symbols / quotes / total rate / venue delay | Submits / opportunities | Source → decision | Source → submit result | Sampled tree / child MiB | Final WAL MiB |
| --- | --- | --- | --- | --- | --- |
| 1 / 6,000 / 100 Hz / 0 ms | 299 / 300 | 0.594 / 1.166 / 2.490 | 13.902 / 18.973 / 21.725 | 24.94 / 6.83 | 16.21 |
| 270 / 12,000 / 200 Hz / 0 ms | 587 / 600 | 3.070 / 174.326 / 218.366 | 20.513 / 195.035 / 238.158 | 65.12 / 29.73 | 299.03 |
| 270 / 12,000 / 200 Hz / 20 ms | 584 / 600 | 3.246 / 182.714 / 237.634 | 42.467 / 222.560 / 282.591 | 65.92 / 30.45 | 297.22 |

| Cell | Callback / queue / attempt barrier median ms | Barrier confirmations / failures | Sampled CPU seconds | Outer elapsed seconds |
| --- | --- | --- | --- | --- |
| single | 3.713 / 3.183 / 3.612 | 1803 / 0 | 4.76 | 60.294 |
| wide | 3.960 / 3.538 / 3.606 | 3531 / 0 | 44.82 | 60.058 |
| wide-delayed | 3.860 / 3.589 / 3.593 | 3513 / 0 | 45.71 | 60.092 |

| Outcome | Observation |
| --- | --- |
| Admission and completion | Each durable intent reaches a completed submit; no risk refusals, callback faults or barrier failures occur in these cells |
| Missing source opportunities | 1 / 13 / 16 opportunities have no durable intent; WAL records do not separate coalescing from shutdown for this residual |
| Readback | All three final WALs validate through the rebuilt `engine latency` command |
| Reconstruction | Full WALs retain counts and final quantile summaries; individual HDR samples and process-tree snapshots are not retained |
| Baseline durability | These cells measure separate callback, queued-dispatch and attempted-send barriers; the Round-3 candidate combines their order prefix before the external send |
| Scope limit | One active child over 270 symbols is not 270 workers; a 60-second no-fill run does not establish a universal resource ceiling, production-day accounting or a latency SLO |

### Round-3 measurements

| Boundary | Contract |
| --- | --- |
| Baseline | `bb4bc3d3`, same runtime as `f6c71460`; Rust 1.90.0, Apple M4, macOS, release; no other compiler during either run |
| Recipe | Round-3 unloaded: 2,000 events / 100 Hz; wide: 12,000 events / 200 Hz, `S001USDT` through `S270USDT`; one order opportunity per 20 events |
| Barrier comparison | R3-03 uses the engine histogram row "dispatch barrier observed"; the separate WAL observer table includes an additional thread/channel and is not the comparison baseline |
| Scope | Shared before cells for R3-01, R3-02, R3-04, R3-08, R3-11, R3-12 and the early R3-09 gate; combined later cells cannot attribute a latency change to an individual item |
| Raw output | `/tmp/r3-before-unloaded.log`, `/tmp/r3-before-wide-270.log`; complete WALs share each basename |
| Invalid setup | An initial wide invocation had an invalid symbol-list expression and refused boot; it contributes no measurement |

| Cell | Submits / opportunities | Decision p50 / p99 | Market to submit p50 / p99 | Dispatch barrier observations | WAL bytes |
| --- | --- | --- | --- | --- | --- |
| Before, unloaded | 98 / 100 | 702 µs / 1.41 ms | 16.15 ms / 20.73 ms | 196 | 2,268,634 |
| Before, 270 symbols | 594 / 600 | 2.96 ms / 101.25 ms | 19.76 ms / 122.55 ms | 1,188 | 312,858,177 |
| Embedded + parallel step 2, unloaded | 100 / 100 | 19.5 µs / 35.2 µs | 9.31 ms / 11.57 ms | 200 | 189,902 |
| Embedded + parallel step 2, 270 symbols | 600 / 600 | 18.5 µs / 28.3 µs | 13.68 ms / 21.28 ms | 1,200 | 1,178,319 |
| One barrier, unloaded; CPU interference | 100 / 100 | 3.1 µs / 18.4 µs | 4.72 ms / 6.68 ms | 100 | 189,584 |
| One barrier, 270 symbols; CPU interference | 600 / 600 | 1.3 µs / 9.7 µs | 6.02 ms / 8.72 ms | 600 | 1,177,990 |
| One barrier, unloaded; uncontended repeat | 100 / 100 | 21.8 µs / 27.1 µs | 5.72 ms / 7.50 ms | 100 | 189,585 |
| One barrier, 270 symbols; uncontended repeat | 600 / 600 | 14.8 µs / 42.6 µs | 9.16 ms / 14.38 ms | 600 | 1,178,013 |
| Record freeze + conformance/test-clock block, unloaded | 100 / 100 | 16.2 µs / 46.6 µs | 6.59 ms / 9.63 ms | 100 | 189,742 |
| Record freeze + conformance/test-clock block, 270 symbols | 600 / 600 | 14.8 µs / 39.2 µs | 10.72 ms / 15.10 ms | 600 | 1,176,721 |
| Binary64 conversion + immutable risk policy, unloaded | 100 / 100 | 17.6 µs / 29.5 µs | 5.51 ms / 7.07 ms | 100 | 189,741 |
| Binary64 conversion + immutable risk policy, 270 symbols | 600 / 600 | 7.8 µs / 20.4 µs | 9.54 ms / 13.57 ms | 600 | 1,176,704 |
| Reused callback action buffer, unloaded | 100 / 100 | 6.0 µs / 16.9 µs | 6.12 ms / 7.73 ms | 100 | 189,743 |
| Reused callback action buffer, 270 symbols | 600 / 600 | 6.3 µs / 21.5 µs | 9.28 ms / 14.15 ms | 600 | 1,176,699 |
| Reduced exact products + aggregate margin division, unloaded | 100 / 100 | 6.8 µs / 14.2 µs | 5.57 ms / 6.93 ms | 100 | 189,739 |
| Reduced exact products + aggregate margin division, 270 symbols | 600 / 600 | 5.3 µs / 12.1 µs | 8.53 ms / 12.49 ms | 600 | 1,176,698 |
| Grouped pending risk, unloaded | 100 / 100 | 7.0 µs / 35.8 µs | 5.44 ms / 6.55 ms | 100 | 189,748 |
| Grouped pending risk, 270 symbols | 600 / 600 | 5.4 µs / 17.6 µs | 7.42 ms / 10.12 ms | 600 | 1,176,698 |
| Grouped pending risk, unloaded repeat | 100 / 100 | 6.4 µs / 20.3 µs | 5.58 ms / 6.64 ms | 100 | 189,736 |
| Pending quantity index, unloaded | 100 / 100 | 5.5 µs / 31.3 µs | 5.40 ms / 6.33 ms | 100 | 189,739 |
| Pending quantity index, 270 symbols | 600 / 600 | 5.8 µs / 20.1 µs | 7.40 ms / 10.44 ms | 600 | 1,176,700 |
| Covered native stop writes, unloaded | 100 / 100 | 6.8 µs / 11.7 µs | 5.33 ms / 6.19 ms | 100 | 189,739 |
| Covered native stop writes, 270 symbols | 600 / 600 | 5.6 µs / 17.4 µs | 7.13 ms / 10.39 ms | 600 | 1,176,693 |
| Grouped priced quantities, unloaded | 100 / 100 | 4.6 µs / 10.0 µs | 5.26 ms / 6.69 ms | 100 | 189,739 |
| Grouped priced quantities, 270 symbols | 600 / 600 | 4.5 µs / 10.6 µs | 5.87 ms / 7.27 ms | 600 | 1,176,693 |
| Batched rational sums, unloaded | 100 / 100 | 4.8 µs / 9.3 µs | 5.05 ms / 6.70 ms | 100 | 189,737 |
| Batched rational sums, 270 symbols | 600 / 600 | 4.5 µs / 10.9 µs | 5.25 ms / 6.35 ms | 600 | 1,176,693 |
| Borrowed positive quantities, unloaded | 100 / 100 | 4.7 µs / 10.4 µs | 5.03 ms / 6.25 ms | 100 | 189,742 |
| Borrowed positive quantities, 270 symbols | 600 / 600 | 4.5 µs / 11.8 µs | 5.25 ms / 6.42 ms | 600 | 1,176,694 |
| Borrowed stop fractions, unloaded | 100 / 100 | 4.9 µs / 12.0 µs | 5.05 ms / 6.93 ms | 100 | 189,739 |
| Borrowed stop fractions, 270 symbols | 600 / 600 | 4.9 µs / 11.9 µs | 5.41 ms / 6.57 ms | 600 | 1,176,680 |

| Candidate boundary | Observation |
| --- | --- |
| Embedded source | Working tree based on `bb4bc3d3`, R3-01/02 plus parallel R3-04/08/11/12 and early R3-09; before R3-03 |
| Raw candidate output | `/tmp/r3-embedded-unloaded.log`, `/tmp/r3-embedded-wide-270.log`; complete WALs share each basename |
| Interference | No compiler during either candidate run; the same box has 44 GiB free after Cargo removes rebuildable debug artifacts |
| Acceptance | Wide decision p99 passes 50 µs; unloaded decision p50 misses 10 µs; both cells still use two dispatch barriers per order; no barrier failures |
| Footer limitation | These binaries retain an obsolete IPC sentence in the report footer; the executed boot and callback path is embedded |
| R3-03 source boundary | One barrier covers queued order and attempt; checkpointed callbacks join this prefix. An uncached leverage mutation retains a preceding checkpoint barrier. Parallel virtual-time test work changes venue deadline clocks, so these cells do not isolate every compiler/layout effect |
| R3-03 raw output | `/tmp/r3-one-barrier-unloaded.log`, `/tmp/r3-one-barrier-wide-270.log`; complete WALs share each basename |
| R3-03 measurement limit | All 700 opportunities complete with one dispatch barrier per order and zero barrier failures. A stale core test consumes ~55% of one CPU during these cells; latency qualification requires a repeat after that process stops |
| R3-03 uncontended repeat | `/tmp/r3-one-barrier-clean-{unloaded,wide-270}.log`; no test or compiler process runs during either cell. Both decision p50 values and both submit p50 values miss their targets; wide decision p99 passes. All 700 opportunities complete with one dispatch barrier per order and zero barrier failures |
| Record-freeze block | R3-05/06/07/10 share the uncontended one-barrier cells as their before boundary; concurrent implementation prevents individual latency attribution. Fifty current write kinds, eight retained read kinds, current tags, debug-only readback and single-scan boot are included. StopSet and retained segment-version removals remain open. |
| Record-freeze output | `/tmp/r3-record-freeze-{unloaded,wide-270}.log` and matching WALs; release build `/tmp/r3-record-freeze-release-build.log`. No test or compiler runs during either cell. All 700 opportunities complete, with zero barrier failures. |
| Record-freeze latency | Narrow decision p50 and submit p50 remain above target; wide decision p99 remains below 50 µs. Barrier medians are 4.28 / 4.20 ms, and submit p50 exceeds one barrier plus 1 ms. |
| Printed footer | These binaries' footer still describes IPC and two dispatch barriers; source and the counted barrier rows prove the embedded one-barrier path. The text correction has no timing meaning. |
| R3-13 first change | Normalize binary64 powers of two before constructing the rational; parse immutable envelope policies at construction. Risk admission and fresh-state pre-send rechecks remain in place. |
| R3-13 output | `/tmp/r3-exact-risk-{unloaded,wide-270}.log` and matching WALs; normal release build `/tmp/r3-exact-risk-release-build.log`. No compiler or test process runs during either cell. All 700 opportunities complete with one barrier per order and zero barrier failures. |
| R3-13 acceptance | Wide decision p99 passes; narrow decision p50 and submit p50 still miss. Dispatch barrier medians are 4.09 / 4.03 ms. Both submit medians exceed one barrier plus 1 ms. |
| Exact parity | 24,576 binary64 patterns compare against `BigRational::from_float`, including every exponent, signed zeros, subnormals, nonfinite values and canonical serialized bytes. The parity test passes before and after; 157 risk tests pass after. This is a refactor, not a new bug claim. |
| R3-14 output | `/tmp/r3-action-buffer-{unloaded,wide-270}.log` and matching WALs; normal release build `/tmp/r3-action-buffer-release-build.log`. No compiler or test process runs during either cell. All 700 opportunities complete with one barrier per order and zero barrier failures. |
| R3-14 acceptance | Narrow decision p50 and wide decision p99 pass. Submit p50 remains above target and above one barrier plus 1 ms; barrier medians are 4.12 / 4.08 ms. All 808 core tests pass, including action discard on panic and checkpoint deduplication; `/tmp/r3-action-buffer-core.log`. |
| Exact product/margin output | `/tmp/r3-exact-products-{unloaded,wide-270}.log` and matching WALs; normal release build `/tmp/r3-exact-products-release-build.log`. No compiler or test process runs during either cell. All 700 opportunities complete, with one barrier per order and zero failures. |
| Exact product/margin parity | Reduced products compare exact values and canonical bytes against the original rational implementation for fixed extremes and 4,096 seeded input pairs. Margin compares the original rowwise calculation across 1,024 lifecycle steps, three query frontiers and three leverage values, including refusal order. These refactor comparisons pass before and after; 158 risk tests pass. |
| Exact product/margin acceptance | Both decision targets pass. Narrow submit p50 misses 5 ms; dispatch barrier medians are 4.01 / 4.03 ms. Submit remains above one barrier plus 1 ms. |
| Grouped pending risk | Each assessment values all pending rows from current inputs, then sums notionals by effective stop fraction before calculating loss. No aggregate survives the assessment. Original row validation and refusal ordering are unchanged. |
| Grouped output | `/tmp/r3-grouped-risk-{unloaded,wide-270}.log` and matching WALs; normal release build `/tmp/r3-grouped-risk-release-build.log`. No compiler or test process runs during either cell. All 700 opportunities complete with one barrier each and zero failures. |
| Grouped qualification | 159 risk tests and strict crate Clippy pass. A 256-step comparison preserves original rowwise exact totals and canonical bytes across additions, removals, quantity changes and fractions at or immediately above the disaster floor. The combined source has full developer qualification below. |
| Grouped acceptance | Both decision targets pass, but narrow submit p50 still misses 5 ms; barrier medians are 4.02 / 4.03 ms. The Mac budget rejects this narrow p99: 35,800 ns exceeds its 21,300 ns limit (`/tmp/r3-grouped-risk-budget-check.log`). The budget remains unchanged. |
| Grouped repeat | `/tmp/r3-grouped-risk-unloaded-repeat.log` and matching WAL; no compiler/test process runs concurrently. All 100 opportunities complete with one barrier each and zero failures. Decision p99 is 20,300 ns and the unchanged Mac budget passes; submit p50 remains above 5 ms. Both runs remain recorded, so the first tail-budget failure is not erased. |
| Quantity index | Per-symbol totals retain positive and negative pending quantities and unknown-row counts. Registration, replacement, temporary exclusion, fill and retirement update them; settled balances and prices remain query inputs. |
| Quantity index output | `/tmp/r3-quantity-index-{unloaded,wide-270}.log` and matching WALs; normal release build `/tmp/r3-quantity-index-release-build.log`. No compiler or test process runs during either cell. All 700 opportunities complete with one barrier each and zero failures. |
| Quantity index parity | The original rowwise interval calculation matches exact values, error text and serialized bytes over 4,096 reservation lifecycle steps, changing settled quantities and reconstructed books. This refactor comparison passes before and after; all 160 risk tests pass (`/tmp/r3-quantity-index-{before,after}.log`). |
| Quantity index acceptance | Both decision targets pass; narrow submit still exceeds 5 ms and one barrier plus 1 ms. Barrier medians are 4.05 / 4.01 ms. Narrow p99 is 31,300 ns, above the unchanged Mac budget's 21,300 ns limit. |
| Covered stop output | `/tmp/r3-covered-stop-{unloaded,wide-270}.log` and matching WALs; normal release build `/tmp/r3-covered-stop-release-build.log`. No compiler or test process runs during either cell. All 700 opportunities complete, with one barrier each and zero failures. The quantity-index cells are the before boundary. |
| Covered stop scope | The synthetic bench requests no standalone stop moves, so these cells qualify the combined source without measuring the record savings. The stop fixture proves that a matching exact sleeve stop suppresses StopSet; replay and rotation retain 95.1 instead of the stale 90.0 repair level. Both assertions fail before the change (`/tmp/r3-covered-stop-before.log`); all 809 core tests pass afterward, one ignored (`/tmp/r3-covered-stop-core.log`). |
| Covered stop acceptance | Both decision targets and the unchanged Mac budget pass (`/tmp/r3-covered-stop-budget-check.log`). Narrow submit p50 still misses 5 ms; barrier medians are 4.02 / 3.98 ms. Submit remains above one barrier plus 1 ms. |
| Current developer qualification | `scripts/dev.sh check` passes 1,959 Rust tests (zero failed, seven ignored), 1,646 Python tests, formatting and strict workspace Clippy; `/tmp/r3-developer-check-8.log`. All six individual feature builds/suites and the combined 810-test venue/public/market-data suite pass; strict all-feature Clippy passes. The release suite passes 1,957 tests, zero failed, seven ignored (`/tmp/r3-release-qualification.log`). The complete retained-family rehearsal passes both realms through the production exact-instrument embedded boot entry point, with real WAL readers/writer/rotation and mocked transport/risk/collateral, preserving exact ownership, accounting, lots and all 12 stops (`/tmp/r3-exact-boot-fixtures.log`). The updated older-fixture regression also passes. The final developer gate passes with the latest test-only support changes. |
| Priced quantity grouping | Fresh assessments group quantities by effective price and stop fraction before notional multiplication; margin groups quantities by effective price. Validation and price-read ordering remain unchanged. `/tmp/r3-priced-quantities-{unloaded,wide-270}.log` and matching WALs; build `/tmp/r3-priced-quantities-release-build.log`. The preceding covered-stop cells are the before boundary. |
| Priced quantity parity | The original rowwise valuation matches exact totals, errors, canonical bytes and price-read order across 512 lifecycle steps and three price-query cases. The existing 1,024-step margin comparison includes repeated prices across symbols. Both comparisons pass before and after; all 161 risk tests pass. |
| Batched rational sums | ExactSum combines equal-denominator runs and reduces before a denominator change and at finish. Stored Exact values remain canonical. The original rational sum matches values and canonical bytes at 4,096 prefixes, including cancellation and extreme scales. All 226 type/risk checks pass. `/tmp/r3-batched-ratios-{unloaded,wide-270}.log` and matching WALs; build `/tmp/r3-batched-ratios-release-build.log`. |
| Borrowed positive quantities | Grouping borrows nonnegative quantities; negative quantities retain their exact magnitude. All 161 risk tests pass. `/tmp/r3-borrowed-quantities-{unloaded,wide-270}.log` and matching WALs; build `/tmp/r3-borrowed-quantities-release-build.log`. |
| Measurement scope | These six cells run without a concurrent test or compiler. All 2,100 opportunities complete with one barrier each and zero failures. Both decision targets pass throughout. Narrow submit medians remain above 5 ms; barrier medians are 4.20 / 4.14 ms, 4.20 / 4.15 ms and 4.19 / 4.15 ms respectively. |
| Borrowed stop fractions | Assessment keys borrow the immutable stop fraction and clone only distinct output groups. All 161 risk tests and strict type/risk Clippy pass. `/tmp/r3-borrowed-fractions-{unloaded,wide-270}.log` and matching WALs; build `/tmp/r3-borrowed-fractions-release-build.log`. All 700 opportunities complete with one barrier each and zero failures; no compiler/test runs concurrently. Barrier medians are 4.19 / 4.17 ms. Both decision targets and the unchanged Mac budget pass, but narrow submit still exceeds 5 ms and wide submit exceeds one barrier plus 1 ms. |

| Initial embedded stage profile | Median µs | First / last ten orders, median µs |
| --- | --- | --- |
| Market apply | 0.292 | 0.271 / 0.459 |
| Virtual stop check | 1.917 | 1.729 / 2.312 |
| Native stop supervision | 3.042 | 2.667 / 3.979 |
| Exact reference-price observation | 4.084 | 3.667 / 4.438 |
| Embedded callback and action capture | 9.792 | 10.209 / 10.104 |
| Risk admission | 929.792 | 188.729 / 1,770.000 |
| Quantization and physical protection plan | 187.250 | 127.334 / 259.959 |
| Pre-send risk/protection recheck | 823.229 | 238.167 / 1,419.749 |

| Profile boundary | Contract |
| --- | --- |
| Recipe | Same unloaded 2,000-event / 100 Hz workload; 100 order samples; `/tmp/r3-stage-profile-unloaded.log` |
| Instrumentation | Temporary stage timestamps and stderr output; the instrumented binary is diagnostic and cannot qualify latency |
| Source | Temporary changes in scheduling, admission and dispatch are restored after capture; R3-13 owns measured follow-up work |
| Interpretation | Admission and pre-send recheck grow with outstanding orders. Preserve both checks and their exact decisions while removing repeated computation; warming CPU load is not qualification |

| Post-grouping profile | Median per call µs | Calls / 100 orders | Mean total per order µs |
| --- | --- | --- | --- |
| Physical pending interval | 37.292 | 400 | 174.098 |
| Pending risk rows | 62.541 | 200 | 135.386 |
| Unreflected margin | 110.229 | 200 | 253.667 |

| Profile boundary | Contract |
| --- | --- |
| Source and scope | Grouped-risk source, before the quantity index; same 2,000-event / 100 Hz recipe. Temporary stage timing records median admission 326.771 µs, quantization/protection 161.875 µs and pre-send recheck 404.812 µs (`/tmp/r3-grouped-profile-unloaded.log`). |
| Detailed capture | `/tmp/r3-detailed-risk-profile-unloaded.log`; per-call timers and stderr output make this diagnostic. Source instrumentation is removed and the normal release is rebuilt before the quantity-index cells. |

## Invariants

- Must retain measured cells, missing opportunities and empty sample sets.
- Must distinguish synthetic process workloads, embedded accounting and authenticated host evidence.
- Must rebuild and remeasure changed execution code before applying these numbers to it.
- Must preserve callback/dispatch durability and exact reduction identity through restart.

## Operational Recipes

```sh
TASK_SYSROOT="$(rustup run 1.90.0 rustc --print sysroot)"
export PATH="$TASK_SYSROOT/bin:$PATH" RUSTC="$TASK_SYSROOT/bin/rustc" RUSTDOC="$TASK_SYSROOT/bin/rustdoc"
export CARGO_INCREMENTAL=0
cargo build --manifest-path engine/Cargo.toml --release --locked -p engine-tools --bins
engine/target/release/engine-tools bench --events 6000 --rate 100 --every 20 \
  --symbols BTCUSDT --wal /tmp/bench-narrow.wal
```

```sh
# Use a fresh WAL path for each cell; omit the delay for the other wide cell.
SYMBOLS=$(python3 -c 'print(",".join(f"BENCH{i:03d}USDT" for i in range(270)))')
engine/target/release/engine-tools bench --events 12000 --rate 200 --every 20 \
  --symbols "$SYMBOLS" --venue-delay-ms 20 --wal /tmp/bench-wide-delay.wal
engine/target/release/engine-tools latency --wal /tmp/bench-wide-delay.wal
```
