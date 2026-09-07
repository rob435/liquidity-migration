# Operational state

## Purpose

Record the latest verified host snapshot and the source of each operational setting.

## Spec Tables

| Observation | Value |
| --- | --- |
| Verified at | 2026-09-07 11:14:38 UTC authenticated native positions/stops, services, heartbeats, disk and loaded images after reader deployment |
| Evidence | [Deploy run `34114063829`](https://github.com/rob435/liquidity-migration/actions/runs/34114063829); native read `/tmp/r3-reader-deployed-native.json`; services/images `/tmp/r3-reader-deployed-host.json`; workflow log `/tmp/r3-reader-deploy-vps-job.log` |
| Host | `208.84.103.4` |
| Deployed commit | Completed generation, checkout and both loaded engine/worker images match `8c92c96464bfe66662891c05f53a9510eefe8ba2`. Stored previous generation: `905c10d3de3cd2e913b626f0a49cd0e2001fb420` |
| Funded permission | Mainnet remains armed; CARRY/LONG/EXODUS entry permissions remain enabled in both realms, with demo PROBE enabled |
| Runtime state | Engine PIDs demo `2974055` / mainnet `2975547`; worker PIDs `2973992` / `2975491`. All four services are active with heartbeats under five seconds, zero restarts/OOMs and no children. Both engines report `may_open=true`, `strategy_errors=[]` and zero stream resets. Each realm has seven positions and seven exact full-size native stops |
| Readiness boundary | Demo worker reports ready at 11:14:38. Mainnet worker reports ready at 11:18:06 on the same PID, connected at public-stream epoch 2 with complete ticker coverage, zero stream faults, zero queued frames and no backpressure; its initial post-handover snapshot is recovering |
| Execution | Embedded callbacks, default Bybit binary; both engines use `Type=notify`, `WatchdogSec=30s`. Engine anonymous memory is 296.9 MiB demo / 249.8 MiB mainnet. Host Python contains only pip 24.0 and websocket-client 1.9.1 |
| Demo soak | All 31 workflow observations pass from 11:08:42 through 11:13:42 UTC, reaching 300 seconds before mainnet handover; both native-state verification commands report already-complete |
| Disk and WAL | 29.67 GiB free on `/var/lib`; tape reserves 25 GiB. All predeploy WAL inventory paths remain without shrinking; current inventory has 61 demo / 59 mainnet files, including the demo shadow-era file. No host WAL family is converted |
| Compatible retained release | All three binaries remain at `/opt/liquidity-migration-engine/releases/8c92c96464bfe66662891c05f53a9510eefe8ba2/`; staged archive remains at `/opt/liquidity-migration-engine/staged/8c92c96464bfe66662891c05f53a9510eefe8ba2.tar.gz`. Their hashes match the downloaded normal release archive. This image retains every segment reader, Python import recovery and normalized legacy allocation readers. Retention does not establish automatic rollback across changed runtime inputs |
| Source qualification | The deployed reader passes 1,980 developer Rust / 1,694 Python tests, 1,982 hosted Rust tests and 1,980 release tests, eight ignored. [Independent release qualification](https://github.com/rob435/liquidity-migration/actions/runs/34111799713) passes account workloads and all 800 order completions, but decision p99 median 15.05 µs exceeds absolute 13.95 µs and paired 10.8 µs; submit p50 1.12 ms passes. No qualified archive is published. Build isolation and budget enforcement criteria are met; final latency acceptance remains open |
| Copied-data qualification | The compatible reader passes all 27 final original/converted base pairs / 54 boots, complete rotation state and ordered venue effects, nine queued/nine prepared callback retrievals, and two filled archived-order queries. Real source-frontier coverage is zero; transport, risk and collateral are mocked. Original inputs remain unchanged |
| Local follow-up | Normalized runtime writes, exact sim/backtest metadata, Python-import removal, ordinary v2–v6 reader removal and stop-path simplification are implemented locally. Reader/converter/core focused checks pass 120 tests; stop-path checks pass 37 including 34 equivalence cases. The current release image measures narrow decision 6.083 µs (pass), submit 5.406719 ms (miss), wide decision p99 21.167 µs (pass), all 700 orders/one barrier each. All 27 converted-only candidate boots pass, with nine queued/nine prepared retrievals, both archived fills and unchanged inputs; final integrated qualification/publication remain pending. [Implementation checkpoint](docs/tier1-round-handoff.md), [plan](docs/tier1-audit-round-3.md) and [all measurements](docs/execution-performance.md) |

| Release image | SHA256 |
| --- | --- |
| Engine, loaded in both realms and retained | `9fc63d5344c9190cb700cef151b6af8c1082eecb64ee347395c1c01302f01ae6` |
| Signal worker, loaded in both realms and retained | `96fb34e84f174baa1acd931a35202f3d42009b3b8ac099e3916c4fabfa44caf8` |
| Engine tools, installed and retained | `1794e243a263af0b77ec7cbc9639e8fc6aa997562356609e72398015b7d2b1a4` |
| Staged archive | `9765701173dabecb6258d216302bd58d80c420bbc81d53607e54a35d7c5bac2e` |

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
