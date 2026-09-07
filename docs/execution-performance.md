# Execution measurements

## Purpose

State measured execution workloads, timing boundaries and resource limits for the deployed Round-3 engine and its retained baseline.

## Spec Tables

| Decision | Current evidence |
| --- | --- |
| Deployed engine | Completed generation `905c10d3`; demo loads its engine, while identical runtime inputs leave mainnet on `fc2ad99c`. The 300-second demo soak and explicit `32858587`/`905c10d3` demo return drill pass. Dated host evidence lives in [STATE.md](../STATE.md) |
| Qualification | The `ecc3ea12` four-cell qualification passes 1,962 release tests and account workloads but fails median run-level decision p99 at 22.3 µs against 13.95 µs. All four decision cells fail; submit's median passes at 1.055 ms. The normal hosted checks pass 1,964 Rust and 1,673 Python tests. The paired-source relative implementation passes 79 focused tests; fresh hosted qualification is pending. R3-06 remains open; all preceding failures remain recorded |
| Point targets | The qualified-source remeasurement records narrow decision p50 4.751 µs, narrow submit p50 4.997119 ms and wide decision p99 15.047 µs, meeting the original thresholds with only 2.881 µs submit headroom. The estimator after cell on identical bytes measures 5.079039 ms narrow submit and misses 5 ms. The paired-source after cell on the same bytes measures 4.939775 ms narrow submit and 10.255 µs wide decision p99. Earlier repeats are 4.981 / 5.083 / 4.989 ms. These observations establish no consistent 5 ms bound or SLO |
| Latest source boundary | Callback-buffer reuse, direct binary64 normalization, immutable envelope policy, reduced products, aggregate margin division, grouped pending risk and per-symbol pending quantity totals, covered native-stop writes, fresh priced-quantity grouping and batched exact sums; borrowed prices, reused order projections, temporary route-membership bitsets, exact storage bit bounds and a venue-actor yield; normal release builds carry no temporary profiling |

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
| Before borrowed prices, unloaded | 100 / 100 | 4.4 µs / 15.0 µs | 5.07 ms / 6.54 ms | 100 | 189,739 |
| Before borrowed prices, 270 symbols | 600 / 600 | 4.3 µs / 11.3 µs | 5.37 ms / 6.25 ms | 600 | 1,176,695 |
| Borrowed prices, unloaded | 100 / 100 | 4.5 µs / 9.7 µs | 5.05 ms / 8.79 ms | 100 | 189,751 |
| Borrowed prices, 270 symbols | 600 / 600 | 4.5 µs / 11.0 µs | 5.35 ms / 6.70 ms | 600 | 1,176,695 |
| Reused order projections, unloaded | 100 / 100 | 4.8 µs / 16.1 µs | 5.07 ms / 8.07 ms | 100 | 189,759 |
| Reused order projections, 270 symbols | 600 / 600 | 4.8 µs / 10.6 µs | 5.35 ms / 6.77 ms | 600 | 1,176,681 |
| Subscription hash sets, unloaded | 100 / 100 | 4.7 µs / 9.8 µs | 5.04 ms / 7.87 ms | 100 | 189,759 |
| Subscription hash sets, 270 symbols | 600 / 600 | 4.6 µs / 11.0 µs | 5.36 ms / 6.22 ms | 600 | 1,176,681 |
| Subscription bitsets, unloaded | 100 / 100 | 4.3 µs / 15.3 µs | 5.04 ms / 6.18 ms | 100 | 189,742 |
| Subscription bitsets, 270 symbols | 600 / 600 | 4.4 µs / 11.6 µs | 5.24 ms / 6.64 ms | 600 | 1,176,697 |
| Exact storage bit bound, unloaded | 100 / 100 | 4.6 µs / 11.4 µs | 5.02 ms / 6.30 ms | 100 | 189,747 |
| Exact storage bit bound, 270 symbols | 600 / 600 | 4.4 µs / 11.6 µs | 5.18 ms / 6.46 ms | 600 | 1,176,695 |
| Venue actor yield, unloaded | 100 / 100 | 4.3 µs / 13.4 µs | 5.03 ms / 6.78 ms | 100 | 189,740 |
| Venue actor yield, 270 symbols | 600 / 600 | 4.3 µs / 13.3 µs | 5.14 ms / 6.53 ms | 600 | 1,176,684 |
| Venue actor yield, unloaded repeat 1 | 100 / 100 | 4.5 µs / 14.3 µs | 4.98 ms / 6.37 ms | 100 | 189,734 |
| Venue actor yield, unloaded repeat 2 | 100 / 100 | 4.7 µs / 23.9 µs | 5.08 ms / 6.48 ms | 100 | 189,737 |
| Venue actor yield, unloaded repeat 3 | 100 / 100 | 4.2 µs / 7.5 µs | 4.99 ms / 6.21 ms | 100 | 189,734 |
| Qualified follow-up remeasurement, unloaded | 100 / 100 | 4.751 µs / 16.639 µs | 4.997119 ms / 6.340607 ms | 100 | 189,734 |
| Qualified follow-up remeasurement, 270 symbols | 600 / 600 | 4.667 µs / 15.047 µs | 5.140479 ms / 6.504447 ms | 600 | 1,176,684 |
| Four-cell qualifier after, unloaded | 100 / 100 | 5.087 µs / 26.127 µs | 5.079039 ms / 6.467583 ms | 100 | 189,734 |
| Four-cell qualifier after, 270 symbols | 600 / 600 | 4.503 µs / 11.423 µs | 5.140479 ms / 6.471679 ms | 600 | 1,176,684 |
| Paired-source qualifier after, unloaded | 100 / 100 | 4.711 µs / 7.375 µs | 4.939775 ms / 6.291455 ms | 100 | 189,731 |
| Paired-source qualifier after, 270 symbols | 600 / 600 | 4.295 µs / 10.255 µs | 5.136383 ms / 6.291455 ms | 600 | 1,176,684 |

| Candidate boundary | Observation |
| --- | --- |
| Operational-only remeasurement | `/tmp/r3-selected-pair-{unloaded,wide-270}.{log,wal}`; exact final notes `/tmp/r3-selected-pair-quantiles.log`. No test or compiler runs during either cell; both WALs validate and all 700 opportunities complete with one barrier each, zero failures. Runtime inputs equal `fc2ad99c`; the new demo-helper option does not run inside this benchmark |
| Remeasurement build scope | Engine-tools SHA256 `8b7d44c3b68ee4e78bb54bcdf8632e77e631821b02e6ce1590a54e60f7ca5a5a`, unchanged within this pair, carries the precommit `32858587-dirty` label and is produced during the final release qualification. The earlier yield/repeat image is `2249e4b746d6ecef67cdccec4cae4dc1a48d17f3cab38be7cb2d8b840baa0680`; these are different build bytes. No timing improvement is attributed to the operational helper |
| Four-cell qualifier boundary | The qualified follow-up pair is the before boundary; `/tmp/r3-four-cell-estimator-{unloaded,wide-270}.{log,wal}` is the after pair on the same Mac and unchanged `8b7d44c3` executable bytes, with no concurrent compiler or tests. Both WALs validate; all 700 orders have one barrier and zero failures. Exact notes: `/tmp/r3-four-cell-estimator-quantiles.log`. Barrier medians are 4.239359 / 4.167679 ms. Both decision targets pass, but narrow submit misses 5 ms and narrow p99 exceeds the unchanged 21.3 µs Mac budget. This Python-only change establishes no runtime timing improvement |
| Paired-source qualifier boundary | The four-cell pair is the before boundary; `/tmp/r3-paired-source-estimator-{unloaded,wide-270}.{log,wal}` is the after pair, 06:43:55–06:45:15 UTC. The same Mac and unchanged `8b7d44c3` executable run without concurrent compiler or tests. Both WALs validate; all 700 orders have one barrier and zero failures. Exact notes: `/tmp/r3-paired-source-estimator-quantiles.log`; barrier medians 4.192255 / 4.157439 ms. Narrow decision/submit and wide decision targets pass; wide submit remains above 5 ms. The helper change establishes no runtime timing improvement |
| R3-13 point acceptance | Narrow decision p50 and submit p50, wide decision p99 and one-barrier counts meet the original targets on the qualified source. Existing original-algorithm comparisons preserve exact values, canonical bytes, read/refusal ordering and lifecycle outcomes. Narrow submit has 0.05762% headroom; prior misses remain visible and no repeated-run SLO is established |
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
| Current developer qualification | `scripts/dev.sh check` passes 1,962 Rust tests (zero failed, seven ignored), 1,646 Python tests, formatting and strict workspace Clippy (`/tmp/r3-latency-followup-developer-check.log`). Release all-target tests pass 1,961 tests, zero failed, seven ignored (`/tmp/r3-latency-followup-release-tests.log`). Both copied-WAL fixtures pass separately in release, including all 12 missing-stop repairs and full-prefix/rotated reboot (`/tmp/r3-latency-followup-boot-fixtures.log`); transport, risk and collateral are mocked. All six heavy seeds pass with two crashes each and byte-identical repeat WALs (`/tmp/r3-latency-followup-heavy-sim.log`). The six individual venue feature builds/suites, combined 810-test venue/public/market-data suite and strict all-feature Clippy qualify the earlier embedded boundary; this follow-up has default-Bybit qualification. |
| Priced quantity grouping | Fresh assessments group quantities by effective price and stop fraction before notional multiplication; margin groups quantities by effective price. Validation and price-read ordering remain unchanged. `/tmp/r3-priced-quantities-{unloaded,wide-270}.log` and matching WALs; build `/tmp/r3-priced-quantities-release-build.log`. The preceding covered-stop cells are the before boundary. |
| Priced quantity parity | The original rowwise valuation matches exact totals, errors, canonical bytes and price-read order across 512 lifecycle steps and three price-query cases. The existing 1,024-step margin comparison includes repeated prices across symbols. Both comparisons pass before and after; all 161 risk tests pass. |
| Batched rational sums | ExactSum combines equal-denominator runs and reduces before a denominator change and at finish. Stored Exact values remain canonical. The original rational sum matches values and canonical bytes at 4,096 prefixes, including cancellation and extreme scales. All 226 type/risk checks pass. `/tmp/r3-batched-ratios-{unloaded,wide-270}.log` and matching WALs; build `/tmp/r3-batched-ratios-release-build.log`. |
| Borrowed positive quantities | Grouping borrows nonnegative quantities; negative quantities retain their exact magnitude. All 161 risk tests pass. `/tmp/r3-borrowed-quantities-{unloaded,wide-270}.log` and matching WALs; build `/tmp/r3-borrowed-quantities-release-build.log`. |
| Measurement scope | These six cells run without a concurrent test or compiler. All 2,100 opportunities complete with one barrier each and zero failures. Both decision targets pass throughout. Narrow submit medians remain above 5 ms; barrier medians are 4.20 / 4.14 ms, 4.20 / 4.15 ms and 4.19 / 4.15 ms respectively. |
| Borrowed stop fractions | Assessment keys borrow the immutable stop fraction and clone only distinct output groups. All 161 risk tests and strict type/risk Clippy pass. `/tmp/r3-borrowed-fractions-{unloaded,wide-270}.log` and matching WALs; build `/tmp/r3-borrowed-fractions-release-build.log`. All 700 opportunities complete with one barrier each and zero failures; no compiler/test runs concurrently. Barrier medians are 4.19 / 4.17 ms. Both decision targets and the unchanged Mac budget pass, but narrow submit still exceeds 5 ms and wide submit exceeds one barrier plus 1 ms. |
| Borrowed prices | Fresh risk and margin grouping borrow effective prices and clone only retained/output values. All 161 risk tests and strict risk Clippy pass. `/tmp/r3-before-borrowed-prices-{unloaded,wide-270}.log` and `/tmp/r3-borrowed-prices-{unloaded,wide-270}.log`, matching WALs and executable hashes retain both boundaries. All 1,400 opportunities complete with one barrier each and zero failures, without a concurrent compiler/test. Barrier medians are 4.20 / 4.16 ms before and after. Submit medians improve 20 µs each; narrow 5 ms and wide one-barrier-plus-1-ms acceptance remain unmet. Narrow after p99 includes an 8.28 ms barrier tail; no sample is discarded. |
| Reused order projections | Validation and application compute decimal validity and f64 projections once per operation. The 512-case original implementation comparison covers storage errors, projection errors, partial request updates and canonical bytes across both order kinds and three sleeve effects. It passes before and after; all 227 type/risk tests and strict type/risk Clippy pass. `/tmp/r3-projection-{unloaded,wide-270}.log`, matching WALs and executable hash; no concurrent compiler/test. All 700 opportunities complete, one barrier each, zero failures. Barrier medians are 4.20 / 4.16 ms. These cells establish no end-to-end improvement over the borrowed-price boundary; both submit targets remain open. |
| Subscription hash-set attempt | `/tmp/r3-route-membership-{unloaded,wide-270}.log` and matching WALs; all 700 opportunities complete, one barrier each, zero failures, without concurrent compiler/tests. Narrow/wide barrier medians remain 4.20 / 4.16 ms. Wide dispatch queue rises from 164.5 to 187.6 µs and submit changes from 5.35 to 5.36 ms; this attempt establishes no improvement. The temporary membership representation is replaced under R3-15. |
| Subscription bitsets | Fresh per-symbol feed bits represent current, retained and required membership. Route iteration and partial admission updates stay in their original order. The comparison covers 270 interned names, first/later admission failure, retries and retirement; the inactive-sleeve restart/settlement case also passes. Strict core Clippy passes. `/tmp/r3-route-bitsets-{unloaded,wide-270}.log` and matching WALs; all 700 opportunities complete, one barrier each, zero failures, without concurrent compiler/tests. Barrier medians are 4.19 / 4.16 ms; wide dispatch queue falls to 107.5 µs. Wide submit improves 0.11 ms from the original projection boundary; narrow submit remains 5.04 ms. |
| Exact storage bit bound | Magnitudes with at most 3N bits have at most N decimal digits because 8^N < 10^N. The existing decimal boundary and serialization remain unchanged. The original validator matches signed numerator/denominator cases around both bit thresholds and the decimal limit, including canonical bytes and readback. The comparison passes before and after; 12 order-term tests, all 161 risk tests and strict type/risk Clippy pass. `/tmp/r3-bitbound-{unloaded,wide-270}.log` and matching WALs; all 700 opportunities complete, one barrier each, zero failures, without concurrent compiler/tests. Barrier medians are 4.21 / 4.17 ms. Submit p50 improves to 5.02 / 5.18 ms; wide dispatch queue remains 106.8 µs. |
| Venue actor yield | Yield after durable authorization and complete mutation registration, before route maintenance. The full core suite passes 810 tests, zero failed, two ignored (`/tmp/r3-dispatch-yield-core-tests-final.log`). Its first run exposes a test that expects a pending rejection from an immediate mock reply; the fixture now creates that condition with a 1 ms virtual delay, retaining every assertion. `/tmp/r3-dispatch-yield-{unloaded,wide-270}.log` and matching WALs; all 700 opportunities complete, one barrier each, zero failures, without concurrent compiler/tests. Barrier medians are 4.20 / 4.16 ms. Wide dispatch queue falls to 4.4 µs and submit to 5.14 ms, passing one barrier plus 1 ms. Narrow submit remains 5.03 ms; three unchanged narrow repeats assess this remaining boundary. |
| Unchanged narrow repeats | Three runs are declared before execution; `/tmp/r3-dispatch-yield-repeat-{1,2,3}.log` and matching WALs retain all cells. The Rust WAL reader validates all five final WALs. Exact stored submit p50 is 4,980,735 / 5,083,135 / 4,988,927 ns; median of those run medians is 4,988,927 ns, not a pooled percentile. One run misses 5 ms; its decision p99 also exceeds the unchanged Mac CI budget. All 1,000 opportunities across the final pair and three repeats complete, with one barrier each and zero failures. Each submit median is at most its own dispatch-barrier median plus 1 ms. `/tmp/r3-dispatch-yield-exact-quantiles.jsonl` contains the extracted quantiles from the validated WAL notes. |

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

| Buffered borrowed-price profile | Median per call µs | Calls / 100 orders |
| --- | --- | --- |
| Prepare intent, inclusive | 294.772 | 100 |
| Assess intent | 112.604 | 100 |
| Quantize order and physical protection | 115.229 | 100 |
| Commit prepared order | 46.291 | 100 |
| Pre-send risk/protection recheck | 150.750 | 100 |
| Risk inventory evaluation | 83.145 | 200 |
| Pending risk rows | 12.313 | 200 |
| Unreflected margin | 14.709 | 200 |

| Profile boundary | Contract |
| --- | --- |
| Buffered capture | `/tmp/r3-buffered-price-profile.log`, `/tmp/r3-buffered-price-profile-summary.txt`; same 2,000-event workload. In-memory samples print after the run. Timing and locking overhead remains; nested rows overlap and cannot be summed |
| Rejected diagnostic | Per-call stderr in `/tmp/r3-borrowed-price-profile.log` inflates parent spans; its inclusive totals do not support an optimization claim |
| Restoration | Every temporary source file is restored with hash checks. `/tmp/r3-borrowed-prices-normal-rebuild.log` rebuilds the normal release before further acceptance measurements |

| Wide post-projection profile | Median per call µs | Calls |
| --- | --- | --- |
| After-turn work | 139.375 | 13,438 |
| Portfolio route maintenance, included above | 136.875 | 13,438 |
| Required portfolio routes, included above | 38.583 | 13,439 |
| Risk assessment | 172.833 | 600 |
| Order quantization and physical protection | 91.062 | 600 |
| Pre-send risk/protection recheck | 207.730 | 600 |

| Profile boundary | Contract |
| --- | --- |
| Capture | `/tmp/r3-wide-stage-profile.log`, `/tmp/r3-wide-stage-profile-summary.txt`; 12,000 events / 270 symbols / 200 Hz, with buffered temporary timing. Nested spans overlap; this is diagnostic. Source is restored by hash before implementation continues |
| Follow-up | Fresh membership bitsets replace repeated linear scans. The uninstrumented projection cell records wide dispatch queue p50 164.5 µs; bitsets measure 107.5 µs, and the subsequent venue yield measures 4.4 µs |

### Hosted Linux qualification

| Boundary | Evidence |
| --- | --- |
| Fixed paired diagnostic | [Run 34084393881](https://github.com/rob435/liquidity-migration/actions/runs/34084393881) compares the qualified `a4189a48` archive (A) with a fresh native-target `fc2ad99c` build (B) on one Intel Xeon Platinum 8573C / ext4 worker in fixed order `A B B A B A A B`. All eight processes exit zero; all 800 opportunities complete with one barrier each and zero failures. Two A and two B cells fail the original 9,000 ns decision limit. Raw logs/WALs under `/tmp/r3-latency-paired-34084393881`; each WAL validates. The temporary workflow is removed after this experiment |
| Diagnostic build scope | A engine-tools SHA256 `7281865d18f0a1f03b9a607d1f0fc89df555f8ad8cae8f7aa6db0497ea818eaf`; B `df7851bc59b743ab85d7db10dbd48cc77b3ff84a51f91769f360ef5483bb8a5c`. All three sibling binaries are fixed through the eight cells. B is freshly rebuilt diagnostic code; it does not replace the failed qualification or identify its unavailable bytes |
| Source / runner | `a4189a4897409e65acba7a2078b964986ceea928`, Rust 1.90.0, `ubuntu-latest`, `x86_64-unknown-linux-gnu`; [run 34074530152](https://github.com/rob435/liquidity-migration/actions/runs/34074530152) |
| Build scope | The qualification job builds and tests its own release binaries with an explicit native target. Their hashes differ from the separately built deployment artifact; these measurements bind to the qualification artifact and source commit |
| Recipe | 2,000 events / 100 Hz / BTCUSDT / every 20; 100 completed submits, 100 dispatch barriers, zero barrier failures |
| Cell | Decision p50 / p99: 5.0 / 6.0 µs; submit p50 / p99: 1.09 / 2.40 ms; dispatch barrier p50: 635.9 µs |
| Optimized checks | 1,959 tests passed, zero failed, seven ignored; two million account-state operations with 65,536 retained IDs, plus repeated history-recovery workloads |
| Budget | [execution-latency-budgets.toml](execution-latency-budgets.toml) registers baseline source `a4189a4897409e65acba7a2078b964986ceea928`. Linux qualification uses 1.5× its same-worker median decision p99 and submit p50. The stored 9,300 / 1,090,000 ns references and 13,950 / 1,635,000 ns limits remain absolute diagnostics with separate verdicts. Darwin keeps four candidate cells and its unchanged absolute budget; standalone log checking remains absolute on both platforms |
| Calibration boundary | The first run passes provisional 75,000 / 7,500,000 ns limits. Its unmodified log also passes the calibrated limits; the budget update has no runtime change |
| Artifact | `/tmp/r3-hosted-qualified-a4189a48/engine-binaries-a4189a4897409e65acba7a2078b964986ceea928-qualified.tar.gz`; verified checksums and embedded qualification log. Raw log: `/tmp/r3-hosted-qualification-raw.log` |
| Scope | One hosted worker does not establish venue-network latency or a universal Linux bound. Mac point targets are measured separately |
| Calibrated repeat | [Run 34076340582](https://github.com/rob435/liquidity-migration/actions/runs/34076340582), source `32858587` with identical runtime inputs: 100/100 submits, 100 barriers, zero failures. Decision p50 / p99 3.9 / 16.6 µs; submit p50 / p99 1.51 / 149.16 ms; barrier p50 / p99 987.6 µs / 148.64 ms. Decision p99 fails the 9.0 µs limit; submit p50 passes 1.635 ms. The failed job uploads no qualified artifact. Raw log `/tmp/r3-hosted-calibrated-qualification.log` |
| Runner comparison | The successful qualification uses worker `d47b96e7-a977-4ae7-a736-c8a3302651c0` in `eastus`; the failing qualification uses `1ea9fbaa-7e54-433c-8657-5a073e9b6d45` in `westus3`. Both report Ubuntu 24.04.4 and image `20260831.293.1`; CPU and filesystem identity are absent. Replaying both logs through the current checker reproduces their original verdicts. Different workers limit the comparison but do not identify the cause |
| Deployed-source qualification | [Run 34081614240](https://github.com/rob435/liquidity-migration/actions/runs/34081614240), source `fc2ad99c`, passes 1,962 release tests, zero failed, seven ignored, plus two million account-state operations and repeated history workloads. Its separate native-target build completes 100/100 submits with 100 barriers and zero failures. Decision p50 / p99 is 5.8 / 9.3 µs; submit p50 / p99 is 1.16 / 3.53 ms; barrier p50 / p99 is 628.2 µs / 3.04 ms. Decision p99 fails the unchanged 9.0 µs limit; submit passes 1.635 ms. No qualified archive is uploaded. Raw log `/tmp/r3-fc2ad99c-hosted-qualification.log`; these are different build bytes from the deployed archive |
| Calibration decision | Identical A bytes fail the original decision limit twice on the same worker: source changes are unnecessary for this failure. The four A printed p99 values have median 9,300 ns (exact WAL summary 9,299 ns); this calibration statistic is not a pooled percentile. Only the failing decision reference changes. All eight original submit medians pass, so its reference stays fixed. The precise scheduling/storage cause remains unidentified |
| Fresh calibrated qualification | [Run 34085706786](https://github.com/rob435/liquidity-migration/actions/runs/34085706786), source `905c10d3`, passes 1,962 release tests, zero failed, seven ignored, plus account-state/history workloads. Its separate native-target build completes 100/100 submits, 100 barriers and zero failures. Decision p50 / p99 is 5.4 / 14.3 µs; submit p50 / p99 is 1.16 / 2.61 ms; barrier p50 / p99 is 633.9 µs / 2.12 ms. Decision p99 fails 13.95 µs; submit passes 1.635 ms. No qualified archive is uploaded. Raw log `/tmp/r3-905c10d3-hosted-qualification.log` |
| Candidate estimator | Linux builds reference A and candidate B with the same pinned compiler, native target and build command. All builds, candidate tests, soak and smoke finish before fixed `A B B A B A A B` cells, each with a fresh WAL. Four run-level metrics per image form each median; no pooled percentiles. Existing `qualification.log` and `latency_budget` retain source commits, frozen binary hashes, all raw logs and individual absolute verdicts. Process failure or missing, empty, duplicate, malformed or unordered selected histograms fails qualification after all fixed cells execute. Benchmark WALs remain temporary; the strict archive layout is unchanged and contains only candidate binaries |
| Estimator regressions | The existing `qualify()` API fails before on 14,300 > 13,950 ns and passes afterward at median decision 9,000 ns while retaining the first failed verdict (`/tmp/r3-four-cell-qualifier-{before,after}.log`). A malformed duplicate histogram is accepted before its parser correction and rejected afterward (`/tmp/r3-four-cell-malformed-duplicate-before.log`). All 61 focused tests and three doc-link tests pass (`/tmp/r3-four-cell-root-focused-corrected.log`). Coverage includes four-cell doubled-histogram rejection, fatal process/output errors, fresh paths, retained logs and the 13,950.5 ns median boundary. Replaying each four-cell A/B log set through the estimator passes its original medians and fails all four doubled selected-histogram sets (`/tmp/r3-four-cell-real-log-check.log`); this log replay is not fresh qualification |
| Four-cell qualification | [Run 34088883848](https://github.com/rob435/liquidity-migration/actions/runs/34088883848), source `ecc3ea12`, passes 1,962 release tests, zero failed, seven ignored, and account-state/history workloads. Its four fresh cells complete all 400 orders with one barrier each and zero failures. Median run-level decision p99 22,300 ns fails 13,950 ns; median run-level submit p50 1,055,000 ns passes 1,635,000 ns. No qualified archive is uploaded. `/tmp/r3-ecc3ea12-hosted-qualification.log` retains every raw cell and verdict |
| Qualification contract | R3-06 remains open pending fresh hosted qualification. The implemented Linux relative contract replaces cross-worker absolute acceptance after four-cell aggregation also fails. A relative pass cannot establish the 13.95 µs absolute bound. A freshly compiled historical source is the control, not the original qualified archive bytes. Same-worker pairing reduces hardware confounding but a noisy or slow A can still hide a change. Dedicated stable hardware is the alternative for an absolute bound; a repeatable B regression against A requires runtime attribution |
| Paired-source checks | All 79 qualifier tests and three doc-link tests pass (`/tmp/r3-paired-source-root-focused.log`). The unchanged qualification entry point fails before at 22,300 > 13,950 ns (`/tmp/r3-paired-source-qualifier-before.log`); the new relative contract can pass slow-host fixtures while retaining both absolute failures. This is a contract comparison, not a runtime bug claim. Tests reject B at 2× A on either metric, retain every cell, and show that doubling an already faster B can still pass. Mutating either frozen image during the final cell prevents publication. A real archive extraction of the registered source validates the source layout and internal symlink (`/tmp/r3-paired-source-extraction.log`); no local Linux build is claimed |
| Attribution limit | The latest account-state middle/late median window costs are 179 / 182 ns per operation versus 186 / 187 ns in `905c10d3`, while decision and storage tails worsen. This does not support a uniform CPU slowdown or identify the scheduling/storage cause. There is no same-worker baseline cell in this failed qualification |

| Fresh `ecc3ea12` cell | Decision p50 / p99 µs | Submit p50 / p99 ms | Barrier p50 / p99 ms | Individual decision budget |
| --- | --- | --- | --- | --- |
| 1 | 10.8 / 32.0 | 1.09 / 116.06 | 0.6420 / 115.61 | Fail |
| 2 | 8.0 / 28.9 | 1.04 / 206.96 | 0.5888 / 206.44 | Fail |
| 3 | 9.2 / 15.5 | 1.03 / 170.52 | 0.5811 / 170.13 | Fail |
| 4 | 6.5 / 15.7 | 1.07 / 182.58 | 0.6538 / 182.19 | Fail |

| Fixed cell | Decision p99 µs | Submit p50 ms | Original budget | Revised budget |
| --- | --- | --- | --- | --- |
| 01 A | 7.9 | 1.31 | Pass | Pass |
| 02 B | 8.6 | 1.23 | Pass | Pass |
| 03 B | 18.9 | 1.27 | Fail | Fail |
| 04 A | 10.0 | 1.31 | Fail | Pass |
| 05 B | 9.4 | 1.33 | Fail | Pass |
| 06 A | 8.6 | 1.34 | Pass | Pass |
| 07 A | 10.5 | 1.29 | Fail | Pass |
| 08 B | 8.0 | 1.28 | Pass | Pass |

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
