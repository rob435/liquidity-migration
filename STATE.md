# Operational state

## Purpose

Record the latest verified host snapshot and the source of each operational setting.

## Spec Tables

| Observation | Value |
| --- | --- |
| Verified at | 2026-09-07 17:24:28 UTC authenticated native positions/stops, services, heartbeats, disk, loaded images and Rust equity samples after cleanup deployment |
| Evidence | [Deploy run `34146148488`](https://github.com/rob435/liquidity-migration/actions/runs/34146148488); native read `/tmp/cleanup-final-native.json`; services/images `/tmp/cleanup-final-host.json`; recorder `/tmp/cleanup-final-observer.json`; workflow log `/tmp/cleanup-deploy-vps.log` |
| Host | `208.84.103.4` |
| Deployed commit | Completed generation, checkout and both loaded engine/worker images match the verified release archive for `76ad13ae265a5feff8010934b1a913d3064053d6`. Stored previous generation: `70f4c55770d49f04ec9ac1f0d2ebd6a5d4118831` |
| Funded permission | Mainnet remains armed; CARRY/LONG/EXODUS entry permissions remain enabled in both realms, with demo PROBE enabled |
| Runtime state | Engine PIDs demo `3047452` / mainnet `3048667`; worker PIDs `3047392` / `3048611`. All four services are active with heartbeats under five seconds, zero restarts/OOMs and no children. Both engines report `may_open=true`, `strategy_errors=[]` and zero stream resets. Each realm has seven positions and seven exact full-size native stops |
| Readiness boundary | Both workers report ready; all fourteen native stops match current position quantities exactly. All quantities and stop levels are unchanged from the 16:40:27 UTC predeploy read. The subsequent status read reports no failed systemd units and no warning/error journal entries for these four services since 17:15 UTC |
| Execution | Embedded callbacks, default Bybit binary; both engines use `Type=notify`, `WatchdogSec=30s`. Engine anonymous memory is 173.58 MiB demo / 126.86 MiB mainnet. Host Python contains only pip 24.0 and websocket-client 1.9.1 |
| Demo soak | All 31 workflow observations pass from 17:15:33.843 through 17:20:33.848 UTC, reaching 300 seconds before mainnet handover at 17:20:35.277; both native-state verification commands report already-complete. The deploy step finishes at 17:21:08.101 |
| Disk and WAL | 28.810 GiB free on `/var/lib`; tape reserves 25 GiB. All predeploy WAL inventory paths remain without shrinking; current inventory has 63 demo / 62 mainnet files, including the demo shadow-era file. No host WAL family is converted or pruned |
| Compatible retained release | All three compatible binaries remain at `/opt/liquidity-migration-engine/releases/8c92c96464bfe66662891c05f53a9510eefe8ba2/`; its archive remains under `staged/8c92c96464bfe66662891c05f53a9510eefe8ba2.tar.gz`. The 17:24:28 UTC read verifies all four hashes unchanged. This image retains every segment reader, Python import recovery and normalized legacy allocation readers. Retention does not establish automatic rollback across changed runtime inputs |
| Source qualification | Deployed source passes 2,003 developer Rust / 2,005 hosted Rust / 1,675 Python tests, zero failures and eight Rust ignores; formatting, strict Clippy, Ruff, ShellCheck and mypy pass. The ordinary Linux release archive verifies. Twenty recorder tests cover the 29-case Python oracle, I/O, clocks, HTTP and numeric boundaries; a compiled-command comparison matches all six fresh host samples, metrics and both curves. Executable handover and shallow-checkout regressions fail before their fixes and pass afterward |
| Last latency qualification | [Run `34128439094`](https://github.com/rob435/liquidity-migration/actions/runs/34128439094) is bound to `70f4c557`, not the cleanup release: 1,981 release tests, account/history workloads and all eight latency cells pass. Candidate median decision p99 / submit p50 is 2,000 / 721,150 ns, within absolute and paired limits. Its separate qualified archive verifies. Tail delays and all prior misses remain in [execution-performance.md](docs/execution-performance.md); no new latency qualification is claimed for `76ad13ae` |
| Copied-data qualification | The compatible Stage A reader passes all 27 original/converted base pairs / 54 boots, complete rotation state and ordered venue effects. Stage B passes all 27 converted-only boots, nine queued/nine prepared retrievals, both known filled archive queries and all 154 input length/mtime checks. Real source-frontier coverage is zero; future fills are absent and transport, risk and collateral are mocked. Original inputs remain unchanged. A complete matched production-day replay remains unqualified |
| Legacy recovery boundary | Ordinary admission is exact-only; archived scalar lineage, provenance-driven grid adoption, simulated binary64 fills and native protective repair remain required. The zero-hit/module-removal criterion remains unmet; see [quantity contracts](docs/engine.md) and [retained recovery](docs/operations.md) |
| Historical adapters | Recorder adapter and normalized Bybit/CSV/Parquet inputs use the same Rust core. Book, trade and bar execution assumptions are separate; [schemas and commands](docs/data.md#historical-adapters-and-execution). These research changes require no unrelated funded-runtime deployment |
| Production-day accounting | 2026-09-06 USDT linear window: all 38 demo / 77 mainnet trade fills and 28/29 funding executions match copied WAL/private cash records. Net transaction cash changes are `159.55178845` / `10.56366464` USDT. Order snapshots match cumulative fills/fees for 109/49 engine requests; one historical XCN request/terminal difference remains explicit. No independent midnight position/balance pair or complete chronological lifecycle/public-data reproduction is established; [exact gaps](docs/operations.md#observed-production-day-reconstruction) |
| Current-source measurements | Frozen `80db33df` baseline covers sustained traffic, burst backlog, partial filled-history throughput and delayed replies. A 2M-operation/100K-history offline soak completes; the fixed WAL-reader comparison falls from 5.656 s / 9.41 MB peak Python allocation to 0.103 s / 4.70 MB. Adverse cells, identities and scope remain in [execution-performance.md](docs/execution-performance.md#current-source-sustained-and-offline-reader-measurements); this does not requalify deployed network latency |
| Terminal retention boundary | Three zero-filled cancelled demo requests still lack exact terms in live segment `000063` at the 2026-09-07 21:04:59 UTC read: `eng-1788685989000-{8,9,10}`, retained since `1788702340615` ms. Natural expiry requires both wall time and complete execution history strictly beyond 2026-09-13 13:47:40.615 UTC; neither condition is met. Expiry alone does not retire archived lineage, legacy inventory or protective repair |
| Equity recorder | Rust `engine-tools record-equity` samples all six artifacts and pushes successfully after installation; both engine samples carry `76ad13ae`. The normal global command is active, the temporary handover override is absent and the Python script is deleted. During the demo soak, the pinned Rust command also pushes six samples while the funded runtime retains its previous images. Both operator curve commands read the existing monthly history successfully |

| Release image | SHA256 |
| --- | --- |
| Final engine, loaded in both realms | `4eb0bf51df07642c59ac61ade5b06920010adace36aab9bad4e9f17ab1c003f8` |
| Final signal worker, loaded in both realms | `8d9f0de77569498e44bd0f09b16fcca817c0f8d47050f4bab0376634d8fa0208` |
| Final engine tools, installed | `040d76c82bf220c19ef0dddc381df6c6553eb64c410684294314fd48d1a9eeb0` |
| Final downloaded release archive | `53136a8a14abfef44b497fd4531e8a94e09bf9abd0314095c10bf966d7b9a907` |
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
| WLDUSDT | 897.6 / 0.3521 | 73.3 / 0.3531 |

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
