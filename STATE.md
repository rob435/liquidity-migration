# Operational state

## Purpose

Record the latest verified host snapshot and the source of each operational setting.

## Spec Tables

| Observation | Value |
| --- | --- |
| Verified at | 2026-09-07 01:42:53 UTC authenticated native positions/stops, service state, heartbeats, disk and loaded images |
| Evidence | [Deploy run `34055716541`](https://github.com/rob435/liquidity-migration/actions/runs/34055716541); private native read `/tmp/r3-prepush-native-protection.txt`; service, heartbeat, disk and loaded images `/tmp/r3-prepush-host-state.txt`; corrective LONG recovery `/tmp/r3-step0-long.txt` |
| Host | `208.84.103.4`; 4 vCPU, 8 GiB RAM, 118 GB disk |
| Deployed commit | `bb4bc3d32f99dfa81152386627deb24764873343`; current loaded engine and worker images match the release artifact; companion and child images were verified at handover |
| Funded permission | `REAL_MONEY=true`; verified by `scripts/ops.sh status` at 20:00 UTC |
| Runtime state | Both engines and workers active with zero service restarts. Engine heartbeats are under five seconds old; both report `may_open=true`, `strategy_errors=[]` and zero stream resets. Authenticated reads match six open positions to six exact full-size native stops in each realm |
| Worker lifetime | Both LONG children remain alive after handover; the invalid filled-state repair is deployed. The sanctioned handover clears both historical reconciliation latches after authenticated agreement |
| Stored previous commit | `af09aab53fc13cf53f66c393931c0ecccd765598`; rollback remains subject to runtime-input and WAL compatibility, with forward repair across incompatible state |
| Disk | 26.21 GiB free on `/var/lib`; watchdog minimum 25 GiB. Measured WAL growth is workload-dependent and is not a steady-state capacity guarantee. |
| Timer observation | The natural 20:00 demo probe records PULL 1.041 ms after its deadline, cancel dispatch 9.557 ms later and cancellation 23.936 ms after dispatch. Captured timer/cancel links are unambiguous; the FIRE preparation clock is absent from the focused read, so total resting time is not measured (`/tmp/r3-step0-probe-analysis.txt`) |
| Entry permissions | CARRY/LONG/EXODUS enabled in both realms, demo PROBE enabled; original permissions survive handover |
| Current implementation | Round-3 changes remain local and undeployed; the host observation above applies to `bb4bc3d3`. [Implementation checkpoint](docs/tier1-round-handoff.md); [Round-3 plan](docs/tier1-audit-round-3.md) |

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
