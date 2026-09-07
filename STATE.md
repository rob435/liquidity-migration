# Operational state

## Purpose

Record the latest verified host snapshot and the source of each operational setting.

## Spec Tables

| Observation | Value |
| --- | --- |
| Verified at | 2026-09-07 10:09:00 UTC authenticated native positions/stops, services, heartbeats, disk and loaded images during the reader-stage build |
| Evidence | [Deploy run `34085705580`](https://github.com/rob435/liquidity-migration/actions/runs/34085705580); native read `/tmp/r3-native-reader-build.json`; services/images `/tmp/r3-host-reader-build.json`; workflow log `/tmp/r3-deploy-905c10d3-complete.log`; drill log `/tmp/r3-demo-selected-pair-run.log` |
| Host | `208.84.103.4`; 4 vCPU, 8 GiB RAM, 118 GB disk |
| Deployed commit | Completed generation and checkout `905c10d3de3cd2e913b626f0a49cd0e2001fb420`; demo loads its verified engine. Mainnet retains the `fc2ad99c` engine and PID because runtime inputs are identical. The worker image is identical in both archives |
| Funded permission | Mainnet remains armed and unchanged-left-running at 05:26:23 UTC; funded entry permissions remain enabled |
| Runtime state | Both engines and workers are active on unchanged PIDs/images; workers report ready. All heartbeats are under five seconds old; both engines report `may_open=true`, `strategy_errors=[]` and zero stream resets. All four cgroups have zero OOM events and services have zero restarts. Authenticated reads match seven positions to seven exact full-size native stops in each realm |
| Readiness boundary | The 07:30:20 snapshot captures demo worker `recovering` after instrument refresh and public-stream epoch 2. Its 07:32:49 heartbeat and settled 07:33:18 read report `ready`, connected, complete coverage, zero stream faults and no restart. `/tmp/r3-demo-worker-final-recovery.log` retains the transition; readiness is not claimed continuously across the earlier sample |
| Execution | Both engines run embedded callbacks with no strategy children. Both engine units use `Type=notify`, `WatchdogSec=30s`; anonymous memory is 252.4 MiB demo / 80.5 MiB mainnet. The invalid filled-state repair remains deployed |
| Stored previous commit | `fc2ad99c64cfa6652739caccd9da2ced2dc37583`. The explicit demo drill selects older `32858587` and restores `905c10d3` from 05:29:01 to 05:31:12 UTC, verifying fresh account readiness and loaded images. Both generation markers and mainnet PIDs stay unchanged |
| Disk | 30.51 GiB free on `/var/lib`; research tape reserves 25 GiB and the host liveness alarm is 5 GB. All 60 demo and 59 mainnet files matched by the WAL inventory remain present without shrinking since 08:24; the demo inventory includes its shadow-era file. No retained reader is removed |
| Timer observation | The natural 04:15 demo probe records one source-to-submit sample of 19.84 ms including venue network; decision 137.1 µs and observed barrier 1.11 ms. One sample is not a latency distribution (`/tmp/r3-fc2ad99c-demo-probe-journal.log`) |
| Entry permissions | CARRY/LONG/EXODUS enabled in both realms, demo PROBE enabled; original permissions survive handover |
| Current implementation | Round-3 embedded/default-Bybit execution and the explicitly qualified demo-pair helper are deployed. Demo passes 300 seconds through 05:26:22 UTC; unchanged mainnet keeps running. Host Python contains only pip 24.0 and websocket-client 1.9.1. [Implementation checkpoint](docs/tier1-round-handoff.md); [Round-3 plan](docs/tier1-audit-round-3.md) |
| Qualified local follow-up | The deployed source passes 1,962 developer Rust tests, 1,661 Python tests, 1,961 local release tests, repeated heavy crash simulations and copied-WAL restart/stop-repair fixtures. Hosted debug passes 1,964 tests. Fresh qualification [34085706786](https://github.com/rob435/liquidity-migration/actions/runs/34085706786) passes 1,962 release tests and account workloads but fails decision p99 at 14.3 µs versus 13.95 µs; submit p50 1.16 ms passes. No qualified archive is uploaded |
| Source qualification | `6de33fa3` passes the developer gate (1,962 Rust / 1,691 Python), normal hosted checks (1,964 Rust / 1,691 Python) and release qualification [34093133061](https://github.com/rob435/liquidity-migration/actions/runs/34093133061) (1,962 Rust plus account workloads). Candidate medians 10.5 µs decision p99 / 1.25 ms submit p50 pass relative and absolute limits; its archive verifies. Its helper enforces only relative acceptance. The local R3-06 correction requires both, passes 81 qualifier tests plus three doc-link tests, and accepts the recorded eight-cell log; corrected-source qualification fails before benchmarks because the shared Cargo target reuses the reference WAL library; R3-17 isolates builds. [Execution measurements](docs/execution-performance.md) retain every cell and prior failure |
| Local converter follow-up | `wal-convert-v5` writes a separate copied family and retains existing lot semantics and source records; eight fixtures, 19 WAL unit tests, both CLI entry points and scoped strict Clippy pass. Copied quarantine families convert 14 demo and 13 mainnet bases; independent full CRC/hash/state checks and all 27 affected segment fills comparisons pass. The same converter image passes fixed-control narrow decision p50 7.751 / 8.631 µs and submit p50 4.718591 / 4.968447 ms; preceding wide decision p99 is 23.711 µs. Earlier misses remain recorded. Commit `46bbb346` passes the mandatory developer gate (1,971 Rust / 1,693 Python). Corrected-source qualification [34104340078](https://github.com/rob435/liquidity-migration/actions/runs/34104340078) fails candidate compilation by reusing the baseline WAL library; normal hosted CI passes 1,973 Rust / 1,693 Python tests. No converter code, reader removal or converted WAL is deployed |
| Reader-stage follow-up | Local source includes independent qualification build targets, paged callback assembly, legacy FIFO allocation readers/economics and engine-clock portfolio retries. Runtime normalized receipt writes and exact sim/backtest metadata remain held for the next stage. The release reader accepts a generated two-record receipt fixture that the old reader rejects, and its full writer-WAL fills report equals the frozen writer's report exactly. Final-source copied boots are running. Mac narrow decision 15.671 µs misses 10 µs; narrow submit 4.968447 ms and wide decision p99 25.919 µs pass, with all 700 orders/one barrier each and valid readbacks. Developer gate and hosted qualification remain pending |

| Release image | SHA256 |
| --- | --- |
| Engine, demo / installed `905c10d3` | `1d2eecb8c16a2792a655d1901654163f2d60f43e414b7bb25a34f725a5e6bfc7` |
| Engine, mainnet retained `fc2ad99c` | `6d59459580fae52b0bc972009d55dbb16a4232642b12cb6844c0955719b0b95f` |
| Signal worker, loaded in both realms | `36cf9d8d3a3b9d831be90c29f9b435c59690413e7a1d0cc8cddead76575e69af` |
| Engine tools, installed companion | `a7a695f09046fce2bfc96c1a2d9af3936ee0599fe97cbd8ae88b6f3e455ede2f` |

| Open long position | Demo quantity / native stop | Mainnet quantity / native stop |
| --- | --- | --- |
| LINKUSDT | 53.9 / 11.262 | 4.2 / 11.238 |
| TAOUSDT | 3.031 / 202.17 | 0.248 / 202.32 |
| ARBUSDT | 1788.1 / 0.13766 | 138.8 / 0.13767 |
| JUPUSDT | 1334 / 0.2021 | 103 / 0.2026 |
| BNBUSDT | 2.16 / 673.7 | 0.17 / 673.4 |
| LTCUSDT | 22.5 / 47.11 | 1.7 / 47.08 |
| WLDUSDT | 944.8 / 0.3521 | 73.3 / 0.3531 |

| Sleeve ID | Mainnet | Demo | Repository authority |
| --- | --- | --- | --- |
| 0 | CARRY | CARRY | `configs/lane2_carry_hold_v7.json` |
| 1 | LONG | LONG | `configs/long_native_v12.json` |
| 2 | EXODUS | EXODUS | `configs/lane2_exodus_short_v1.json` |
| 3 | MAKER, quoting disabled | PROBE, enabled | `deploy/engine.mainnet.toml.template`, `deploy/engine.demo.toml.template` |

| Setting | Repository authority |
| --- | --- |
| Capital, leverage, exposure and rolling loss | `configs/operational.json` |
| Native configuration | Rust `render-native-config`; generated template regions |
| Account identity and permission | Root-owned realm env/config on the host; authenticated account reader |
| Signal delivery | Durable spool plus `stream.sock` notification; worker owns unpublished/retained prefixes |
| Units and activation | `deploy/fleet_manifest.tsv`; [systemd inventory](deploy/systemd/README.md) |
| Equity and telemetry | Equity-recorder timer, one-minute sampling; [observability](docs/observability.md) |
| History | [CHANGELOG.md](CHANGELOG.md); dated archived entries retain prior incidents and deployments |

## Invariants

- Must distinguish a dated host observation from local source and test results.
- Must replace this snapshot after verifying an actual deployment; commit success alone does not update it.
- Must preserve append-only sleeve IDs, exact ownership and native protection.
- Must verify WAL/worker compatibility before selecting a previous binary.

## Operational Recipes

```sh
scripts/ops.sh status
scripts/ops.sh --help
```

[Operations](docs/operations.md) · [Engine](docs/engine.md) · [Data](docs/data.md) · [Trading rules](docs/trading_logic.md)
