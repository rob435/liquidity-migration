# Operational state

## Purpose

Record the latest verified host snapshot and the source of each operational setting.

## Spec Tables

| Observation | Value |
| --- | --- |
| Verified at | 2026-09-07 06:00:14 UTC authenticated native positions/stops, service state, heartbeats, disk and loaded images after the selected-pair demo drill |
| Evidence | [Deploy run `34085705580`](https://github.com/rob435/liquidity-migration/actions/runs/34085705580); native read `/tmp/r3-native-four-cell-checkpoint.json`; services/images `/tmp/r3-host-four-cell-checkpoint.json`; workflow log `/tmp/r3-deploy-905c10d3-complete.log`; drill log `/tmp/r3-demo-selected-pair-run.log` |
| Host | `208.84.103.4`; 4 vCPU, 8 GiB RAM, 118 GB disk |
| Deployed commit | Completed generation and checkout `905c10d3de3cd2e913b626f0a49cd0e2001fb420`; demo loads its verified engine. Mainnet retains the `fc2ad99c` engine and PID because runtime inputs are identical. The worker image is identical in both archives |
| Funded permission | Mainnet remains armed and unchanged-left-running at 05:26:23 UTC; funded entry permissions remain enabled |
| Runtime state | Both engines and workers are active; workers report ready. Engine heartbeats are under five seconds old; both report `may_open=true`, `strategy_errors=[]` and zero stream resets. All four cgroups have zero OOM events and services have zero restarts. Authenticated reads match six positions to six exact full-size native stops in each realm |
| Execution | Both engines run embedded callbacks with no strategy children. Both engine units use `Type=notify`, `WatchdogSec=30s`; anonymous memory is 247.0 MiB demo / 72.0 MiB mainnet. The invalid filled-state repair remains deployed |
| Stored previous commit | `fc2ad99c64cfa6652739caccd9da2ced2dc37583`. The explicit demo drill selects older `32858587` and restores `905c10d3` from 05:29:01 to 05:31:12 UTC, verifying fresh account readiness and loaded images. Both generation markers and mainnet PIDs stay unchanged |
| Disk | 28.92 GiB free on `/var/lib`; watchdog minimum 25 GiB. All 58 demo and 57 mainnet WAL files survive deployment and the drill without shrinking. No retained reader is removed |
| Timer observation | The natural 04:15 demo probe records one source-to-submit sample of 19.84 ms including venue network; decision 137.1 µs and observed barrier 1.11 ms. One sample is not a latency distribution (`/tmp/r3-fc2ad99c-demo-probe-journal.log`) |
| Entry permissions | CARRY/LONG/EXODUS enabled in both realms, demo PROBE enabled; original permissions survive handover |
| Current implementation | Round-3 embedded/default-Bybit execution and the explicitly qualified demo-pair helper are deployed. Demo passes 300 seconds through 05:26:22 UTC; unchanged mainnet keeps running. Host Python contains only pip 24.0 and websocket-client 1.9.1. [Implementation checkpoint](docs/tier1-round-handoff.md); [Round-3 plan](docs/tier1-audit-round-3.md) |
| Qualified local follow-up | The deployed source passes 1,962 developer Rust tests, 1,661 Python tests, 1,961 local release tests, repeated heavy crash simulations and copied-WAL restart/stop-repair fixtures. Hosted debug passes 1,964 tests. Fresh qualification [34085706786](https://github.com/rob435/liquidity-migration/actions/runs/34085706786) passes 1,962 release tests and account workloads but fails decision p99 at 14.3 µs versus 13.95 µs; submit p50 1.16 ms passes. No qualified archive is uploaded |
| Pending qualification | Source `ecc3ea12` passes 1,962 local Rust and 1,673 Python tests; hosted debug passes 1,964 Rust tests. Fresh four-cell qualification `34088883848` passes 1,962 release tests and account workloads but fails median run-level decision p99 at 22.3 µs versus 13.95 µs. Submit's median passes at 1.055 ms; all 400 orders complete with one barrier each and zero failures. R3-06 stays open, with no qualified archive. The paired-source relative implementation passes 79 focused tests; fresh hosted qualification is pending. Absolute limits and failures stay visible. Latest unchanged-binary Mac narrow submit is 4.939775 ms after a 5.079039 ms miss; no consistent 5 ms bound is established |

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
