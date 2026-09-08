# Operational state

## Purpose

Record the latest verified host snapshot and the source of each operational setting.

## Spec Tables

| Observation | Value |
| --- | --- |
| Verified at | 2026-09-08 16:00:47 UTC unit state, engine and worker heartbeats, engine journals and disk, read over the `diagnose` workflow. No authenticated account, native-stop, WAL or image reading is taken at that time; the 10:24:02/10:25:30 rows below them are the last such reading |
| Evidence | [Diagnose run `34248345738`](https://github.com/rob435/liquidity-migration/actions/runs/34248345738) for the current reading; [deploy run `34246230385`](https://github.com/rob435/liquidity-migration/actions/runs/34246230385) for what is installed. Earlier: [deploy run `34213632476`](https://github.com/rob435/liquidity-migration/actions/runs/34213632476); local evidence root `/tmp/annual-20260908/`: `final-host.json`, `final-native.json`, `final-extra.json`, `runtime-config.json`, `release-verify.json`, `final-verification.json`, `deployed-study.json`, `deploy.log`; durable research and execution copies under `~/SHARED_DATA/bybit_full_pit/reports/annual_20260908/` |
| Host | `208.84.103.4` |
| Deployed commit | `ce2b1299e29f528bd1daf762033d1e88e69b2b26` in both realms: deploy `34246230385` replaced demo at 15:47:57 and mainnet at 15:53:38 and recorded `deploy-ok` at 15:54:03. Stored rollback target is `441811eb227b1084eaa3343d3d10428994425481`. Both engine heartbeats identify `ce2b129`; no release-archive or image hash reading is taken since 10:24 and the image table below is the earlier release. `cecff2e` and later commits are not deployed |
| Funded permission | Mainnet remains armed and may open again: the 15:53 handover applied an operator-placed `reconcile-clear.mainnet.note`, which reset the latch and restated exposure over six symbols, and the engine reads `may_open=true`. `rolling_loss_tripped=true` at `-10.30624956` against the `10` USDT limit still refuses entries, so every mainnet sleeve reads `entries_enabled=false`. Demo reads `may_open=true` with CARRY/LONG/EXODUS/PROBE enabled. Incident `mainnet-ac90e31c207bc0da` in [CHANGELOG.md](CHANGELOG.md) |
| Runtime state | Engine PIDs demo `3344892` / mainnet `3346208`; worker PIDs `3344834` / `3346152`. All four services are active with heartbeats under five seconds and no failed units. Mainnet reports `may_open=true`, `rolling_loss_tripped=true`, `stream_resets=0`, `strategy_errors=[]`, every sleeve `entries_enabled=false`, six `inside_resize_band` entry blockers, no working entries and six positions; demo reports `may_open=true`, `strategy_errors=[]` and six positions. At 16:00:46 the demo worker reads `status=ready` and the mainnet worker `status=recovering`, both with `bybit_ws_gap_open=false` and complete ticker coverage |
| Readiness boundary | No failed systemd units at 15:54:03, and every fleet timer is active in that verify table. `liquidity-migration-mainnet-liveness.timer` passes every 30 s — 15:59:19, 15:59:50, 16:00:21, each `Result=success` — and `liquidity-migration-execution-study.timer` came back with the 15:53 mainnet handover, closing `host-51b05439c4f09794`; the host watchdog reads `ok scope=host units-and-heartbeats-healthy` at 15:55:04 and 15:58:04 with no `watchdog:mainnet`. The eighteen native stops matching position quantities are the 10:25:30 reading and predate the three mainnet reductions at 15:10:29 and 15:11:11; no native stop or position reading is taken since. These are sampled host observations, not a complete production-day replay |
| Execution | Embedded callbacks, default Bybit binary; both engines use `Type=notify`, `WatchdogSec=30s`. Engine anonymous memory in the post-handover read is 111.77 MiB demo / 295.08 MiB mainnet; all four processes have zero swap |
| Demo soak | All 31 workflow observations pass from 10:17:59.988 through 10:22:59.992 UTC, reaching 300 seconds before mainnet handover; `deploy-ok` is recorded at 10:23:36.295 |
| Disk and WAL | 42 GiB free on `/var/lib` at 15:36:52, 64% used; tape reserves 25 GiB. All 131 prior WAL inventory paths remain without shrinking or changing inode. No host WAL family is converted or pruned |
| Compatible retained release | All three compatible binaries remain at `/opt/liquidity-migration-engine/releases/8c92c96464bfe66662891c05f53a9510eefe8ba2/`; its archive remains under `staged/8c92c96464bfe66662891c05f53a9510eefe8ba2.tar.gz`. The post-handover read verifies all four hashes unchanged. Retention does not establish automatic rollback across changed runtime inputs |
| Source qualification | Deployed source passes 2,042 developer Rust / 2,044 hosted Rust / 1,719 Python tests, zero failures, eight Rust ignores and one root/systemd-only Python skip. Formatting, strict Clippy on Rust 1.90, Ruff, ShellCheck and mypy pass. Worked-entry recovery and native passive-deadline regressions fail before their respective fixes and pass afterward; the ordinary Linux release archive verifies |
| Last latency qualification | [Run `34128439094`](https://github.com/rob435/liquidity-migration/actions/runs/34128439094) is bound to `70f4c557`: 1,981 release tests, account/history workloads and all eight latency cells pass. Candidate median decision p99 / submit p50 is 2,000 / 721,150 ns, within absolute and paired limits. Its separate qualified archive verifies. Tail delays and all prior misses remain in [execution-performance.md](docs/execution-performance.md); no new latency qualification is claimed for `441811eb` |
| Copied-data qualification | The compatible Stage A reader passes all 27 original/converted base pairs / 54 boots, complete rotation state and ordered venue effects. Stage B passes all 27 converted-only boots, nine queued/nine prepared retrievals, both known filled archive queries and all 154 input length/mtime checks. Real source-frontier coverage is zero; future fills are absent and transport, risk and collateral are mocked. Original inputs remain unchanged. A complete matched production-day replay remains unqualified |
| Legacy recovery boundary | Ordinary admission is exact-only; archived scalar lineage, provenance-driven grid adoption, simulated binary64 fills and native protective repair remain required. The zero-hit/module-removal criterion remains unmet; see [quantity contracts](docs/engine.md) and [retained recovery](docs/operations.md) |
| Historical adapters | Recorder adapter and normalized Bybit/CSV/Parquet inputs use the same Rust core. Book, trade and bar execution assumptions are separate; [schemas and commands](docs/data.md#historical-adapters-and-execution). These research changes require no unrelated funded-runtime deployment |
| Production-day accounting | 2026-09-06 USDT linear window: all 38 demo / 77 mainnet trade fills and 28/29 funding executions match copied WAL/private cash records. Net transaction cash changes are `159.55178845` / `10.56366464` USDT. Order snapshots match cumulative fills/fees for 109/49 engine requests; one historical XCN request/terminal difference remains explicit. No independent midnight position/balance pair or complete chronological lifecycle/public-data reproduction is established; [exact gaps](docs/operations.md#observed-production-day-reconstruction) |
| Current-source measurements | Frozen `80db33df` baseline covers sustained traffic, burst backlog, partial filled-history throughput and delayed replies. A 2M-operation/100K-history offline soak completes; the fixed WAL-reader comparison falls from 5.656 s / 9.41 MB peak Python allocation to 0.103 s / 4.70 MB. Adverse cells, identities and scope remain in [execution-performance.md](docs/execution-performance.md#current-source-sustained-and-offline-reader-measurements); this does not requalify deployed network latency |
| Terminal retention boundary | Three zero-filled cancelled demo requests still lack exact terms in live segment `000063` at the 2026-09-07 21:04:59 UTC read: `eng-1788685989000-{8,9,10}`, retained since `1788702340615` ms. Natural expiry requires both wall time and complete execution history strictly beyond 2026-09-13 13:47:40.615 UTC; neither condition is met. Expiry alone does not retire archived lineage, legacy inventory or protective repair |
| Equity recorder | Rust `engine-tools record-equity` remains active; its 10:24:23 and 10:25:21 UTC jobs each record and push six samples after handover. Both engine heartbeats identify `441811eb` |
| Execution study | [Contract and commands](docs/execution-study.md); the fifteen-minute timer is inactive from 15:10:16 and its service has not run since, so no report postdates that handover. Installed `441811eb` report includes the exact `passive_entry_30s` candidate alongside the seven existing policies. [Annual review](docs/research/annual-execution-2026-09-08.md) retains every queue/latency cell. LONG demo now uses the 30 s GTC policy; mainnet LONG still crosses. Realized demo savings are not yet observed |
| Observed fees / model boundary | The 48 h sample ending 07:47:54 UTC has 54 trade fills matching authenticated Bybit execution and transaction records. Actual cost is 10.3433 bp fees + 4.0142 bp slippage = 14.3575 bp per executed side, with 100% cost coverage. Most selected symbols charge 10 / 3.6 bp taker / maker; CAP and HEMI charge 11 / 4 bp. Five usable LONG opening comparisons shape the demo choice; the ARB crossing error of −21.615 bp at 5 ms and small selected sample preclude claiming proven savings |
| Backup / recorder recovery | Last backup observation remains 2026-09-08 08:34 UTC: the scheduled 03:20:41 run has 259 remote files and `35,498,086,400` recorded bytes, and both recorders have `disk_blocked=false`, fresh receipts and zero queue drops. No post-10:23-deploy backup completion is claimed |

| Release image | SHA256 |
| --- | --- |
| Final engine, loaded in both realms | `e53006cbaf98adacaa76d78db2ece08f8e2b4ca2beaf22102b90d9af2b07367d` |
| Final signal worker, loaded in both realms | `76d189d0164d6a36eb2662173e42e82ab4b7afef39f4cf0a371d4405124e4a9e` |
| Final engine tools, installed | `5e16fbbb5c709fba36c482accd17b7ca27b6b5d2090b07ba3ab57b6070d4ab5f` |
| Final downloaded release archive | `f1fc521d6d79752a264b3c6c5042908581ddf6c449ef6af634f676d776db0534` |
| Compatible Stage A engine, retained | `9fc63d5344c9190cb700cef151b6af8c1082eecb64ee347395c1c01302f01ae6` |
| Compatible Stage A signal worker, retained | `96fb34e84f174baa1acd931a35202f3d42009b3b8ac099e3916c4fabfa44caf8` |
| Compatible Stage A engine tools, retained | `1794e243a263af0b77ec7cbc9639e8fc6aa997562356609e72398015b7d2b1a4` |
| Compatible Stage A staged archive | `9765701173dabecb6258d216302bd58d80c420bbc81d53607e54a35d7c5bac2e` |

Open positions below are the 2026-09-08 10:25:30 authenticated reading. Mainnet
closed ACEUSDT (carry), JUPUSDT and ARBUSDT (long) at 15:10:29 and 15:11:11 and
now holds six; demo also reports six, and which six is not in that reading.

| Open position | Demo side / quantity / native stop | Mainnet side / quantity / native stop |
| --- | --- | --- |
| ACEUSDT | long / 3024.6 / 0.12465 | long / 213.3 / 0.12439 |
| ARBUSDT | long / 1788.1 / 0.13766 | long / 138.8 / 0.13767 |
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
