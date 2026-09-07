# Operational state

## Purpose

Record the latest verified host snapshot and the source of each operational setting.

## Spec Tables

| Observation | Value |
| --- | --- |
| Verified at | 2026-09-07 12:58:53 UTC authenticated native positions/stops, services, heartbeats, disk and loaded images after Stage B deployment |
| Evidence | [Deploy run `34119441979`](https://github.com/rob435/liquidity-migration/actions/runs/34119441979); native read `/tmp/r3-stage-b-followup-native.json`; services/images `/tmp/r3-stage-b-followup-host.json`; workflow log `/tmp/r3-stage-b-deploy-vps-job.log` |
| Host | `208.84.103.4` |
| Deployed commit | Completed generation, checkout and both loaded engine/worker images match `937ba60d4bcb6488d33ef5b115c638e817039882`. Stored previous generation: `8c92c96464bfe66662891c05f53a9510eefe8ba2` |
| Funded permission | Mainnet remains armed; CARRY/LONG/EXODUS entry permissions remain enabled in both realms, with demo PROBE enabled |
| Runtime state | Engine PIDs demo `2987784` / mainnet `2988855`; worker PIDs `2987727` / `2988799`. All four services are active with heartbeats under four seconds, zero restarts/OOMs and no children. Both engines report `may_open=true`, `strategy_errors=[]` and zero stream resets. Each realm has seven positions and seven exact full-size native stops |
| Readiness boundary | Both workers report ready in the authenticated postdeploy snapshot; all fourteen native stops match current position quantities exactly. Demo LTC is 21.4 versus 22.5 at 12:18:33 UTC, with unchanged 47.11 stop; other position quantities and all stop levels remain unchanged |
| Execution | Embedded callbacks, default Bybit binary; both engines use `Type=notify`, `WatchdogSec=30s`. Engine anonymous memory is 160.87 MiB demo / 118.50 MiB mainnet. Host Python contains only pip 24.0 and websocket-client 1.9.1 |
| Demo soak | All 31 workflow observations pass from 12:10:56 through 12:15:56 UTC, reaching 300 seconds before mainnet handover at 12:15:57; both native-state verification commands report already-complete. Deployment completes at 12:16:29 |
| Disk and WAL | 25.896 GiB free on `/var/lib`; tape reserves 25 GiB. All predeploy WAL inventory paths remain without shrinking; current inventory has 61 demo / 60 mainnet files, including the demo shadow-era file. No host WAL family is converted. The separate 13:00:03 UTC tape status is unblocked; its historical disk-drop count remains 8,761 |
| Compatible retained release | All three compatible binaries remain at `/opt/liquidity-migration-engine/releases/8c92c96464bfe66662891c05f53a9510eefe8ba2/`; its archive remains under `staged/8c92c96464bfe66662891c05f53a9510eefe8ba2.tar.gz`. The 12:19:00 UTC read verifies all four hashes unchanged. This image retains every segment reader, Python import recovery and normalized legacy allocation readers. Retention does not establish automatic rollback across changed runtime inputs |
| Source qualification | Stage B passes 1,974 developer Rust / 1,718 Python tests and 1,976 hosted Rust tests, zero failed and eight ignored. Strict checks pass. [Release qualification `34119432164`](https://github.com/rob435/liquidity-migration/actions/runs/34119432164) passes 1,974 release tests, account/history workloads and all eight latency cells against this exact SHA. Candidate median decision p99 / submit p50 is 5,950 / 1,115,000 ns, passing absolute and same-worker relative limits. The separate Linux qualified archive verifies its packed binaries and embedded log; these are distinct from the loaded deployment bytes. The prior reader qualification fails decision latency; its complete cells remain in [execution-performance.md](docs/execution-performance.md) |
| Copied-data qualification | The compatible Stage A reader passes all 27 original/converted base pairs / 54 boots, complete rotation state and ordered venue effects. Stage B passes all 27 converted-only boots, nine queued/nine prepared retrievals, both known filled archive queries and all 154 input length/mtime checks. Real source-frontier coverage is zero; future fills are absent and transport, risk and collateral are mocked. Original inputs remain unchanged |
| Local follow-up | R3-21 streams route inputs; R3-22 folds physical stop candidates; R3-23 excludes optional scalar admission from ordinary builds while preserving legacy recovery. Focused tests and strict core Clippy pass. Final frozen `5d90e8f3` meets narrow decision 7.959 µs, wide decision p99 26.431 µs and narrow submit 4.882431 ms, with all 700 orders, one barrier and valid readbacks. The fixed control and every earlier miss remain recorded; only rebuildable local release cache is cleared. These changes are not yet deployed or through the final developer/hosted gate. R3-08 remains conditional. [Implementation checkpoint](docs/tier1-round-handoff.md), [plan](docs/tier1-audit-round-3.md), [all measurements](docs/execution-performance.md) |

| Release image | SHA256 |
| --- | --- |
| Stage B engine, loaded in both realms | `c628090dc5b5a4625d8cd8b6827a2cba03b13e3caaefff3d86006b9baafc0b21` |
| Stage B signal worker, loaded in both realms | `436f2fc1f667cab6858d2b563fbe40822ca794fda5a38cdf9009965160b0cc48` |
| Stage B engine tools, installed | `de28527b4f6a9d59d4db715d2619e26b3ae95236e6b1d37d1b069c8498f5b207` |
| Stage B downloaded release archive | `deb5a292fac2e1115c054b73a4f2de06401c8f824b5202eed5843224a3621fd9` |
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
