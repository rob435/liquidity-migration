# Operational state

## Purpose

Record the latest verified host snapshot and the source of each operational setting.

## Spec Tables

| Observation | Value |
| --- | --- |
| Verified at | 2026-09-08 21:06:24 UTC every fleet unit's state and every heartbeat's age, read in the deploy's own verify table after `deploy-ok`. Loaded images, PIDs, failed units and the authenticated MEXC identity are the 17:24:50 UTC SSH read; worker readiness, rolling-loss and watchdog rows are the 16:54 UTC `diagnose` reading; Bybit positions, stops, WAL inventory and CARRY checkpoint rows are the 16:12–16:16 UTC read. None of the three is re-read here, and the 20:35–21:06 handovers replaced the demo, mainnet and mexc processes |
| Evidence | [Deploy run `34276974179`](https://github.com/rob435/liquidity-migration/actions/runs/34276974179) (`22794ad1`, `deploy-ok` 21:06 UTC) for what is installed and for the 21:06:24 verify table; its failed predecessor [run `34274722547`](https://github.com/rob435/liquidity-migration/actions/runs/34274722547) (`21bd9227`, mexc handover) is the incident in [CHANGELOG.md](CHANGELOG.md); [diagnose run `34253894947`](https://github.com/rob435/liquidity-migration/actions/runs/34253894947) for the 16:54 reading; durable evidence root `~/SHARED_DATA/bybit_full_pit/reports/carry_daily_20260908/`: `host-after.json`, `account-{demo,mainnet}-after.json`, `state-after.json`, `release-verify.json`, `host-verification.json`, `daily-verification.json`, `qualification.json`, `deploy.log`; independent infrastructure receipts under `reports/infra_20260908/` |
| Host | `208.84.103.4` |
| Deployed commit | Completed generation and checkout are `22794ad11b76209ad24522967484847bf8aab1ca`, with `deploy-ok` at 21:06 UTC; the demo and mainnet engine binaries are unchanged from `21bd9227`, so both realms kept the processes their 20:35:17 and 20:40:52 handovers started and their heartbeats still identify `21bd9227`. Stored previous generation: `32f27d4b61d2ba53c377a7811aa19179372b621f`. The image hashes in the table below are the 17:25 reading and are not re-read; the 20:35–21:06 handovers replaced the loaded binaries. Later source and documentation commits are not evidence of installation |
| Source / installed boundary | The MEXC first-boot native-state repair `22794ad1` and the `live-proven` promotion `21bd9227` are installed. `512b1007` is the last generation read over SSH; every row dated 16:12–17:25 was taken under it |
| MEXC realm | `mexc_mainnet` is armed in `/etc/liquidity-migration/mexc-mainnet.env` and the installed engine reports it `live-proven`, so the realm took its first handover under `22794ad1`: `liquidity-migration-engine-mexc` and `signal-worker-mexc` are active with 4 s and 3 s heartbeats and `mexc-liveness.timer` is active at 21:06:24 UTC. Its native strategy state was initialized empty on that handover — the realm has no earlier WAL history. `engine-mexc.toml` is rendered with LONG entries on and CARRY/EXODUS entries off. Account `key-e3b03c8170d1fc6b`: identity bound and `flat=true samples=2 positions=0 open_orders=0` at 17:24 UTC, 52.6207 USDT equity at 17:05 UTC, neither re-read since its units started |
| Funded permission | Mainnet remains armed with sole leverage authority; configured CARRY/LONG/EXODUS entries remain enabled in both Bybit realms, with demo PROBE enabled. Both Bybit engines report `may_open=true`. Both rolling-loss restrictions are tripped and every sleeve refuses entries under them: mainnet 13.31799509 USDT against a 10 USDT limit at 21:07 UTC, demo 178.95632852 against 161.012885862 at 20:42 UTC. `reduce_only` exits are admitted ahead of the check |
| CARRY holding | Both realms hold fixed quantity targets until the next daily decision; intraday funding, upcoming-book drop and pre-settlement exits are disabled. Current HEMI anchors remain demo `14131` / mainnet `1161`; native stops and explicit reductions remain active. The `FLOCKUSDT` exit tombstone for decision `1788825600000` remains intact; no new pre-settlement fires feed EXODUS |
| Runtime state | Engine PIDs demo `3426511` / mainnet `3427651` / mexc `3440463`; worker PIDs `3426454` / `3427595` / `3440405`. Demo and mainnet come from the 20:35:17 and 20:40:52 handovers, mexc from its first handover at 21:05:56. All six services are active at 21:06:24 UTC with heartbeat ages between 0 s and 4 s. Demo and mainnet engines report `may_open=true`, `strategy_errors=[]` and four positions each at 21:07 UTC; `systemctl --failed` is not re-read since 17:24 |
| Readiness boundary | Every manifest timer, the mexc one included, is active in the 21:06:24 verify table, and the last read of `systemctl --failed` is the empty 17:24:50 one. The mexc realm has one accepted startup sample: `heartbeat-ok` for its worker at 21:06:11 and its engine at 21:06:23, on a realm with no trading history at all. Worker `status=ready`, complete ticker coverage and the standing rolling-loss page are the 16:54 reading; the 70 host/account checks are the 16:16:53 observation. Earlier post-startup samples include bounded `recovering` states, including a mark-freshness lapse after gap repair. These are sampled observations, not uninterrupted-readiness or complete production-day proof |
| Execution | Embedded callbacks; all three engines use `Type=notify`, `WatchdogSec=30s`. Zero swap is the 17:24 reading of the four Bybit processes. LONG uses 30-second PostOnly entry work in every realm; terminal order lookup and exact fill recovery precede an IOC remainder |
| Demo soak | All 31 observations pass from 16:06:00.607 through 16:11:00.615 UTC, reaching 300 seconds before mainnet handover. `deploy-ok` is recorded at 16:11:35.448 UTC |
| Disk and WAL | 42.310 GiB free on `/var/lib`; tape reserves 25 GiB. All 141 pre-handover WAL inventory paths retain their inodes and at least their original bytes; current inventory has 143 paths. No live WAL is pruned |
| Retained Stage A release | All three Stage A binaries remain at `/opt/liquidity-migration-engine/releases/8c92c96464bfe66662891c05f53a9510eefe8ba2/`, with its staged archive; all four hashes remain unchanged. This historical reader is not requalified against current runtime-reconfiguration records. Retention does not authorize rollback across changed runtime inputs |
| Source qualification | Deployed source passes 2,079 developer Rust / 2,081 hosted Rust / 1,744 Python tests, zero failures, eight Rust ignores and one root/systemd-only Python skip. Rust 1.90 formatting and strict Clippy, Ruff, ShellCheck and mypy pass. Daily holding, compatible state recovery, history timeout and crossed-stop regressions fail before their fixes and pass afterward; the Linux release archive verifies |
| Last latency qualification | [Run `34128439094`](https://github.com/rob435/liquidity-migration/actions/runs/34128439094) is bound to `70f4c557`: 1,981 release tests, account/history workloads and all eight latency cells pass. Candidate median decision p99 / submit p50 is 2,000 / 721,150 ns, within absolute and paired limits. Its separate qualified archive verifies. Tail delays and all prior misses remain in [execution-performance.md](docs/execution-performance.md); no new latency qualification is claimed for `cecff2e2` |
| Copied-data qualification | The compatible Stage A reader passes all 27 original/converted base pairs / 54 boots, complete rotation state and ordered venue effects. Stage B passes all 27 converted-only boots, nine queued/nine prepared retrievals, both known filled archive queries and all 154 input length/mtime checks. Real source-frontier coverage is zero; future fills are absent and transport, risk and collateral are mocked. Original inputs remain unchanged. A complete matched production-day replay remains unqualified |
| Legacy recovery boundary | Ordinary admission is exact-only; archived scalar lineage, provenance-driven grid adoption, simulated binary64 fills and native protective repair remain required. The zero-hit/module-removal criterion remains unmet; see [quantity contracts](docs/engine.md) and [retained recovery](docs/operations.md) |
| Historical adapters | Recorder adapter and normalized Bybit/CSV/Parquet inputs use the same Rust core. Book, trade and bar execution assumptions are separate; [schemas and commands](docs/data.md#historical-adapters-and-execution). These research changes require no unrelated funded-runtime deployment |
| Production-day accounting | 2026-09-06 USDT linear window: all 38 demo / 77 mainnet trade fills and 28/29 funding executions match copied WAL/private cash records. Net transaction cash changes are `159.55178845` / `10.56366464` USDT. Order snapshots match cumulative fills/fees for 109/49 engine requests; one historical XCN request/terminal difference remains explicit. No independent midnight position/balance pair or complete chronological lifecycle/public-data reproduction is established; [exact gaps](docs/operations.md#observed-production-day-reconstruction) |
| Current-source measurements | Frozen `80db33df` baseline covers sustained traffic, burst backlog, partial filled-history throughput and delayed replies. A 2M-operation/100K-history offline soak completes; the fixed WAL-reader comparison falls from 5.656 s / 9.41 MB peak Python allocation to 0.103 s / 4.70 MB. Adverse cells, identities and scope remain in [execution-performance.md](docs/execution-performance.md#current-source-sustained-and-offline-reader-measurements); this does not requalify deployed network latency |
| Terminal retention boundary | Three zero-filled cancelled demo requests still lack exact terms in live segment `000063` at the 2026-09-07 21:04:59 UTC read: `eng-1788685989000-{8,9,10}`, retained since `1788702340615` ms. Natural expiry requires both wall time and complete execution history strictly beyond 2026-09-13 13:47:40.615 UTC; neither condition is met. Expiry alone does not retire archived lineage, legacy inventory or protective repair |
| Equity recorder | Rust `engine-tools record-equity` remains scheduled every minute; its 16:12:22 UTC job completes successfully after handover. The demo and mainnet heartbeats identify `21bd9227` at 21:07 UTC |
| Execution study | [Contract and commands](docs/execution-study.md); fifteen-minute timer active. Both LONG realms use the 30-second PostOnly policy. The [annual review](docs/research/annual-execution-2026-09-08.md) retains all queue/latency cells and the selected sample’s limits; realized savings from the new policy are not established |
| Observed fees / model boundary | The 48 h sample ending 07:47:54 UTC has 54 trade fills matching authenticated Bybit execution and transaction records. Actual cost is 10.3433 bp fees + 4.0142 bp slippage = 14.3575 bp per executed side, with 100% cost coverage. Most selected symbols charge 10 / 3.6 bp taker / maker; CAP and HEMI charge 11 / 4 bp. Five usable LONG opening comparisons shape the demo choice; the ARB crossing error of −21.615 bp at 5 ms and small selected sample preclude claiming proven savings |
| Backup / recorder recovery | The 16:02 backup completes at 16:05:36 UTC with 347 files; the independent remote comparison reports 347 matches and zero differences. The timer runs every 15 minutes with a 10-minute job budget. Both market recorders have fresh, unblocked status; cumulative prior disk-drop counters remain explicit in the raw receipts. NTP is synchronized; the 16:08 venue-clock probe reports +8.94 ms offset with ±11.10 ms uncertainty and no alert |

| Release image | SHA256 |
| --- | --- |
| Final engine, loaded in both realms | `fcaea5f8192e59c4af49812157aae8b1846a9b2fe7e490e3ea4d9ce4e2a44629` |
| Final signal worker, loaded in both realms | `f8706abf06226b7e0e1a6e1cf466b4270618c13c8b912b3cc8eefcd92b127c6b` |
| Final engine tools, installed | `650d35cd8178aa0cbbe37e157375c26615478d4e206d3055aefbcaa4cafd1794` |
| Final downloaded release archive | `b10e4f83b99dd6342855072958251a58f0669bc7a6c3e59b3f2f68b80e82e344` |
| Retained Stage A engine, retained | `9fc63d5344c9190cb700cef151b6af8c1082eecb64ee347395c1c01302f01ae6` |
| Retained Stage A signal worker, retained | `96fb34e84f174baa1acd931a35202f3d42009b3b8ac099e3916c4fabfa44caf8` |
| Retained Stage A engine tools, retained | `1794e243a263af0b77ec7cbc9639e8fc6aa997562356609e72398015b7d2b1a4` |
| Retained Stage A staged archive | `9765701173dabecb6258d216302bd58d80c420bbc81d53607e54a35d7c5bac2e` |

| Open position | Demo side / quantity / native stop | Mainnet side / quantity / native stop |
| --- | --- | --- |
| HEMIUSDT | long / 14131 / 0.007911 | long / 1161 / 0.007907 |
| INJUSDT | long / 59.5 / 5.804 | long / 4.6 / 5.805 |
| LINKUSDT | long / 53.9 / 11.997 | long / 4.2 / 11.971 |
| LTCUSDT | long / 21.4 / 49.18 | long / 1.7 / 49.14 |
| TAOUSDT | long / 3.031 / 231.82 | long / 0.248 / 232 |
| WLDUSDT | long / 852.7 / 0.4044 | long / 73.3 / 0.4055 |

| Sleeve ID | Mainnet | Demo | MEXC (running since 21:05:56 UTC) | Repository authority |
| --- | --- | --- | --- | --- |
| 0 | CARRY | CARRY | CARRY, entries off | `configs/lane2_carry_hold_v7.json` |
| 1 | LONG | LONG | LONG, entries on | `configs/long_native_v12.json` |
| 2 | EXODUS | EXODUS | EXODUS, entries off | `configs/lane2_exodus_short_v1.json` |
| 3 | MAKER, quoting disabled | PROBE, enabled | none | `deploy/engine.mainnet.toml.template`, `deploy/engine.demo.toml.template`, `deploy/engine.mexc.toml.template` |

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
