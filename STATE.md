# Operational state

## Purpose

Record the latest verified host snapshot and the source of each operational setting.

## Spec Tables

| Observation | Value |
| --- | --- |
| Verified at | 2026-09-07 02:49:28 UTC authenticated native positions/stops, service state, heartbeats, disk and loaded images |
| Evidence | [Deploy run `34076341887`](https://github.com/rob435/liquidity-migration/actions/runs/34076341887); native read `/tmp/r3-native-post-drill.json`; services and images `/tmp/r3-host-post-drill.json`; workflow log `/tmp/r3-deploy-32858587-complete.log`; rollback log `/tmp/r3-compatible-demo-drill.log` |
| Host | `208.84.103.4`; 4 vCPU, 8 GiB RAM, 118 GB disk |
| Deployed commit | Completed generation and checkout `32858587f70755980332a3fefd048446980659d8`; demo loads its release engine. Mainnet retains the `a4189a48` engine because runtime inputs are identical; the workflow leaves its PID and image unchanged |
| Funded permission | `REAL_MONEY=true`; `scripts/ops.sh status` reports armed at 02:13 UTC |
| Runtime state | Both engines and workers are active; workers report ready. Engine heartbeats are under five seconds old; both report `may_open=true`, `strategy_errors=[]` and zero stream resets. All four cgroups have zero OOM events and services have zero restarts. Authenticated reads match six positions to six exact full-size native stops in each realm |
| Execution | Both engines run embedded callbacks with no strategy children. Both engine units use `Type=notify`, `WatchdogSec=30s`; anonymous memory is 211.0 MiB demo / 107.1 MiB mainnet. The invalid filled-state repair remains deployed |
| Stored previous commit | `a4189a4897409e65acba7a2078b964986ceea928`; the sanctioned demo drill passes predecessor/current loaded-image and readiness checks at 02:47:01 / 02:48:03 UTC. Mainnet PIDs stay unchanged; configuration and durable state are retained |
| Disk | 26.51 GiB free on `/var/lib`; watchdog minimum 25 GiB. A 30-second demo soak sample measures 28,537 WAL bytes/s, zero engine errors and no process change; this is a workload sample, not a capacity guarantee. |
| Timer observation | The natural 02:15 embedded demo probe `eng-1788746775000-1` records decision-to-cancel dispatch 2000.955 ms and cancel dispatch-to-Cancelled 12.074 ms; no fill appears in the pinned segment. Its one-order window measures source-to-submit 12.59 ms including the venue network. One sample is not a latency distribution; the Rust WAL reader validates segment 000056 (`/tmp/r3-embedded-probe-wal-validation.log`) |
| Entry permissions | CARRY/LONG/EXODUS enabled in both realms, demo PROBE enabled; original permissions survive handover |
| Current implementation | Round-3 embedded/default-Bybit execution is deployed. The first embedded handover passes 300 demo seconds before mainnet at 02:11:42 UTC; generation `32858587` passes another 300 seconds through 02:43:42 UTC, then leaves the unchanged mainnet runtime running. Host Python contains only pip 24.0 and websocket-client 1.9.1. [Implementation checkpoint](docs/tier1-round-handoff.md); [Round-3 plan](docs/tier1-audit-round-3.md) |
| Qualified local follow-up | Borrowed exact prices, reused order projections, fresh route membership bits, exact storage bit bounds and earlier venue-actor scheduling pass 1,962 developer Rust tests, 1,646 Python tests, 1,961 release tests, repeated heavy crash simulations and copied-WAL restart/stop-repair fixtures. This source is pending workflow deployment; it does not change the host observation above |

| Release image | SHA256 |
| --- | --- |
| Engine, mainnet retained from `a4189a48` | `6d8a0765aadae6ecf2bfe23825108c36b2dfe81c9b5f34a9c516827abc7bdd87` |
| Engine, demo and installed companion generation `32858587` | `beccaa74534db544ba57d091bd3420a6537d812d7487bf89dc152cf444fb851f` |
| Signal worker, loaded in both realms | `e77bb2b88ac32fbac1b4f61574c2eabb41a818f8ab33e92ed1c0a155394e0e5e` |
| Engine tools, installed companion | `1ecdb1bdd2754813fa51d703d9ed9b65fee445c3f150ce0b1034c152d477c1a9` |

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
