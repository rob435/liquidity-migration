# Operational state

## Purpose

Record the latest verified host snapshot and the source of each operational setting.

## Spec Tables

| Observation | Value |
| --- | --- |
| Verified at | 2026-09-08 08:36:59 UTC services, heartbeats, disk and loaded images; authenticated native positions/stops and process/WAL/retained-image read at 08:34:57; installed study verified again at 08:38:02 |
| Evidence | [Deploy run `34203614326`](https://github.com/rob435/liquidity-migration/actions/runs/34203614326); local evidence root `/tmp/execution-study-review-20260908/`: `settled-host.json`, `final-native.json`, `final-extra.json`, `final-backup.json`, `deployed-report.json`, `cached-report.json`, `account.json`, `reconciled-costs.json`, `deploy.log`; deployed reports under `/var/lib/liquidity-migration/execution-study/` |
| Host | `208.84.103.4` |
| Deployed commit | Completed generation, checkout and all four loaded engine/worker images match the verified release archive for `329ba5dc20fee120bd72814a54d9555fd6fe3f2d`. Stored previous generation: `06220e5657a2d1e332583c2e7650e116961a3f93`. Later documentation commits do not change these installed images |
| Funded permission | Mainnet remains armed; CARRY/LONG/EXODUS entry permissions remain enabled in both realms, with demo PROBE enabled |
| Runtime state | Engine PIDs demo `3245937` / mainnet `3247330`; worker PIDs `3245878` / `3247274`. All four services are active with heartbeats under five seconds, zero restarts/OOMs and no children. Both engines report `may_open=true`, `strategy_errors=[]` and zero stream resets. Demo has eleven positions; mainnet has ten |
| Readiness boundary | Both workers report ready, with no spool backpressure or open WebSocket gap. All twenty-one native stops match the current position quantities exactly. No failed systemd units. These are sampled host/account observations, not a complete production-day replay |
| Execution | Embedded callbacks, default Bybit binary; both engines use `Type=notify`, `WatchdogSec=30s`. Engine anonymous memory at 08:34:57 UTC is 177.19 MiB demo / 124.27 MiB mainnet; all four processes have zero swap |
| Demo soak | All 31 workflow observations pass from 08:28:21.720 through 08:33:21.730 UTC, reaching 300 seconds before mainnet handover at 08:33:23.045; `deploy-ok` is recorded at 08:33:55.458 |
| Disk and WAL | 48.834 GiB free on `/var/lib`; tape reserves 25 GiB. All 131 prior WAL inventory paths remain without shrinking or changing inode. Numbered inventory: 67 files per realm; 64 sealed segments per realm share backup-stage blocks. The growing `000068` files remain independent of the stage. No host WAL family is converted or pruned |
| Compatible retained release | All three compatible binaries remain at `/opt/liquidity-migration-engine/releases/8c92c96464bfe66662891c05f53a9510eefe8ba2/`; its archive remains under `staged/8c92c96464bfe66662891c05f53a9510eefe8ba2.tar.gz`. The 08:34:57 UTC read verifies all four hashes unchanged. This image retains every segment reader, Python import recovery and normalized legacy allocation readers. Retention does not establish automatic rollback across changed runtime inputs |
| Source qualification | Deployed source passes 2,037 developer Rust / 2,039 hosted Rust / 1,715 Python tests, zero failures, eight Rust ignores and one root/systemd-only Python skip. Formatting, strict Clippy on Rust 1.90, Ruff, ShellCheck and mypy pass. Eighteen study tests include cost aggregation, missing inputs, rebates, buy/sell signs, replay and cache reuse. The two new all-in reporting regressions fail before the change and pass afterward; the ordinary Linux release archive verifies |
| Last latency qualification | [Run `34128439094`](https://github.com/rob435/liquidity-migration/actions/runs/34128439094) is bound to `70f4c557`: 1,981 release tests, account/history workloads and all eight latency cells pass. Candidate median decision p99 / submit p50 is 2,000 / 721,150 ns, within absolute and paired limits. Its separate qualified archive verifies. Tail delays and all prior misses remain in [execution-performance.md](docs/execution-performance.md); no new latency qualification is claimed for `329ba5dc` |
| Copied-data qualification | The compatible Stage A reader passes all 27 original/converted base pairs / 54 boots, complete rotation state and ordered venue effects. Stage B passes all 27 converted-only boots, nine queued/nine prepared retrievals, both known filled archive queries and all 154 input length/mtime checks. Real source-frontier coverage is zero; future fills are absent and transport, risk and collateral are mocked. Original inputs remain unchanged. A complete matched production-day replay remains unqualified |
| Legacy recovery boundary | Ordinary admission is exact-only; archived scalar lineage, provenance-driven grid adoption, simulated binary64 fills and native protective repair remain required. The zero-hit/module-removal criterion remains unmet; see [quantity contracts](docs/engine.md) and [retained recovery](docs/operations.md) |
| Historical adapters | Recorder adapter and normalized Bybit/CSV/Parquet inputs use the same Rust core. Book, trade and bar execution assumptions are separate; [schemas and commands](docs/data.md#historical-adapters-and-execution). These research changes require no unrelated funded-runtime deployment |
| Production-day accounting | 2026-09-06 USDT linear window: all 38 demo / 77 mainnet trade fills and 28/29 funding executions match copied WAL/private cash records. Net transaction cash changes are `159.55178845` / `10.56366464` USDT. Order snapshots match cumulative fills/fees for 109/49 engine requests; one historical XCN request/terminal difference remains explicit. No independent midnight position/balance pair or complete chronological lifecycle/public-data reproduction is established; [exact gaps](docs/operations.md#observed-production-day-reconstruction) |
| Current-source measurements | Frozen `80db33df` baseline covers sustained traffic, burst backlog, partial filled-history throughput and delayed replies. A 2M-operation/100K-history offline soak completes; the fixed WAL-reader comparison falls from 5.656 s / 9.41 MB peak Python allocation to 0.103 s / 4.70 MB. Adverse cells, identities and scope remain in [execution-performance.md](docs/execution-performance.md#current-source-sustained-and-offline-reader-measurements); this does not requalify deployed network latency |
| Terminal retention boundary | Three zero-filled cancelled demo requests still lack exact terms in live segment `000063` at the 2026-09-07 21:04:59 UTC read: `eng-1788685989000-{8,9,10}`, retained since `1788702340615` ms. Natural expiry requires both wall time and complete execution history strictly beyond 2026-09-13 13:47:40.615 UTC; neither condition is met. Expiry alone does not retire archived lineage, legacy inventory or protective repair |
| Equity recorder | Rust `engine-tools record-equity` remains active; its 08:34:22 UTC job records and pushes six samples successfully after handover. Both engine heartbeats identify `329ba5dc` |
| Execution study | [Contract and commands](docs/execution-study.md); active timer runs 900 s after completion. Installed code succeeds at 08:35:08 and 08:38:02 UTC (11.139 / 0.197 CPU seconds). The report contains 23 aligned orders / 54 identified fills, twelve complete comparisons and twelve unchanged cache hits on the repeat run. Eleven CAP orders have invalid archived tape and remain unscored in the simulation. All 23 original order records and hypothetical outcomes match the predeploy report. Funded execution policy is unchanged |
| Observed fees / model boundary | The 54 trade fills match authenticated Bybit execution and transaction records by identity, quantities, prices and paid fees. Actual cost is 10.3433 bp fees + 4.0142 bp slippage = 14.3575 bp per executed side; all-in coverage is 100%, on one arrival-notional basis. Account-symbol rates remain 10 / 3.6 bp taker / maker on most selected symbols; CAP and HEMI are 11 / 4 bp. Twelve usable hypothetical orders and ARB crossing error of -21.615 bp at 5 ms support no selected live policy |
| Backup / recorder recovery | 128 sealed WAL files share stage blocks. Latest scheduled backup succeeds at 03:20:41 UTC: 259 remote files, recorded bytes `35,498,086,400`; the study output remains in its source list. The report generated after the 08:33 deployment awaits the next ordinary backup. Both recorders have `disk_blocked=false`, fresh receipts and zero queue drops; cumulative disk-drop counts remain unchanged since 01:54:47 UTC (Bybit 8,816,049 / Binance 2,487,759) |

| Release image | SHA256 |
| --- | --- |
| Final engine, loaded in both realms | `683690a8978b094d5e29909e09931c3c96f9e590ea393d2403e9fc71e1572344` |
| Final signal worker, loaded in both realms | `8d9f0de77569498e44bd0f09b16fcca817c0f8d47050f4bab0376634d8fa0208` |
| Final engine tools, installed | `06372a8387f252d6b2893755fb7907ef431ba8875295ae9e189e3c58fd5af668` |
| Final downloaded release archive | `749626d395f94c15194deaa66c92c8fb17a1d17f62f90d37defc6efbbdf99288` |
| Compatible Stage A engine, retained | `9fc63d5344c9190cb700cef151b6af8c1082eecb64ee347395c1c01302f01ae6` |
| Compatible Stage A signal worker, retained | `96fb34e84f174baa1acd931a35202f3d42009b3b8ac099e3916c4fabfa44caf8` |
| Compatible Stage A engine tools, retained | `1794e243a263af0b77ec7cbc9639e8fc6aa997562356609e72398015b7d2b1a4` |
| Compatible Stage A staged archive | `9765701173dabecb6258d216302bd58d80c420bbc81d53607e54a35d7c5bac2e` |

| Open position | Demo side / quantity / native stop | Mainnet side / quantity / native stop |
| --- | --- | --- |
| ACEUSDT | long / 3024.6 / 0.12465 | long / 213.3 / 0.12439 |
| ARBUSDT | long / 1788.1 / 0.13766 | long / 138.8 / 0.13767 |
| BNBUSDT | long / 2.16 / 673.7 | long / 0.17 / 673.4 |
| FLOCKUSDT | short / 1038 / 0.08238 | — |
| HEMIUSDT | long / 15008 / 0.005713 | long / 1161 / 0.005711 |
| INJUSDT | long / 59.5 / 5.16 | long / 4.6 / 5.161 |
| JUPUSDT | long / 1334 / 0.2021 | long / 103 / 0.2026 |
| LINKUSDT | long / 53.9 / 11.262 | long / 4.2 / 11.238 |
| LTCUSDT | long / 21.4 / 47.11 | long / 1.7 / 47.08 |
| TAOUSDT | long / 3.031 / 202.17 | long / 0.248 / 202.32 |
| WLDUSDT | long / 852.7 / 0.3521 | long / 73.3 / 0.3531 |

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
scripts/ops.sh execution-study
scripts/ops.sh --help
```

[Operations](docs/operations.md) · [Engine](docs/engine.md) · [Data](docs/data.md) · [Trading rules](docs/trading_logic.md)
