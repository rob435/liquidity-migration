# Operational state

## Purpose

Record the latest verified host snapshot and the source of each operational setting.

## Spec Tables

| Observation | Value |
| --- | --- |
| Verified at | 2026-09-08 02:09:44 UTC authenticated native positions/stops, services, heartbeats, disk and loaded images; process/WAL/retained-image read at 02:05 UTC |
| Evidence | [Deploy run `34177470521`](https://github.com/rob435/liquidity-migration/actions/runs/34177470521); local evidence root `/tmp/execution-study-real-20260908/`: `final-host.json`, `final-native.json`, `final-extra.json`, `backup-final.json`, `live-second.json`, `final-deploy.log`; deployed reports under `/var/lib/liquidity-migration/execution-study/`, also checked off-host |
| Host | `208.84.103.4` |
| Deployed commit | Completed generation, checkout and all four loaded engine/worker images match the verified release archive for `06220e5657a2d1e332583c2e7650e116961a3f93`. Stored previous generation: `e301252f1330aadb54fd275466868da4a52c9db4`. Later documentation commits do not change these installed images |
| Funded permission | Mainnet remains armed; CARRY/LONG/EXODUS entry permissions remain enabled in both realms, with demo PROBE enabled |
| Runtime state | Engine PIDs demo `3164929` / mainnet `3166546`; worker PIDs `3164873` / `3166490`. All four services are active with heartbeats under four seconds, zero restarts/OOMs and no children. Both engines report `may_open=true`, `strategy_errors=[]` and zero stream resets. Demo has ten positions; mainnet has nine |
| Readiness boundary | Both workers report ready, with no spool backpressure or open WebSocket gap. All nineteen native stops match the current position quantities exactly. No failed systemd units. These are sampled host/account observations, not a complete production-day replay |
| Execution | Embedded callbacks, default Bybit binary; both engines use `Type=notify`, `WatchdogSec=30s`. Engine anonymous memory at 02:05 UTC is 269.50 MiB demo / 216.20 MiB mainnet; all four processes have zero swap |
| Demo soak | All 31 workflow observations pass from 01:50:40.668 through 01:55:40.669 UTC, reaching 300 seconds before mainnet handover at 01:55:41.876; `deploy-ok` is recorded at 01:56:16.146 |
| Disk and WAL | 57.779 GiB free on `/var/lib`; tape reserves 25 GiB. All 131 predeploy WAL paths remain without shrinking or changing inode. Numbered inventory: 65 demo / 64 mainnet; 63 sealed segments per realm share byte-identical backup-stage blocks. The growing demo `000066` is not yet staged; the mainnet `000065` snapshot is independent. No host WAL family is converted or pruned |
| Compatible retained release | All three compatible binaries remain at `/opt/liquidity-migration-engine/releases/8c92c96464bfe66662891c05f53a9510eefe8ba2/`; its archive remains under `staged/8c92c96464bfe66662891c05f53a9510eefe8ba2.tar.gz`. The 02:05 UTC read verifies all four hashes unchanged. This image retains every segment reader, Python import recovery and normalized legacy allocation readers. Retention does not establish automatic rollback across changed runtime inputs |
| Source qualification | Deployed source passes 2,035 developer Rust / 2,037 hosted Rust / 1,715 Python tests, zero failures, eight Rust ignores and one root/systemd-only Python skip. Formatting, strict Clippy on Rust 1.90, Ruff, ShellCheck and mypy pass. The separate root/systemd mount fixture passes on the host and fails with the old unit. Backup linking, legacy-fill compatibility, per-symbol tape errors and clock-unit regressions fail before their fixes and pass afterward. The ordinary Linux release archive verifies |
| Last latency qualification | [Run `34128439094`](https://github.com/rob435/liquidity-migration/actions/runs/34128439094) is bound to `70f4c557`: 1,981 release tests, account/history workloads and all eight latency cells pass. Candidate median decision p99 / submit p50 is 2,000 / 721,150 ns, within absolute and paired limits. Its separate qualified archive verifies. Tail delays and all prior misses remain in [execution-performance.md](docs/execution-performance.md); no new latency qualification is claimed for `06220e56` |
| Copied-data qualification | The compatible Stage A reader passes all 27 original/converted base pairs / 54 boots, complete rotation state and ordered venue effects. Stage B passes all 27 converted-only boots, nine queued/nine prepared retrievals, both known filled archive queries and all 154 input length/mtime checks. Real source-frontier coverage is zero; future fills are absent and transport, risk and collateral are mocked. Original inputs remain unchanged. A complete matched production-day replay remains unqualified |
| Legacy recovery boundary | Ordinary admission is exact-only; archived scalar lineage, provenance-driven grid adoption, simulated binary64 fills and native protective repair remain required. The zero-hit/module-removal criterion remains unmet; see [quantity contracts](docs/engine.md) and [retained recovery](docs/operations.md) |
| Historical adapters | Recorder adapter and normalized Bybit/CSV/Parquet inputs use the same Rust core. Book, trade and bar execution assumptions are separate; [schemas and commands](docs/data.md#historical-adapters-and-execution). These research changes require no unrelated funded-runtime deployment |
| Production-day accounting | 2026-09-06 USDT linear window: all 38 demo / 77 mainnet trade fills and 28/29 funding executions match copied WAL/private cash records. Net transaction cash changes are `159.55178845` / `10.56366464` USDT. Order snapshots match cumulative fills/fees for 109/49 engine requests; one historical XCN request/terminal difference remains explicit. No independent midnight position/balance pair or complete chronological lifecycle/public-data reproduction is established; [exact gaps](docs/operations.md#observed-production-day-reconstruction) |
| Current-source measurements | Frozen `80db33df` baseline covers sustained traffic, burst backlog, partial filled-history throughput and delayed replies. A 2M-operation/100K-history offline soak completes; the fixed WAL-reader comparison falls from 5.656 s / 9.41 MB peak Python allocation to 0.103 s / 4.70 MB. Adverse cells, identities and scope remain in [execution-performance.md](docs/execution-performance.md#current-source-sustained-and-offline-reader-measurements); this does not requalify deployed network latency |
| Terminal retention boundary | Three zero-filled cancelled demo requests still lack exact terms in live segment `000063` at the 2026-09-07 21:04:59 UTC read: `eng-1788685989000-{8,9,10}`, retained since `1788702340615` ms. Natural expiry requires both wall time and complete execution history strictly beyond 2026-09-13 13:47:40.615 UTC; neither condition is met. Expiry alone does not retire archived lineage, legacy inventory or protective repair |
| Equity recorder | Rust `engine-tools record-equity` remains active; its 02:04 and 02:05 UTC jobs each record and push six samples successfully. Both engine heartbeats identify `06220e56` |
| Execution study | [Contract and commands](docs/execution-study.md); active timer runs 900 s after completion. Final code succeeds at 01:58:13 and 02:02:15 UTC (8.676 / 1.056 CPU seconds). The second report contains 54 aligned orders / 91 identified fills, eleven complete comparisons and nine unchanged cache hits; 32 orders lack contemporaneous rules and eleven CAP orders have invalid archived tape. These remain unscored. No funded execution policy is changed |
| Observed fees / model boundary | Authenticated symbol rates are 10 / 3.6 bp taker / maker on eleven sampled symbols; CAP and HEMI are 11 / 4 bp. Nine observed market orders support crossing calibration; ARB error is -21.615 bp at 5 ms versus +1.834 bp at 100 ms. The small, correlated, seen-data sample selects no live policy |
| Backup / recorder recovery | 126 sealed WAL links release `33,847,439,360` duplicate stage bytes. Latest backup succeeds at 02:05:53 UTC: 257 remotely matching files, zero differences, no unlinkable roots; repeat linking releases zero further bytes. Live, staged and downloaded remote study reports match by SHA256; 54 per-order files are staged. Both recorders have `disk_blocked=false`, fresh receipts and zero queue drops; cumulative disk-drop counts remain unchanged from 01:54:47 through 02:09:44 UTC (Bybit 8,816,049 / Binance 2,487,759) |

| Release image | SHA256 |
| --- | --- |
| Final engine, loaded in both realms | `8f10847e5c17e5de87880060e80a24039dbfa9de83bf08048902d98295f29ac0` |
| Final signal worker, loaded in both realms | `8d9f0de77569498e44bd0f09b16fcca817c0f8d47050f4bab0376634d8fa0208` |
| Final engine tools, installed | `e78c8ee03ce14092a35715e1a7aa20186e3a6d462b8badd2acec65e2c869bdab` |
| Final downloaded release archive | `a39eadfbebda9076a4a088579660b2be088b8a6086129aa56934e19025571013` |
| Compatible Stage A engine, retained | `9fc63d5344c9190cb700cef151b6af8c1082eecb64ee347395c1c01302f01ae6` |
| Compatible Stage A signal worker, retained | `96fb34e84f174baa1acd931a35202f3d42009b3b8ac099e3916c4fabfa44caf8` |
| Compatible Stage A engine tools, retained | `1794e243a263af0b77ec7cbc9639e8fc6aa997562356609e72398015b7d2b1a4` |
| Compatible Stage A staged archive | `9765701173dabecb6258d216302bd58d80c420bbc81d53607e54a35d7c5bac2e` |

| Open long position | Demo quantity / native stop | Mainnet quantity / native stop |
| --- | --- | --- |
| ACEUSDT | 2887.5 / 0.12464 | 213.3 / 0.12439 |
| ARBUSDT | 1788.1 / 0.13766 | 138.8 / 0.13767 |
| BNBUSDT | 2.16 / 673.7 | 0.17 / 673.4 |
| FLOCKUSDT | 1038 / 0.04117 | — |
| HEMIUSDT | 15008 / 0.005713 | 1161 / 0.005711 |
| JUPUSDT | 1334 / 0.2021 | 103 / 0.2026 |
| LINKUSDT | 53.9 / 11.262 | 4.2 / 11.238 |
| LTCUSDT | 21.4 / 47.11 | 1.7 / 47.08 |
| TAOUSDT | 3.031 / 202.17 | 0.248 / 202.32 |
| WLDUSDT | 852.7 / 0.3521 | 73.3 / 0.3531 |

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
