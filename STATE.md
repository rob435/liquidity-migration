# Operational state

## Purpose

Record the latest verified host snapshot and the source of each operational setting.

## Spec Tables

| Observation | Value |
| --- | --- |
| Verified at | 2026-09-07 13:56:11 UTC authenticated native positions/stops, services, heartbeats, disk and loaded images after final Round-3 deployment |
| Evidence | [Deploy run `34128449431`](https://github.com/rob435/liquidity-migration/actions/runs/34128449431); native read `/tmp/r3-final-deployed-native.json`; services/images `/tmp/r3-final-deployed-host.json`; workflow log `/tmp/r3-final-deploy-vps-job.log` |
| Host | `208.84.103.4` |
| Deployed commit | Completed generation, checkout and both loaded engine/worker images match the verified release archive for `70f4c55770d49f04ec9ac1f0d2ebd6a5d4118831`. Stored previous generation: `937ba60d4bcb6488d33ef5b115c638e817039882` |
| Funded permission | Mainnet remains armed; CARRY/LONG/EXODUS entry permissions remain enabled in both realms, with demo PROBE enabled |
| Runtime state | Engine PIDs demo `3003159` / mainnet `3004255`; worker PIDs `3003103` / `3004199`. All four services are active with heartbeats under six seconds, zero restarts/OOMs and no children. Both engines report `may_open=true`, `strategy_errors=[]` and zero stream resets. Each realm has seven positions and seven exact full-size native stops |
| Readiness boundary | Both workers report ready; all fourteen native stops match current position quantities exactly. All quantities and stop levels are unchanged from the 13:36:56 UTC predeploy read |
| Execution | Embedded callbacks, default Bybit binary; both engines use `Type=notify`, `WatchdogSec=30s`. Engine anonymous memory is 82.04 MiB demo / 262.44 MiB mainnet. Host Python contains only pip 24.0 and websocket-client 1.9.1 |
| Demo soak | All 31 workflow observations pass from 13:49:35.551 through 13:54:35.557 UTC, reaching 300 seconds before mainnet handover at 13:54:36.590; both native-state verification commands report already-complete. Deployment completes at 13:55:11.152 |
| Disk and WAL | 26.157 GiB free on `/var/lib`; tape reserves 25 GiB. All predeploy WAL inventory paths remain without shrinking; current inventory has 62 demo / 60 mainnet files, including the demo shadow-era file. No host WAL family is converted. The separate 13:00:03 UTC tape status is unblocked; its historical disk-drop count is 8,761 at that observation |
| Compatible retained release | All three compatible binaries remain at `/opt/liquidity-migration-engine/releases/8c92c96464bfe66662891c05f53a9510eefe8ba2/`; its archive remains under `staged/8c92c96464bfe66662891c05f53a9510eefe8ba2.tar.gz`. The 13:56:11 UTC read verifies all four hashes unchanged. This image retains every segment reader, Python import recovery and normalized legacy allocation readers. Retention does not establish automatic rollback across changed runtime inputs |
| Source qualification | Final source passes 1,981 developer Rust / 1,718 Python tests and 1,983 hosted Rust tests, zero failed and eight Rust tests ignored. Strict checks and all seven venue-feature jobs pass. [Release qualification `34128439094`](https://github.com/rob435/liquidity-migration/actions/runs/34128439094) passes 1,981 release tests, account/history workloads and all eight latency cells on the exact deployed SHA. Candidate median decision p99 / submit p50 is 2,000 / 721,150 ns, within absolute and paired limits. The separate Linux qualified archive verifies; its bytes differ from the loaded ordinary archive. Tail delays and all prior misses remain in [execution-performance.md](docs/execution-performance.md) |
| Copied-data qualification | The compatible Stage A reader passes all 27 original/converted base pairs / 54 boots, complete rotation state and ordered venue effects. Stage B passes all 27 converted-only boots, nine queued/nine prepared retrievals, both known filled archive queries and all 154 input length/mtime checks. Real source-frontier coverage is zero; future fills are absent and transport, risk and collateral are mocked. Original inputs remain unchanged |
| Local follow-up | Deployed R3-21 streams route inputs; R3-22 folds physical stop candidates; R3-23 excludes optional scalar admission from ordinary builds while preserving legacy recovery. Final Mac image `5d90e8f3` meets narrow decision 7.959 µs, wide decision p99 26.431 µs and narrow submit 4.882431 ms, with all 700 orders, one barrier and valid readbacks. The fixed control and every earlier miss remain recorded; the measurements do not establish a stable bound or source-only attribution. R3-13 acceptance is complete. R3-08 deletion is stopped because supported recovery and protection still depend on it. [Implementation checkpoint](docs/tier1-round-handoff.md), [plan](docs/tier1-audit-round-3.md), [all measurements](docs/execution-performance.md) |

| Release image | SHA256 |
| --- | --- |
| Final engine, loaded in both realms | `588ab752a3b00edf72e11c395365af137c9f64fcc80d37dd8cc48ad50083048f` |
| Final signal worker, loaded in both realms | `436f2fc1f667cab6858d2b563fbe40822ca794fda5a38cdf9009965160b0cc48` |
| Final engine tools, installed | `a8442eb157863170cad88d681e8b4b41b1136776500640d986e5451eb8a8406c` |
| Final downloaded release archive | `a90e8de80a901ac8601dbfea01739123067240b6049dab3b7552ff192bac4233` |
| Compatible Stage A engine, retained | `9fc63d5344c9190cb700cef151b6af8c1082eecb64ee347395c1c01302f01ae6` |
| Compatible Stage A signal worker, retained | `96fb34e84f174baa1acd931a35202f3d42009b3b8ac099e3916c4fabfa44caf8` |
| Compatible Stage A engine tools, retained | `1794e243a263af0b77ec7cbc9639e8fc6aa997562356609e72398015b7d2b1a4` |
| Compatible Stage A staged archive | `9765701173dabecb6258d216302bd58d80c420bbc81d53607e54a35d7c5bac2e` |

| Open long position | Demo quantity / native stop | Mainnet quantity / native stop |
| --- | --- | --- |
| LINKUSDT | 53.9 / 11.262 | 4.2 / 11.238 |
| TAOUSDT | 3.031 / 202.17 | 0.248 / 202.32 |
| ARBUSDT | 1788.1 / 0.13766 | 138.8 / 0.13767 |
| JUPUSDT | 1334 / 0.2021 | 103 / 0.2026 |
| BNBUSDT | 2.16 / 673.7 | 0.17 / 673.4 |
| LTCUSDT | 21.4 / 47.11 | 1.7 / 47.08 |
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
