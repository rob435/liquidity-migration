# Operational state

## Purpose

Record the latest verified host snapshot and the source of each operational setting.

## Spec Tables

| Observation | Value |
| --- | --- |
| Verified at | 2026-09-07 04:19:36 UTC authenticated native positions/stops, service state, heartbeats, disk and loaded images |
| Evidence | [Deploy run `34081612658`](https://github.com/rob435/liquidity-migration/actions/runs/34081612658); native read `/tmp/r3-native-post-fc2ad99c.json`; services and images `/tmp/r3-host-post-fc2ad99c.json`; workflow log `/tmp/r3-deploy-fc2ad99c-complete.log` |
| Host | `208.84.103.4`; 4 vCPU, 8 GiB RAM, 118 GB disk |
| Deployed commit | Completed generation, checkout and both loaded engine/worker pairs are `fc2ad99c64cfa6652739caccd9da2ced2dc37583`; loaded hashes match the verified default-Bybit deployment archive |
| Funded permission | Mainnet deployment preflight passes at 04:18:24 UTC; funded entry permissions remain enabled |
| Runtime state | Both engines and workers are active; workers report ready. Engine heartbeats are under five seconds old; both report `may_open=true`, `strategy_errors=[]` and zero stream resets. All four cgroups have zero OOM events and services have zero restarts. Authenticated reads match six positions to six exact full-size native stops in each realm |
| Execution | Both engines run embedded callbacks with no strategy children. Both engine units use `Type=notify`, `WatchdogSec=30s`; anonymous memory is 109.1 MiB demo / 71.2 MiB mainnet. The invalid filled-state repair remains deployed |
| Stored previous commit | `32858587f70755980332a3fefd048446980659d8`; the existing demo drill refuses this pair because runtime inputs differ. The completed 02:47–02:48 drill covers `a4189a48`/`32858587` only; it does not qualify this predecessor pair |
| Disk | 29.26 GiB free on `/var/lib`; watchdog minimum 25 GiB. Every pre-deploy WAL filename remains present and nonshrinking: 58 demo files; mainnet grows from 56 to 57 files. No retained reader is removed |
| Timer observation | The natural 04:15 demo probe records one source-to-submit sample of 19.84 ms including venue network; decision 137.1 µs and observed barrier 1.11 ms. One sample is not a latency distribution (`/tmp/r3-fc2ad99c-demo-probe-journal.log`) |
| Entry permissions | CARRY/LONG/EXODUS enabled in both realms, demo PROBE enabled; original permissions survive handover |
| Current implementation | Borrowed exact prices, reused order projections, fresh route membership bits, exact storage bit bounds and earlier venue-actor scheduling are deployed. Demo passes 300 seconds through 04:18:23 UTC before mainnet handover at 04:18:24; deploy completes 04:18:56. Host Python contains only pip 24.0 and websocket-client 1.9.1. [Implementation checkpoint](docs/tier1-round-handoff.md); [Round-3 plan](docs/tier1-audit-round-3.md) |
| Qualified local follow-up | The deployed source passes 1,962 developer Rust tests, 1,646 Python tests, 1,961 local release tests, repeated heavy crash simulations and copied-WAL restart/stop-repair fixtures. Hosted debug passes 1,964 tests and hosted release tests pass 1,962. Separate hosted latency qualification fails decision p99 at 9.3 µs versus 9.0 µs; submit p50 1.16 ms passes |

| Release image | SHA256 |
| --- | --- |
| Engine, loaded in both realms | `6d59459580fae52b0bc972009d55dbb16a4232642b12cb6844c0955719b0b95f` |
| Signal worker, loaded in both realms | `36cf9d8d3a3b9d831be90c29f9b435c59690413e7a1d0cc8cddead76575e69af` |
| Engine tools, installed companion | `a83a55a90865f3d26b5ae9c05b79356b84a0c411b4a8b8f739bb3806bd5810cb` |

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
