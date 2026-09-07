# Operational state

## Purpose

Record the latest verified host snapshot and the source of each operational setting.

## Spec Tables

| Observation | Value |
| --- | --- |
| Verified at | 2026-09-07 02:13:27–02:13:54 UTC authenticated native positions/stops, service state, heartbeats, disk and loaded images |
| Evidence | [Deploy run `34074541111`](https://github.com/rob435/liquidity-migration/actions/runs/34074541111); native read `/tmp/r3-native-after-handover.json`; services and images `/tmp/r3-host-postdeploy.json`; status `/tmp/r3-postdeploy-status.txt`; workflow log `/tmp/r3-deploy-vps-complete.log` |
| Host | `208.84.103.4`; 4 vCPU, 8 GiB RAM, 118 GB disk |
| Deployed commit | `a4189a4897409e65acba7a2078b964986ceea928`; loaded engine and worker images match the default-feature release artifact; installed companion checksum matches |
| Funded permission | `REAL_MONEY=true`; `scripts/ops.sh status` reports armed at 02:13 UTC |
| Runtime state | Both engines and workers are active; workers report ready. Engine heartbeats are under five seconds old; both report `may_open=true`, `strategy_errors=[]` and zero stream resets. All four cgroups have zero OOM events and services have zero restarts. Authenticated reads match six positions to six exact full-size native stops in each realm |
| Execution | Both engines run embedded callbacks with no strategy children. Both engine units use `Type=notify`, `WatchdogSec=30s`; anonymous memory is 134.8 MiB demo / 105.2 MiB mainnet. The invalid filled-state repair remains deployed |
| Stored previous commit | `bb4bc3d32f99dfa81152386627deb24764873343`; its runtime inputs differ, so a compatible demo rollback drill remains unexercised; repair forward across incompatible state |
| Disk | 30.43 GiB free on `/var/lib`; watchdog minimum 25 GiB. A 30-second demo soak sample measures 28,537 WAL bytes/s, zero engine errors and no process change; this is a workload sample, not a capacity guarantee. |
| Timer observation | The natural 20:00 demo probe records PULL 1.041 ms after its deadline, cancel dispatch 9.557 ms later and cancellation 23.936 ms after dispatch. Captured timer/cancel links are unambiguous; the FIRE preparation clock is absent from the focused read, so total resting time is not measured (`/tmp/r3-step0-probe-analysis.txt`) |
| Entry permissions | CARRY/LONG/EXODUS enabled in both realms, demo PROBE enabled; original permissions survive handover |
| Current implementation | Round-3 embedded/default-Bybit execution is deployed. Demo passes 300 seconds of resource checks from 02:06:41 to 02:11:41 UTC before mainnet handover at 02:11:42 UTC. Host Python contains only pip 24.0 and websocket-client 1.9.1. [Implementation checkpoint](docs/tier1-round-handoff.md); [Round-3 plan](docs/tier1-audit-round-3.md) |

| Release image | SHA256 |
| --- | --- |
| Engine, loaded in both realms | `6d8a0765aadae6ecf2bfe23825108c36b2dfe81c9b5f34a9c516827abc7bdd87` |
| Signal worker, loaded in both realms | `e77bb2b88ac32fbac1b4f61574c2eabb41a818f8ab33e92ed1c0a155394e0e5e` |
| Engine tools, installed companion | `4f1db0a5e9cbe719ad0a534b93914f111853c36ec1e31f5d3fa6d765792e2f4d` |

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
