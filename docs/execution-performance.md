# Execution measurements

## Purpose

State the measured execution workloads, timing boundaries and resource limits of the integrated Round-2 engine.

## Spec Tables

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
| Durability decision | Retain callback commit, queued dispatch and attempted-send barriers; each owns a distinct recovery obligation |
| Scope limit | One active child over 270 symbols is not 270 workers; a 60-second no-fill run does not establish a universal resource ceiling, production-day accounting or a latency SLO |

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
