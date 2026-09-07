# Execution measurements

## Purpose

State measured execution workloads, timing boundaries and resource limits for the deployed Round-3 engine and its retained baseline.

## Spec Tables

| Decision | Current evidence |
| --- | --- |
| Deployed engine | Completed generation `70f4c557`; both realms load its verified engine and worker after 300 healthy demo seconds. Compatible `8c92c964` reader binaries and the archive remain intact on the host. Dated images, account/protection and readiness evidence live in [STATE.md](../STATE.md) |
| Qualification | Isolated-build [run 34128439094](https://github.com/rob435/liquidity-migration/actions/runs/34128439094) at deployed `70f4c557` passes 1,981 release tests, zero failed, eight ignored, account/history workloads and all eight fixed latency cells. Candidate median decision p99 / submit p50 is 2,000 / 721,150 ns, passing absolute and same-worker relative limits. The separate qualified Linux archive verifies. Candidate submit p99 reaches 23.22 ms and the maximum 86.05 ms; selected-median acceptance is not a tail bound. All prior cells and failures remain below |
| Point targets | Final local exact-only image `5d90e8f3` meets narrow decision p50 7.959 µs, wide decision p99 26.431 µs and narrow submit p50 4.882431 ms. All 700 opportunities complete with one barrier and valid readbacks. The frozen old-image control still misses submit at 5.283839 ms after release-cache cleanup; every earlier miss remains below. The fixed comparison does not establish a stable bound or assign the submit difference solely to source. The final developer gate, optimized qualification and deployment pass; R3-13 acceptance is complete |
| Latest source boundary | Callback-buffer reuse, direct binary64 normalization, immutable envelope policy, reduced products, aggregate margin division, grouped pending risk and per-symbol pending quantity totals, covered native-stop writes, fresh priced-quantity grouping and batched exact sums; borrowed prices, reused order projections, temporary route-membership bitsets, exact storage bit bounds and a venue-actor yield; per-symbol virtual-stop reads and a split synchronous stop/control path remove full snapshots and empty nested futures. Normal release builds carry no temporary profiling |

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
| Absolute enforcement before, unloaded | 100 / 100 | 5.503 µs / 11.295 µs | 5.136383 ms / 6.512639 ms | 100 | 189,737 |
| Absolute enforcement before, 270 symbols | 600 / 600 | 4.751 µs / 11.295 µs | 5.193727 ms / 6.709247 ms | 600 | 1,176,685 |
| Absolute enforcement after, unloaded | 100 / 100 | 4.503 µs / 9.423 µs | 5.058559 ms / 6.316031 ms | 100 | 189,732 |
| Absolute enforcement after, 270 symbols | 600 / 600 | 6.211 µs / 28.463 µs | 5.206015 ms / 7.249919 ms | 600 | 1,176,694 |
| WAL converter before, unloaded | 100 / 100 | 17.007 µs / 36.351 µs | 4.886527 ms / 5.636095 ms | 100 | 189,737 |
| WAL converter before, 270 symbols | 600 / 600 | 16.751 µs / 35.551 µs | 4.812799 ms / 5.836799 ms | 600 | 1,176,686 |
| WAL converter after, unloaded | 100 / 100 | 17.263 µs / 55.103 µs | 4.968447 ms / 7.360511 ms | 100 | 189,748 |
| WAL converter after, 270 symbols | 600 / 600 | 7.127 µs / 23.711 µs | 5.488639 ms / 6.754303 ms | 600 | 1,176,688 |
| Frozen control 1A, unloaded | 100 / 100 | 7.003 µs / 41.215 µs | 4.812799 ms / 5.496831 ms | 100 | 189,741 |
| Frozen control 2B, unloaded | 100 / 100 | 7.751 µs / 18.879 µs | 4.718591 ms / 6.017023 ms | 100 | 189,737 |
| Frozen control 3B, unloaded | 100 / 100 | 8.631 µs / 25.599 µs | 4.968447 ms / 8.228863 ms | 100 | 189,735 |
| Frozen control 4A, unloaded | 100 / 100 | 7.919 µs / 36.671 µs | 5.324799 ms / 7.069695 ms | 100 | 189,740 |
| Sim metadata before, unloaded | 100 / 100 | 16.591 µs / 49.919 µs | 4.927487 ms / 5.537791 ms | 100 | 189,737 |
| Sim metadata before, 270 symbols | 600 / 600 | 14.919 µs / 40.735 µs | 5.226495 ms / 6.471679 ms | 600 | 1,176,690 |
| Reader stage A after, unloaded | 100 / 100 | 15.671 µs / 37.471 µs | 4.968447 ms / 7.610367 ms | 100 | 189,748 |
| Reader stage A after, 270 symbols | 600 / 600 | 15.583 µs / 25.919 µs | 5.234687 ms / 5.939199 ms | 600 | 1,176,690 |
| Exact metadata + Python-import removal after, unloaded | 100 / 100 | 20.127 µs / 47.583 µs | 4.878335 ms / 5.722111 ms | 100 | 189,738 |
| Exact metadata + Python-import removal after, 270 symbols | 600 / 600 | 12.215 µs / 38.303 µs | 5.246975 ms / 6.266879 ms | 600 | 1,176,689 |
| Reduced readers + stop-path simplification after, unloaded | 100 / 100 | 6.083 µs / 15.671 µs | 5.406719 ms / 6.782975 ms | 100 | 189,741 |
| Reduced readers + stop-path simplification after, 270 symbols | 600 / 600 | 5.795 µs / 21.167 µs | 5.521407 ms / 7.061503 ms | 600 | 1,176,687 |
| Reader/stop control 1A, unloaded | 100 / 100 | 7.667 µs / 24.095 µs | 5.353471 ms / 6.873087 ms | 100 | 189,755 |
| Reader/stop control 2B, unloaded | 100 / 100 | 6.459 µs / 34.175 µs | 5.267455 ms / 6.598655 ms | 100 | 189,741 |
| Reader/stop control 3B, unloaded | 100 / 100 | 5.795 µs / 31.055 µs | 5.361663 ms / 6.463487 ms | 100 | 189,738 |
| Reader/stop control 4A, unloaded | 100 / 100 | 6.667 µs / 25.167 µs | 5.230591 ms / 6.586367 ms | 100 | 189,738 |
| Streamed portfolio routes after, unloaded | 100 / 100 | 6.211 µs / 22.959 µs | 5.386239 ms / 6.713343 ms | 100 | 189,741 |
| Streamed portfolio routes after, 270 symbols | 600 / 600 | 5.375 µs / 18.591 µs | 5.410815 ms / 6.971391 ms | 600 | 1,176,689 |
| Folded physical stops after, unloaded | 100 / 100 | 5.711 µs / 16.007 µs | 6.094847 ms / 7.167999 ms | 100 | 189,743 |
| Folded physical stops after, 270 symbols | 600 / 600 | 5.667 µs / 10.919 µs | 5.787647 ms / 7.053311 ms | 600 | 1,176,687 |
| Cleared release cache, frozen folded-stop control, unloaded | 100 / 100 | 5.211 µs / 14.215 µs | 5.283839 ms / 6.217727 ms | 100 | 189,737 |
| Cleared release cache, frozen folded-stop control, 270 symbols | 600 / 600 | 5.795 µs / 15.711 µs | 5.365759 ms / 6.725631 ms | 600 | 1,176,687 |
| Exact-only ordinary admission after, unloaded | 100 / 100 | 7.959 µs / 26.639 µs | 4.882431 ms / 6.836223 ms | 100 | 189,738 |
| Exact-only ordinary admission after, 270 symbols | 600 / 600 | 6.751 µs / 26.431 µs | 5.287935 ms / 6.787071 ms | 600 | 1,176,696 |

| Candidate boundary | Observation |
| --- | --- |
| Sim metadata before boundary | `/tmp/r3-sim-metadata-before-{unloaded,wide-270}.{log,wal}`, 09:10:15–09:11:35 UTC, unchanged `8ef609da` image on the same Mac. No concurrent compiler or tests; both Rust readbacks pass. All 700 orders complete with one barrier each and zero failures. Narrow decision p50 misses; narrow submit and wide decision p99 pass. Observed barrier medians are 4.167679 / 4.202495 ms; exact notes `/tmp/r3-sim-metadata-before-quantiles.log`. Metadata implementation and its combined after boundary appear below |
| WAL converter boundary | `/tmp/r3-wal-converter-{before,after}-{unloaded,wide-270}.{log,wal}`; before 08:28:50–08:30:10 UTC, after 08:32:42–08:34:03 UTC, same Mac, no concurrent compiler, tests or large capture scans. Before image `8b7d44c3b68ee4e78bb54bcdf8632e77e631821b02e6ce1590a54e60f7ca5a5a`; after `8ef609dab086e14875151d94215b1e8c4e353c43db5486c5c32e85d484abf174`. Each pair completes 700 orders with one barrier each and zero failures. Narrow decision p50 misses 10 µs in both phases; wide decision and narrow submit pass. Exact notes: `/tmp/r3-wal-converter-{before,after}-quantiles.log`; observed barrier medians before 4.167679 / 4.100095 ms, after 4.202495 / 4.276223 ms. These cells alone do not establish a converter-induced order-path regression; the fixed control below measures the same frozen images |
| Reader stage A boundary | `/tmp/r3-reader-stage-a-after-{unloaded,wide-270}.{log,wal}`, 10:11:31–10:12:51 UTC; frozen tools SHA256 `74593ffcf8da3d05787d40cf22c78643901869824947ca90bd7b89d24f720318`, pinned Rust 1.90, same Mac/recipe, no compiler/tests/scans during either cell. Shared before cells are the 09:10 sim-metadata boundary below: narrow decision 16.591 µs and submit 4.927487 ms, wide decision p99 40.735 µs. Stage A contains R3-17/18, R3-19 readers/economics and R3-20 engine-clock retries; runtime grid writers/R3-16 metadata remain held. All 700 orders complete with one barrier, zero failures and valid Rust readback. Exact summaries: `/tmp/r3-reader-stage-a-after-quantiles.log`; observed barrier p50 4.198399/4.190207 ms. Narrow decision still misses 10 µs; narrow submit and wide decision pass. No isolated per-change speedup is claimed |
| Metadata/import-removal boundary | `/tmp/r3-metadata-import-removal-after-{unloaded,wide-270}.{log,wal}`, 10:45:45–10:47:05 UTC; frozen tools SHA256 `a37741f678956713cf55308fb564525a510ec96810e4c1a22d175dfe9f172210`, Rust 1.90, same Mac/recipe, no compilers/tests/large scans. Shared before is the reader Stage A pair above. This image prepares R3-16 metadata, R3-19 runtime normalized allocation writes and R3-08 Python-import removal; segment-reader deletion is absent. All 700 orders complete with one barrier, zero failures and valid Rust readbacks. Exact summaries: `/tmp/r3-metadata-import-removal-after-quantiles.log`; observed barrier p50/p99 is 4.157439/4.636671 ms narrow and 4.206591/5.222399 ms wide. Narrow decision misses; narrow submit and wide decision pass. Combined measurements do not isolate a source regression. A temporary startup-only diagnostic observes requested QoS 33 already inherited; no scheduling policy change or scheduling-adjusted acceptance cell is used |
| Reader/stop-path boundary | `/tmp/r3-reader-stop-after-{unloaded,wide-270}.{log,wal}`, 11:28:04–11:29:24 UTC; frozen ordinary tools SHA256 `b86260e466b6d45b46439f8a47991834de17bf3923e77518242f02ca6df65a70`, Rust 1.90, CARGO_INCREMENTAL=0, same Mac/recipe and unchanged clock/durability/QoS. All builds finish before both cells; no concurrent tests or large scans. The shared before is metadata/import-removal `a37741f6` above. This image adds ordinary v2–v6 reader removal, targeted virtual-stop reads and the stop/control future split. All 700 orders complete with one barrier, zero failures and valid Rust readbacks. Exact summaries: `/tmp/r3-reader-stop-after-quantiles.log`; observed barrier p50/p99 is 4.321279/5.414911 ms narrow and 4.268031/5.345279 ms wide. Both decision targets pass; narrow submit misses 5 ms. This combined pair does not isolate a source-level cause for the submit change; percentile differences are not per-order stage attribution |
| Native CPU sampling diagnostic | Frozen ordinary `b86260e4`, 12:00:20–12:00:40 UTC; the narrow recipe runs once under native `sample` for 19 seconds at 1 ms intervals. Sampling adds overhead: decision p50/p99 5.919/588.287 µs, submit p50/p99 5.324799/7.327743 ms and observed barrier p50/p99 4.157439/5.357567 ms are diagnostic only. All 100 opportunities complete with one dispatch barrier, zero failures and valid Rust readback; WAL is 189,752 bytes. `/tmp/r3-submit-sample.{bench.log,wal,stacks.txt}` and `-quantiles.log` retain results. One active route-maintenance branch contains 194 sampled stacks, including 138 under required portfolio routes and repeated vector growth/allocation. Decimal conversion appears in only three sampled stacks. Inclusive stack counts are not additive CPU durations or proof of a specific per-order saving; R3-21 compares streamed route collection against the ordinary reader/stop before pair |
| Streamed-route boundary | `/tmp/r3-route-stream-after-{unloaded,wide-270}.{log,wal}`, 12:13:00–12:14:20 UTC; ordinary tools SHA256 `ae8c86fbb0fe99eab3cf0dd1c950bda656b6f10fa2dd22c6d3d472b3674c2433`, pinned Rust 1.90 and CARGO_INCREMENTAL=0. Shared before is the ordinary reader/stop pair above. No concurrent builds/tests/scans; clocks, workload, priority and durability are unchanged. All 700 orders complete with one barrier each, zero failures and valid unchanged Rust readbacks. Observed barrier p50/p99 is 4.317183/5.349375 ms narrow and 4.214783/5.398527 ms wide. Both decision targets pass; narrow submit still misses. The same thirteen route/order-filter tests pass before/after; the compact reference algorithm matches the actual prior source. No isolated timing improvement is claimed from this pair |
| Streamed-route CPU diagnostic | The same 19-second / 1-ms native sample runs at 12:21:59–12:22:19 UTC against `ae8c86fb`; `/tmp/r3-route-stream-sample.{bench.log,wal,stacks.txt}`. Exact decision p50/p99 is 7.335/483.327 µs, submit 5.091327/7.340031 ms, observed barrier 4.083711/5.283839 ms. All 100 orders have one barrier, zero failures and a valid unchanged 710-record Rust readback. Inclusive route-maintenance stack counts are 208→84, required routes 193→67, and the direct temporary vector branch 138→0 compared with the prior native sample. These counts overlap and are diagnostic, not elapsed-time savings; final ordinary acceptance remains the pair above |
| Folded-stop boundary | `/tmp/r3-stop-candidates-after-{unloaded,wide-270}.{log,wal}`, 13:04:23–13:05:43 UTC; ordinary tools SHA256 `1a2a488706f8c2b9081f0756233927414d0b8d58a5061e4cf4b535245be50734`, 21,691,104 bytes, pinned Rust 1.90 and incremental disabled. Shared before is the streamed-route pair above. All 700 opportunities complete with one barrier, zero failures and valid unchanged Rust readbacks (710 / 4,210 records). No concurrent builds/tests/scans. Barrier p50/p99 is 5.267455 / 5.464063 ms narrow and 4.505599 / 5.378047 ms wide. Both decision targets pass; narrow submit misses, and its observed barrier median alone exceeds the 5 ms submit target. This cell does not attribute the barrier increase to the stop refactor |
| Folded-stop equivalence | Two reference functions exactly match actual HEAD bodies apart from oracle names/callee; 768 planner cases and 78 engine collection cases pass before/after with identical 624 plans / 222 errors. Malformed opposite-side legacy stops retain eager error precedence even for reducing requests. Twelve distinct focused tests and strict core all-target Clippy pass; independent source review finds no issue. `/tmp/r3-stop-candidates-{before,final,planner-final,shared-final,dispatch-final,clippy-final}.log`. Source removes owned candidate buffers while preserving fresh checks, exact output and every WAL/timing boundary |
| Pre-cleanup resource observation | After the folded-stop pair, the Mac data volume reports 6.0 GiB available and 99% capacity; compiler caches use 6.3 GiB debug / 5.4 GiB release. System memory free percentage is 55%; cumulative swap counters do not establish paging during this cell. Release cache is still present at this observation. Disk pressure is a hypothesis, not an established cause of the measured barrier increase |
| Fixed comparison setup | After freezing the exact-only image, `cargo clean --release` removes 7,393 compiler-cache files / 5.3 GiB; free space rises from 6 to 11 GiB. Debug cache, captures, logs, frozen images and owner data remain. Dry-run/removal logs `/tmp/r3-release-cache-clean{,-dry-run}.log`. This is an explicit environment change; the earlier full-cache pair remains above. One old narrow/wide pair and one new narrow/wide pair are specified before running, without retries |
| Exact-only source and cells | Frozen old A is folded-stop `1a2a488706f8c2b9081f0756233927414d0b8d58a5061e4cf4b535245be50734`; new B is `5d90e8f3bbabfa1802cf159a34f7ce42d361e9d26e8959ea01a0e8a94af4708e`, 21,672,752 bytes, pinned Rust 1.90 ordinary release with incremental disabled. The precommit Boot label is `937ba60d4bcb6488d33ef5b115c638e817039882-dirty`; the measured engine source is committed unchanged at `70f4c55770d49f04ec9ac1f0d2ebd6a5d4118831`. A runs 13:18:15–13:19:36 UTC; B runs 13:20:27–13:21:48 UTC. `/tmp/r3-exact-admission-{before,after}-{unloaded,wide-270}.{log,wal}` retain all four cells. No concurrent builds/tests/scans; workload, timing boundaries, process priority and durability are unchanged. All 1,400 opportunities complete with one barrier each, zero failures and valid unchanged Rust readbacks |
| Fixed comparison verdict | A narrow barrier p50/p99 is 4.325375 / 5.320703 ms, wide 4.280319 / 5.341183 ms; B narrow 4.114431 / 5.779455 ms, wide 4.179967 / 5.775359 ms. A still misses narrow submit at 5.283839 ms. B meets all three Mac point targets: narrow decision p50 7.959 µs, wide decision p99 26.431 µs and narrow submit p50 4.882431 ms. B decision quantiles are worse than A but remain within targets. The paired cells do not isolate storage versus source effects or establish a stable bound; B wide submit maximum is 28.606463 ms, including a 27.197439 ms observed barrier maximum |
| Exact-only qualification scope | Ordinary builds exclude optional boot and scalar admission constructors while deliberate test fixtures remain. The original quantity-conversion error priority and metadata refusals are preserved. Thirty-five matched focused checks pass before/after, plus one metadata check; strict core Clippy passes. Ten matched account-state boots preserve recovered counts, exact holdings, covered stops and may_open; exact binding adds one identity record per boot. Historical inputs remain source-identical, while full-boot soak timing now includes exact metadata/native planning. Logs `/tmp/r3-23-{soak-before,account_state_bench-after,core-clippy-after-fix}.log`. The final developer gate passes 1,981 Rust / 1,718 Python tests; hosted debug passes 1,983 tests and deployment succeeds. Optimized qualification passes 1,981 release tests and all eight fixed cells |
| Reader/stop fixed control | Fixed A B B A, 11:45:26–11:46:47 UTC, same Mac/recipe and unchanged clock/durability/priority, fresh WALs, no concurrent compiler/tests/scans and no retries. A is frozen metadata/import-removal `a37741f6`; B is current reader/stop `b86260e4`, with full hashes above. `/tmp/r3-stop-frozen-abba-{1-A,2-B,3-B,4-A}.{log,wal}`; independently extracted exact Notes and readbacks are in `/tmp/r3-stop-frozen-abba-independent-summary.json`. All four Rust readbacks pass unchanged with 710 records each; all 400 opportunities complete, one dispatch barrier each and zero failures. Observed barrier p50/p99 values are 4.321279/5.427199, 4.284415/5.353471, 4.325375/5.365759 and 4.259839/5.365759 ms. Both B decision medians are below both A medians; both B decision p99s exceed both A p99s. Every submit median misses 5 ms. Overlapping submit results do not attribute the preceding before/after increase to this source change; no scheduler trace establishes CPU placement or frequency |
| Frozen Mac control | Fixed A B B A, 08:53:49–08:55:09 UTC; A/B are the converter before/after SHA256s above. Same recipe, inherited environment and priority, fresh WALs, no concurrent compiler or tests, no retries. `/tmp/r3-mac-frozen-abba-{1-A,2-B,3-B,4-A}.{log,wal}` and `-quantiles.log`; all four Rust readbacks pass and all 400 orders have one barrier with zero failures. Observed barrier p50 values are 4.079615 / 3.973119 / 4.192255 / 4.308991 ms. Both B narrow targets pass; 4A misses submit. The old image also returns below 10 µs, so the earlier 17 µs measurements do not isolate a source regression. No scheduler trace is available; core placement, QoS and frequency remain unmeasured. Decision timing excludes initial feed wake and the subsequent durability barrier |
| Absolute enforcement boundary | `/tmp/r3-absolute-enforcement-{before,after}-{unloaded,wide-270}.{log,wal}`; before 07:55:20–07:56:40 UTC, after 07:59:25–08:00:45 UTC, same Mac and unchanged `8b7d44c3` executable, no concurrent compiler or tests. All four WALs validate; each pair completes 700 orders with one barrier each and zero failures. Exact notes: `/tmp/r3-absolute-enforcement-{before,after}-quantiles.log`. Narrow/wide barrier medians before 4.239359 / 4.177919 ms, after 4.206591 / 4.179967 ms. Both narrow submit cells miss 5 ms; the CI correction has no runtime speedup claim |
| Operational-only remeasurement | `/tmp/r3-selected-pair-{unloaded,wide-270}.{log,wal}`; exact final notes `/tmp/r3-selected-pair-quantiles.log`. No test or compiler runs during either cell; both WALs validate and all 700 opportunities complete with one barrier each, zero failures. Runtime inputs equal `fc2ad99c`; the new demo-helper option does not run inside this benchmark |
| Remeasurement build scope | Engine-tools SHA256 `8b7d44c3b68ee4e78bb54bcdf8632e77e631821b02e6ce1590a54e60f7ca5a5a`, unchanged within this pair, carries the precommit `32858587-dirty` label and is produced during the final release qualification. The earlier yield/repeat image is `2249e4b746d6ecef67cdccec4cae4dc1a48d17f3cab38be7cb2d8b840baa0680`; these are different build bytes. No timing improvement is attributed to the operational helper |
| Four-cell qualifier boundary | The qualified follow-up pair is the before boundary; `/tmp/r3-four-cell-estimator-{unloaded,wide-270}.{log,wal}` is the after pair on the same Mac and unchanged `8b7d44c3` executable bytes, with no concurrent compiler or tests. Both WALs validate; all 700 orders have one barrier and zero failures. Exact notes: `/tmp/r3-four-cell-estimator-quantiles.log`. Barrier medians are 4.239359 / 4.167679 ms. Both decision targets pass, but narrow submit misses 5 ms and narrow p99 exceeds the unchanged 21.3 µs Mac budget. This Python-only change establishes no runtime timing improvement |
| Paired-source qualifier boundary | The four-cell pair is the before boundary; `/tmp/r3-paired-source-estimator-{unloaded,wide-270}.{log,wal}` is the after pair, 06:43:55–06:45:15 UTC. The same Mac and unchanged `8b7d44c3` executable run without concurrent compiler or tests. Both WALs validate; all 700 orders have one barrier and zero failures. Exact notes: `/tmp/r3-paired-source-estimator-quantiles.log`; barrier medians 4.192255 / 4.157439 ms. Narrow decision/submit and wide decision targets pass; wide submit remains above 5 ms. The helper change establishes no runtime timing improvement |
| Frozen converter control acceptance | The earlier converter image meets narrow decision p50 and submit p50 in both predeclared B cells and wide decision p99 in its preceding 270-symbol cell, with all orders and one barrier each. The original point criteria pass; all prior misses remain visible. This does not establish a repeated-run SLO or remove the separate stored CI budget |
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
| Runtime follow-up qualification | `scripts/dev.sh check` passes 1,962 Rust tests (zero failed, seven ignored), 1,646 Python tests, formatting and strict workspace Clippy (`/tmp/r3-latency-followup-developer-check.log`). Release all-target tests pass 1,961 tests, zero failed, seven ignored (`/tmp/r3-latency-followup-release-tests.log`). Both copied-WAL fixtures pass separately in release, including all 12 missing-stop repairs and full-prefix/rotated reboot (`/tmp/r3-latency-followup-boot-fixtures.log`); transport, risk and collateral are mocked. All six heavy seeds pass with two crashes each and byte-identical repeat WALs (`/tmp/r3-latency-followup-heavy-sim.log`). The six individual venue feature builds/suites, combined 810-test venue/public/market-data suite and strict all-feature Clippy qualify the earlier embedded boundary; this follow-up has default-Bybit qualification. |
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
| Budget | [execution-latency-budgets.toml](execution-latency-budgets.toml) registers baseline source `a4189a4897409e65acba7a2078b964986ceea928`. Linux qualification requires candidate medians to pass both 1.5× same-worker reference medians and stored absolute limits. The stored 9,300 / 1,090,000 ns references and 13,950 / 1,635,000 ns limits remain unchanged. Darwin keeps four candidate cells and its unchanged absolute budget; standalone log checking remains absolute on both platforms |
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
| Qualification contract | Linux publication requires both absolute and same-worker relative acceptance. A noisy baseline can weaken the relative comparison but cannot permit an absolute median miss. A freshly compiled historical source is the control; it differs from the original qualified archive bytes. Passing observed medians does not establish a stable latency bound. All fixed cells and raw tails remain recorded |
| Paired-source checks | The 81 qualifier tests and three doc-link tests pass (`/tmp/r3-absolute-enforcement-root-focused.log`). Both absolute-fail/relative-pass regressions fail on the previous helper with `DID NOT RAISE` (`/tmp/r3-absolute-restoration-before.log`) and pass after restoring absolute enforcement. Tests also reject relative failure while absolute acceptance passes, B at 2× A on either metric, malformed cells and final-cell mutation of either image. Both failure lists remain in the verdict. The prior 79-test helper and its relative-only acceptance are not the corrected-source qualification |
| Corrected recorded-log replay | `/tmp/r3-absolute-real-log-check.log` replays the verified `6de33fa3` eight-cell raw log through the corrected checker. Every individual verdict and median matches; candidate medians pass both limits. Doubling either B histogram fails; doubling either histogram for both images preserves relative acceptance but fails the absolute limit. Only recorded process output is substituted: this is checker validation, not a fresh qualification or runtime slowdown experiment |
| Four-cell attribution limit | The `ecc3ea12` account-state middle/late median window costs are 179 / 182 ns per operation versus 186 / 187 ns in `905c10d3`, while decision and storage tails worsen. This does not support a uniform CPU slowdown or identify the scheduling/storage cause. There is no same-worker baseline cell in this failed qualification |
| Shared-target build failure | [Run 34104340078](https://github.com/rob435/liquidity-migration/actions/runs/34104340078), source `46bbb346`, builds reference A in 5m33s then fails candidate compilation with `could not find conversion in engine_wal` at 09:14:34 UTC. The candidate rebuild omits `engine-wal`; normal CI on the same commit passes 1,973 Rust / 1,693 Python tests. `/tmp/r3-46bbb346-hosted-qualification.log` retains the failure. No latency cells or qualified archive are produced. The shared target also leaves earlier paired candidate dependency attribution uncertain even though their logs and archive hashes verify. R3-17 isolates the target directories and adds the real Cargo regression below |
| Independent-build regression | `/tmp/r3-cargo-source-isolation-before.log` captures two successful real Cargo builds whose candidate prints A and fails its B assertion. `/tmp/r3-cargo-source-isolation-after-real.log` recompiles B's library and verifies B in all three packed binaries. Reference target/source are removed after freezing A; candidate keeps its own target. All 82 qualifier tests pass (`/tmp/r3-cargo-source-isolation-focused.log`). Workload output is substituted only in this small build regression. Fresh run `34111799713` at `8c92c964` independently compiles both source trees and measures all eight cells; the candidate includes a rebuilt `engine-wal`. This demonstrates R3-17's intended-build condition. Its latency rejection demonstrates stored-budget enforcement and produces no qualified archive; it does not meet the final latency targets |
| Fresh paired qualification | Run [34093133061](https://github.com/rob435/liquidity-migration/actions/runs/34093133061), source `6de33fa3`, passes 1,962 release tests, zero failed, seven ignored, plus two million account-state operations and repeated history workloads. Fixed A B B A B A A B cells run 07:25:03–07:27:43 UTC: all 800 opportunities complete, one barrier each, zero failures. A medians are 13,650 / 1,240,000 ns; B medians 10,500 / 1,250,000 ns. Relative limits 20,475 / 1,860,000 ns pass; both images' medians also pass absolute 13,950 / 1,635,000 ns limits. Cells 1A, 3B and 7A individually fail decision p99. The 6A barrier maximum is 137.93 ms; no tail bound follows from median acceptance |
| Verified paired artifact | `/tmp/r3-hosted-qualified-6de33fa3/engine-binaries-6de33fa3f6090df55e1333ba3558edc2cb4f897b-qualified.tar.gz`; all six archive members, candidate hashes and embedded log verify. Receipt `/tmp/r3-hosted-qualified-6de33fa3-verification.json`; raw embedded log `/tmp/r3-6de33fa3-qualification-raw.log`; complete hosted log `/tmp/r3-6de33fa3-hosted-qualification.log`. Independent parsing reproduces all eight raw cells and both medians. Qualification assesses local workloads and explicitly does not assess WAL compatibility; these bytes are not the deployed archive; shared-target dependency attribution for these older bytes remains unqualified; the later isolated build does not retroactively establish it |
| Paired build scope | Rust 1.90.0, native x86_64-unknown-linux-gnu, same build command. A source build takes 5m25s, B 3m50s and candidate test compilation 17m23s; all builds, tests, soak and smoke finish before any cell. A engine-tools SHA256 `ac480241b1cf8b42b8b8b6ddd2f8068b3bb2480d7edc001546fbb2efd689e9fe`; B `aa88978d476f86a4dbee43d0332b9ddeef9a685c97d8887f842a357c57d734b9`. Both full three-binary image sets remain in the verified receipt |

| Submit critical-path diagnostic | Observation |
| --- | --- |
| Source and timing | One fresh narrow 100-order run, 12:41:45–12:42:05 UTC, frozen diagnostic `f9b146b497268778f342ba2d71654ae3591d6b95f83530d7ba679f2b6631b850`. Temporary buffered timestamps span the original `on_market` entry through the original EndToEnd observation after timing journal/reply conversion and before order-update handling. No timed-path printing or WAL changes; no concurrent builds/tests/scans. Instrumentation overhead is included and this cell does not reproduce the ordinary 5.386239 ms submit miss |
| Diagnostic cell | Stored HDR decision p50/p99 7.543 / 16.847 µs, submit p50/p99 4.780031 / 5.337087 ms, observed barrier p50/p99 4.059135 / 4.399103 ms. All 100 opportunities complete with one barrier, zero failures and a valid 710-record WAL. Raw logs, complete per-order timestamps, structured intervals, summary and ordinary Rust readback remain under `/tmp/r3-order-profile-run/` |
| Interval validation | All thirteen timestamp row kinds have 100 records. Each order's 76 boundaries are monotonic; adjacent intervals and the nonoverlapping coarse phases separately sum exactly to observed EndToEnd minus the unchanged origin. Existing OrderSent/actor timestamps match the CRC-checked WAL. Nearest-rank empirical submit median is 4.776125 ms; stored HDR quantization explains its difference from the 4.780031 ms cell. Nested phase medians below must not be added to parent medians |
| Main costs | Initial risk median 102.250 µs contains kernel 84.417 µs and snapshot 2.125 µs. Exact quantization takes 81.500 µs. Fresh risk/protection is 157.625 µs, containing kernel 92.667 µs, snapshot 1.417 µs and physical protection 48.708 µs. Barrier request takes 85.458 µs and observed wait 4.057250 ms; those are separate intervals. Post-actor work inside the target is 35.083 µs, of which 10.292 µs is completion handling; subsequent updates and release lie outside the target |
| Order-count growth | Initial kernel quarter medians increase 61.542 → 90.791 → 98.542 → 105.708 µs; fresh physical protection 37.708 → 47.375 → 57.834 → 66.791 µs. Decision-to-OrderSent grows 178.583 → 230.000 → 252.708 → 261.958 µs. This supports examining repeated pending-order work; it does not isolate the source of the ordinary submit miss or justify caching fresh risk |
| Restoration | Exact fresh writes restore all five temporary source files to saved bytes and HEAD; root independently checks hashes and marker removal. Pinned ordinary rebuild succeeds and reproduces frozen streamed-route SHA256 `ae8c86fbb0fe99eab3cf0dd1c950bda656b6f10fa2dd22c6d3d472b3674c2433` exactly. The diagnostic image is never installed or used for acceptance |

| Final qualification boundary | Observation |
| --- | --- |
| Source and builds | [Run 34128439094](https://github.com/rob435/liquidity-migration/actions/runs/34128439094), candidate `70f4c55770d49f04ec9ac1f0d2ebd6a5d4118831`, reference `a4189a4897409e65acba7a2078b964986ceea928`; Rust 1.90.0, native `x86_64-unknown-linux-gnu`, incremental disabled. Separate reference/candidate targets build in 3m34s / 3m13s; release test compilation takes 10m49s. Worker `5743fce2-c1f0-4ce1-83aa-ad14ed697a22`, westus2, Ubuntu 24.04.4 image `20260831.293.1`; CPU/filesystem identity is not recorded. All compilation, workloads and smoke checks finish before the fixed cells |
| Functional scope | All 31 release summaries total 1,981 passed, zero failed, eight ignored; the normal hosted debug job passes 1,983 with two additional debug-only WAL checks. Account-state workload completes 2,000,000 operations with 65,536 live/final IDs; middle/late window medians are 102 / 102 ns per operation. Three exact recoveries each at 0 / 1,000 / 10,000 / 100,000 history rows pass; median elapsed times are 35,883 / 7,959,958 / 117,513,163 / 1,585,813,638 ns. Historical inputs are unchanged; exact boot adds metadata/native-planning work. This workload excludes network, JSON and durable WAL; different workers and boot work prevent source-only attribution against Stage B |
| Cell scope | Fixed A B B A B A A B runs 13:55:58–13:58:38 UTC with fresh WALs and the unchanged 2,000-event / 100-Hz / every-20 / BTCUSDT recipe. All 800 opportunities complete, 100 async dispatch barriers plus six other sync barriers per cell, zero failures. All individual absolute verdicts pass. Values below retain console precision; raw benchmark WALs remain temporary |
| Verdict | Median run-level decision p99 / submit p50: A 1,850 / 758,800 ns; B 2,000 / 721,150 ns. Candidate passes absolute 13,950 / 1,635,000 ns and relative 2,775 / 1,138,200 ns limits. These are medians of four per-run metrics, not pooled percentiles. Candidate cell 5 retains submit p99 23.22 ms / maximum 86.05 ms and barrier p99 23.04 ms / maximum 85.85 ms; no tail or universal latency bound follows |
| Source and hash checks | Successful packaging follows the committed post-cell clean-source and both image hash checks. The log repeats frozen hashes rather than printing an independent after-hash table. Full job log `/tmp/r3-final-qualification-job.log`; independent raw-cell review `/tmp/r3-final-qualification-independent-review.txt`. Root independently recomputes both medians and release totals from the raw log and verifies every archive binary and the embedded log |
| Packed artifact | `/tmp/r3-final-qualified-artifact/engine-binaries-70f4c55770d49f04ec9ac1f0d2ebd6a5d4118831-qualified.tar.gz`, 22,075,261 bytes, SHA256 `0aae7a0ca72e3978d334b633ada640af357f2430941cbbc0afc3564a29594d75`. Existing verifier and independent member hashing validate commit, Linux/x86_64 target, all three ELF binaries and embedded log. Log SHA256 `4b43f60e27900842d88cfc630ca817cfc30975b9c06c1305a203b8b2ac7ca8bc`; verification `/tmp/r3-final-qualification-artifact-review.json`. These qualified bytes differ from the ordinary deployed archive; WAL compatibility is explicitly not assessed by this archive |

| Final qualified image | Binary | SHA256 |
| --- | --- | --- |
| A | `engine` | `a74cf32a1beb21c041a0352ef37b133a05935d7c00bd0512d8f29b731084a925` |
| A | `engine-tools` | `9b1f5775ccf798205b127a6708c3c15dd30d84ec948e906d6d21ce360325768f` |
| A | `signal-worker` | `dedafc1e27fc4327ff47fd9bf9913e25c2ac2a26c6755303b820c566ce3fee5c` |
| B | `engine` | `baa80b7c19a5296d2f9f6d379d7a1fb02e1249aa42344813ecf80dedb39eb722` |
| B | `engine-tools` | `fcd6d4b58fc5de296febb8864898304b77aff43d88a956fc4199c4aaed1a572d` |
| B | `signal-worker` | `303bdf5396bfd1d3deb95cbeabaec4737131549f95d6b2c9baa1072cf12b3271` |

| Fresh `70f4c557` paired cell | Decision p50 / p99 / max µs | Submit p50 / p99 / max ms | Observed barrier p50 / p99 / max ms | Individual absolute budget |
| --- | --- | --- | --- | --- |
| 1A | 0.981 / 1.5 / 4.9 | 0.7757 / 8.93 / 33.72 | 0.5693 / 8.72 / 33.49 | PASS |
| 2B | 0.811 / 2.6 / 2.7 | 0.7414 / 3.45 / 6.87 | 0.5668 / 3.27 / 6.7 | PASS |
| 3B | 0.791 / 1 / 1.7 | 0.6973 / 16.69 / 72.48 | 0.5253 / 16.5 / 72.29 | PASS |
| 4A | 0.951 / 1.7 / 4.2 | 0.7485 / 4.18 / 5.13 | 0.5499 / 3.99 / 4.92 | PASS |
| 5B | 0.771 / 1.4 / 2.2 | 0.7296 / 23.22 / 86.05 | 0.5586 / 23.04 / 85.85 | PASS |
| 6A | 0.992 / 2 / 5.2 | 0.7583 / 3.56 / 13.53 | 0.5699 / 3.38 / 13.37 | PASS |
| 7A | 0.972 / 5.6 / 5.7 | 0.7593 / 26.02 / 27.79 | 0.5417 / 25.8 / 27.61 | PASS |
| 8B | 0.921 / 4.7 / 14 | 0.7127 / 7.91 / 39.45 | 0.5043 / 7.56 / 39.03 | PASS |

| Stage B qualification boundary | Observation |
| --- | --- |
| Source and builds | [Run 34119432164](https://github.com/rob435/liquidity-migration/actions/runs/34119432164), candidate `937ba60d4bcb6488d33ef5b115c638e817039882`, reference `a4189a4897409e65acba7a2078b964986ceea928`; Rust 1.90.0, native `x86_64-unknown-linux-gnu`, incremental disabled. Separate reference/candidate targets build in 5m45s / 5m12s; candidate release test compilation takes 17m19s. Every build, workload and smoke finishes before the fixed cells |
| Functional scope | All 31 release summaries total 1,974 passed, zero failed, eight ignored. Account-state workload completes 2,000,000 operations with 65,536 live/final IDs; middle/late window medians are 187 / 187 ns per operation. Three exact recoveries each at 0 / 1,000 / 10,000 / 100,000 history rows pass; median elapsed times are 71,112 / 13,961,723 / 205,270,630 / 2,672,043,659 ns. This workload excludes network, JSON and durable WAL |
| Cell scope | Fixed A B B A B A A B runs 12:28:40–12:31:20 UTC with fresh WALs and the unchanged 2,000-event / 100-Hz / every-20 / BTCUSDT recipe. All 800 opportunities complete, 100 async dispatch barriers plus six other sync barriers per cell, zero failures. All individual absolute verdicts pass. Values below retain console precision; raw benchmark WALs are temporary |
| Verdict | Median run-level decision p99 / submit p50: A 8,400 / 1,135,000 ns; B 5,950 / 1,115,000 ns. Candidate passes absolute 13,950 / 1,635,000 ns and relative 12,600 / 1,702,500 ns limits. These are medians of four per-run metrics, not pooled percentiles; no stable latency bound or Mac acceptance follows |
| Source and hash checks | Success reaches the committed post-cell clean-source check, both image hash checks and verified packaging. The log repeats frozen hashes rather than printing a separate after-hash table. Full job log `/tmp/r3-stage-b-qualification-job.log`; independent numerical review `/tmp/r3-stage-b-qualification-independent-review.txt` |
| Packed artifact | `/tmp/r3-stage-b-qualified-artifact/engine-binaries-937ba60d4bcb6488d33ef5b115c638e817039882-qualified.tar.gz`, 22,064,093 bytes, SHA256 `d8f159ee948b4b4f82c5e2232ae4d80af43cb21376395b478bfba1c9b575cf86`. Existing verifier and independent member hashing validate commit, Linux/x86_64 target, all three binaries and embedded log. Log SHA256 `0f8cdfc8a3fb0f7d04c29ecb29726de0f9568c3f46afcf7df4e5a53466460df1`. Receipts `/tmp/r3-stage-b-qualified-{verification,independent-archive}.json`. These are separate native-target qualification builds, not the deployed ordinary archive; WAL compatibility is explicitly not assessed by this archive |

| Stage B qualified image | Binary | SHA256 |
| --- | --- | --- |
| A | `engine` | `98031507067aa99c4dc488dbb78498a78292f9ee5f32212e4f0b34e688165eb9` |
| A | `engine-tools` | `60fe78b8102f0681d90ccb17794507cee015a8d6aacad383d9104a25932bb93c` |
| A | `signal-worker` | `9b3f03caacd8f5d40d0a9246b55d3da6a076ee13dce9aec4b68ec59b7d1e9337` |
| B | `engine` | `0f06b32b7d0ccc30ba02df26e553c279081b625fbaf0c6eadeba6c23d94d2249` |
| B | `engine-tools` | `5c31dc60e6881d27231540be6c98b2af632cbc090c99dfa191ffc608743a7481` |
| B | `signal-worker` | `303bdf5396bfd1d3deb95cbeabaec4737131549f95d6b2c9baa1072cf12b3271` |

| Fresh `937ba60d` paired cell | Decision p50 / p99 / max µs | Submit p50 / p99 / max ms | Observed barrier p50 / p99 / max ms | Individual absolute decision budget |
| --- | --- | --- | --- | --- |
| 1 A | 4.6 / 7.7 / 17.9 | 1.14 / 1.49 / 1.63 | 0.6881 / 0.9518 / 1.07 | Pass |
| 2 B | 3.5 / 6.2 / 8.3 | 1.11 / 1.68 / 2.60 | 0.6554 / 1.12 / 2.04 | Pass |
| 3 B | 3.8 / 5.7 / 9.4 | 1.12 / 2.13 / 2.22 | 0.6620 / 1.67 / 1.72 | Pass |
| 4 A | 4.7 / 7.1 / 13.7 | 1.12 / 2.45 / 7.45 | 0.6743 / 2.01 / 6.95 | Pass |
| 5 B | 4.1 / 6.3 / 6.4 | 1.13 / 1.41 / 2.36 | 0.6431 / 0.9400 / 1.88 | Pass |
| 6 A | 4.6 / 12.2 / 15.7 | 1.13 / 1.82 / 1.87 | 0.6564 / 1.33 / 1.35 | Pass |
| 7 A | 5.1 / 9.1 / 13.9 | 1.23 / 1.74 / 3.27 | 0.7183 / 1.22 / 2.72 | Pass |
| 8 B | 3.5 / 5.0 / 5.3 | 1.10 / 1.43 / 2.95 | 0.6636 / 1.06 / 2.39 | Pass |

| Isolated build boundary | Observation |
| --- | --- |
| Isolated hosted qualification | Run [34111799713](https://github.com/rob435/liquidity-migration/actions/runs/34111799713), candidate `8c92c96464bfe66662891c05f53a9510eefe8ba2`, reference `a4189a4897409e65acba7a2078b964986ceea928`; Rust 1.90.0, explicit native `x86_64-unknown-linux-gnu`, Ubuntu 24.04.4 image `20260831.293.1`, worker `343f3978-a576-4c05-be63-b1b1b706adca` in `eastus`. CPU/filesystem identity is not recorded. Complete log: `/tmp/r3-reader-qualification-job.log` |
| Isolated build evidence | A builds in `/tmp/liquidity-qualification-xl9toki8/reference-target` from `reference-source/engine`; B builds in `/home/runner/work/liquidity-migration/liquidity-migration/engine/target` from the candidate checkout. Both use `cargo build --release --locked --workspace --bins --examples` with the same native target and inherited environment (`CARGO_INCREMENTAL=0`); the existing helper pins each `ENGINE_GIT_COMMIT`. The log separately shows both source paths compiling `engine-types`, `engine-core`, `engine-wal`, `engine-venue`, `engine-risk`, `engine-tools` and the other workspace dependencies. A/B builds take 5m46s/5m09s; candidate test compilation takes 17m11s. The reference source/target is removed after freezing its three binaries; no reference test rerun is claimed |
| Isolated workload evidence | All 31 release test summaries total 1,980 passed, zero failed, eight ignored. The account-state workload completes two million operations with 65,536 final IDs and three exact recovery runs at each of 0 / 1,000 / 10,000 / 100,000 history rows; middle/late median window costs are 188 / 188 ns per operation. Candidate tests, soak and three help smoke checks finish before the first measurement; no build/test command occurs during the fixed cells |
| Isolated cell evidence | Fixed A B B A B A A B, 10:59:38–11:02:18 UTC, unchanged 2,000-event / 100-Hz / every-20 / BTCUSDT recipe and fresh `bench-1.wal` through `bench-8.wal`. All 800 opportunities complete, with 100 dispatch barriers and six other sync barriers per cell, zero failures. Every raw histogram, count and individual verdict matches an independent parse; scratch summary `/tmp/r3-reader-qualification-independent-check.json`. All submit medians pass the absolute limit; decision cells 2B, 3B, 7A and 8B fail it. The raw tails remain in the full log; benchmark WALs are temporary and are not retained in a qualified archive |
| Isolated verdict | A median run-level decision p99 / submit p50 is 7,200 / 1,125,000 ns; B is 15,050 / 1,120,000 ns. B decision fails absolute 13,950 ns and relative 10,800 ns; B submit passes absolute 1,635,000 ns and relative 1,687,500 ns. Both failure lists are printed; the process exits 1 for the absolute failure. These are medians of four per-run metrics, not pooled percentiles. No qualified archive or upload occurs. The same-worker comparison identifies a candidate decision-latency regression under this recipe, without identifying its source-level cause or establishing a stable bound |
| Isolated image boundary | The six SHA256 values below are recorded after the pre-cell hash checks and repeated identically in the final latency receipt. The latency refusal occurs before post-benchmark hash verification and archive creation; no independent verification of packed bytes is possible for this failed run. These native qualification builds are distinct from deployment artifacts |

| Isolated image | Binary | Pre-cell SHA256 |
| --- | --- | --- |
| A | `engine` | `a141e6ef4f9ae734bb948e2001083d059d0f82237620ee6609764627a27689a5` |
| A | `engine-tools` | `3c91b5d51a84dcd96d7414cd1d90eceaece28117849973fa8d631c7c1123a778` |
| A | `signal-worker` | `b236e15a1e7dfc00a1a45785dbff1a7a19fd344407ca15a38c54bcc57afd870c` |
| B | `engine` | `e879f543f33ec281200a8528f4963a314bdd6b72609ab08ba4b4f329e83be5b8` |
| B | `engine-tools` | `de90c8d69fb01bebc3cf11925da6aae04494bee6e4808d8fabe7b4078e743fc1` |
| B | `signal-worker` | `2ce09ae1a1b2c7f50aaa926d14d0dcafa599846d01df62687c4e5082a5d92651` |

| Fresh `8c92c964` paired cell | Decision p50 / p99 / max µs | Submit p50 / p99 / max ms | Observed barrier p50 / p99 / max ms | Individual absolute decision budget |
| --- | --- | --- | --- | --- |
| 1 A | 4.7 / 5.9 / 11.1 | 1.09 / 1.37 / 1.4 | 0.6303 / 0.9283 / 0.9364 | Pass |
| 2 B | 5 / 14.6 / 14.9 | 1.1 / 1.39 / 2.21 | 0.6502 / 0.896 / 1.82 | Fail |
| 3 B | 4.9 / 16.1 / 16.1 | 1.11 / 1.76 / 2.02 | 0.6886 / 1.22 / 1.66 | Fail |
| 4 A | 4.8 / 5.7 / 12.5 | 1.12 / 1.32 / 1.41 | 0.6579 / 0.8428 / 0.9528 | Pass |
| 5 B | 5.2 / 11 / 11 | 1.17 / 2.49 / 2.52 | 0.6825 / 2.02 / 2.09 | Pass |
| 6 A | 4.7 / 8.5 / 12 | 1.13 / 2.8 / 10.69 | 0.6584 / 2.37 / 10.27 | Pass |
| 7 A | 5 / 14.8 / 15.6 | 1.13 / 1.63 / 1.65 | 0.6318 / 1.14 / 1.21 | Fail |
| 8 B | 5.3 / 15.5 / 24.3 | 1.13 / 2.34 / 7.34 | 0.6451 / 1.94 / 6.81 | Fail |

| Fresh `6de33fa3` paired cell | Decision p50 / p99 µs | Submit p50 / p99 ms | Barrier p50 / p99 ms | Individual absolute decision budget |
| --- | --- | --- | --- | --- |
| 1 A | 5.5 / 15.1 | 1.26 / 2.90 | 0.7107 / 2.39 | Fail |
| 2 B | 6.0 / 11.0 | 1.26 / 2.69 | 0.6988 / 2.15 | Pass |
| 3 B | 6.1 / 14.5 | 1.24 / 2.48 | 0.6761 / 1.89 | Fail |
| 4 A | 6.0 / 10.9 | 1.24 / 2.01 | 0.6904 / 1.42 | Pass |
| 5 B | 6.1 / 10.0 | 1.27 / 1.76 | 0.7036 / 1.27 | Pass |
| 6 A | 5.8 / 12.2 | 1.24 / 3.96 | 0.6917 / 3.38 | Pass |
| 7 A | 5.4 / 16.6 | 1.24 / 3.88 | 0.6807 / 3.29 | Fail |
| 8 B | 5.7 / 8.4 | 1.24 / 1.65 | 0.7036 / 1.12 | Pass |

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
