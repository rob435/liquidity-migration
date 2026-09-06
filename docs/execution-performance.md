# Execution measurements

## Purpose

State the measured execution workloads, timing boundaries and resource limits of the integrated Round-2 engine.

## Spec Tables

| Field | Scope |
| --- | --- |
| Evidence boundary | 2026-09-06; Rust 1.90.0 release on Apple M4, 10 CPUs, 16 GiB RAM, macOS 15.7.2 |
| Execution | Real engine loop, one registered child strategy and `engine-risk`; synthetic market and venue |
| Source identity | Final combined source in [qualification evidence](tier1-round2-evidence.json); executable hashes below |
| Input | Total rate across symbols; every twentieth global source sequence is an order opportunity |
| Outstanding orders | Synthetic accepts do not fill; growing outstanding-order snapshots increase CPU and WAL volume |
| Clock | Current-process source-to-decision and source-to-submit measurements include child execution and durability; prior-process source stamps do not enter new samples |
| Quantiles | Individual HDR samples; fewer than 1,000 samples place p99.9 at the observed maximum |
| Barriers | WAL request-to-observed-confirmation, including the benchmark observer thread/channel; not pure filesystem sync time |
| Resources | Parent, child and time wrapper sampled approximately every 50 ms; short peaks and final CPU increments can be missed |
| Disk | Complete final WAL with rotation disabled, including boot and report; not a steady-state byte rate or storage quota |
| Interference | No compiler processes observed during these cells; other desktop work is not excluded |
| Private evidence | `/tmp/tier1-round2-integration/measurements-final/summary.json`; raw logs, WALs and hashes are retained in the deployment evidence archive |

| Executable | SHA256 |
| --- | --- |
| `engine` | `77d6d9abb7ebd2b14c41a6c7b1ca0083004e7819374995580dd9b60fa48880f8` |
| `engine-tools` | `4cf389eb7d253be1247b0a411b12ff6098660ba5e049d4ff25d2875e31b18c31` |

Latency cells are milliseconds, `p50 / p99 / p99.9`; MiB means 1,048,576 bytes.

| Symbols / quotes / total rate / venue delay | Submits / opportunities | Source → decision | Source → submit result | Sampled tree / child MiB | Final WAL MiB |
| --- | --- | --- | --- | --- | --- |
| 1 / 6,000 / 100 Hz / 0 ms | 299 / 300 | 0.612 / 1.153 / 1.204 | 14.123 / 18.514 / 21.955 | 24.20 / 6.30 | 16.21 |
| 270 / 12,000 / 200 Hz / 0 ms | 580 / 600 | 3.191 / 205.259 / 230.556 | 21.234 / 225.706 / 251.396 | 64.56 / 29.39 | 294.80 |
| 270 / 12,000 / 200 Hz / 20 ms | 576 / 600 | 3.584 / 222.036 / 301.990 | 46.170 / 266.076 / 344.982 | 66.53 / 30.12 | 292.40 |

| Cell | Callback / queue / attempt barrier median ms | Barrier confirmations / failures | Sampled CPU seconds | Outer elapsed seconds |
| --- | --- | --- | --- | --- |
| single | 3.415 / 3.194 / 3.648 | 1803 / 0 | 4.96 | 60.333 |
| wide | 3.601 / 3.155 / 3.836 | 3489 / 0 | 46.66 | 60.068 |
| wide-delayed | 3.851 / 3.643 / 3.832 | 3465 / 0 | 46.88 | 60.150 |

| Outcome | Observation |
| --- | --- |
| Admission and completion | Each durable intent reaches a completed submit; no risk refusals, callback faults or barrier failures occur in these cells |
| Missing source opportunities | 1 / 20 / 24 opportunities have no durable intent; WAL records do not separate coalescing from shutdown for this residual |
| Readback | All three final WALs validate through the rebuilt `engine latency` command |
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
