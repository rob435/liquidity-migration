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
| Private evidence | `/tmp/tier1-round2-integration/measurements-timer-fix/summary.json`; raw logs, WALs and hashes are retained beside this receipt |

| Executable | SHA256 |
| --- | --- |
| `engine` | `9eba0d53d0c7dbc0c23cb46f93f86aa4abcffcb303fe4749b930aa519fb0247f` |
| `engine-tools` | `81fc876b17e85a5c355d5ae5ef31d1d8bf1782e639c66510fb3ceefb975f9b1f` |

Latency cells are milliseconds, `p50 / p99 / p99.9`; MiB means 1,048,576 bytes.

| Symbols / quotes / total rate / venue delay | Submits / opportunities | Source → decision | Source → submit result | Sampled tree / child MiB | Final WAL MiB |
| --- | --- | --- | --- | --- | --- |
| 1 / 6,000 / 100 Hz / 0 ms | 299 / 300 | 0.665 / 1.241 / 1.434 | 14.377 / 20.136 / 21.660 | 24.67 / 6.53 | 16.21 |
| 270 / 12,000 / 200 Hz / 0 ms | 581 / 600 | 3.158 / 201.064 / 241.566 | 20.087 / 218.628 / 261.489 | 68.48 / 30.47 | 295.40 |
| 270 / 12,000 / 200 Hz / 20 ms | 581 / 600 | 3.342 / 200.147 / 228.590 | 42.992 / 242.745 / 272.892 | 66.41 / 31.58 | 295.41 |

| Cell | Callback / queue / attempt barrier median ms | Barrier confirmations / failures | Sampled CPU seconds | Outer elapsed seconds |
| --- | --- | --- | --- | --- |
| single | 3.684 / 3.505 / 3.681 | 1803 / 0 | 5.04 | 60.319 |
| wide | 3.631 / 3.360 / 3.383 | 3495 / 0 | 46.52 | 60.101 |
| wide-delayed | 3.758 / 3.582 / 3.212 | 3495 / 0 | 46.64 | 60.149 |

| Outcome | Observation |
| --- | --- |
| Admission and completion | Each durable intent reaches a completed submit; no risk refusals, callback faults or barrier failures occur in these cells |
| Missing source opportunities | 1 / 19 / 19 opportunities have no durable intent; WAL records do not separate coalescing from shutdown for this residual |
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
