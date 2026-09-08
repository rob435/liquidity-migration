# Operational state

## Purpose

Record the latest verified host snapshot and the source of each operational setting.

## Spec Tables

| Observation | Value |
| --- | --- |
| Verified at | 2026-09-08 17:24:50 UTC services, PIDs, heartbeats, loaded images, failed units, disk and the mexc realm's units/config, read over SSH after the 17:23:13 `deploy-ok`; authenticated MEXC identity and two-scan flatness at 17:24 UTC. Worker readiness, rolling-loss and watchdog rows are the 16:54 UTC `diagnose` reading; Bybit positions, stops, WAL inventory and CARRY checkpoint rows are the 16:12–16:16 UTC read. Neither is re-read here |
| Evidence | [Deploy run `34255009973`](https://github.com/rob435/liquidity-migration/actions/runs/34255009973) (`512b1007`, `deploy-ok` 17:23:13 UTC) for what is installed; [diagnose run `34253894947`](https://github.com/rob435/liquidity-migration/actions/runs/34253894947) for the 16:54 reading; earlier [deploy run `34251758369`](https://github.com/rob435/liquidity-migration/actions/runs/34251758369) and [deploy run `34247719025`](https://github.com/rob435/liquidity-migration/actions/runs/34247719025); durable evidence root `~/SHARED_DATA/bybit_full_pit/reports/carry_daily_20260908/`: `host-after.json`, `account-{demo,mainnet}-after.json`, `state-after.json`, `release-verify.json`, `host-verification.json`, `daily-verification.json`, `qualification.json`, `deploy.log`; independent infrastructure receipts under `reports/infra_20260908/` |
| Host | `208.84.103.4` |
| Deployed commit | Completed generation and checkout are `512b1007d777cb153a97acd30d3026fbb7e28634`, deployed at 17:17:09 demo and 17:22:47 mainnet with `deploy-ok` at 17:23:13; the demo engine runs `releases/512b1007…/engine` and mainnet `bin/engine`, and the loaded image hashes are the table below, read at 17:25. Stored previous generation: `0e2827afe7fc784cb7c975c6a60c55c0c950bf56`. Later source and documentation commits are not evidence of installation |
| Source / installed boundary | The worker boot-repair reporting change `a167beee`, its two follow-ups and the MEXC realm `6773d221` are all inside the installed `512b1007`. The 17:23:13 verify table prints `mexc armed` and `mexc readiness=live-canary` and lists the four mexc units inactive |
| MEXC realm | `mexc_mainnet` is armed in `/etc/liquidity-migration/mexc-mainnet.env` and the installed engine reports it `live-canary`, so `liquidity-migration-engine-mexc`, `signal-worker-mexc` and `mexc-liveness` are installed, disabled and inactive; `engine-mexc.toml` is rendered with LONG entries on and CARRY/EXODUS entries off. Account `key-e3b03c8170d1fc6b`: identity bound and `flat=true samples=2 positions=0 open_orders=0` at 17:24 UTC; 52.6207 USDT equity at 17:05 UTC. No order has been sent; the canary is the owner's step and its receipt promotes the realm |
| Funded permission | Mainnet remains armed with sole leverage authority; configured CARRY/LONG/EXODUS entries remain enabled in both realms, with demo PROBE enabled. Both engines report `may_open=true`. Mainnet’s rolling-loss restriction remains active: 10.30624956 USDT loss against a 10 USDT limit; demo’s restriction is not tripped |
| CARRY holding | Both realms hold fixed quantity targets until the next daily decision; intraday funding, upcoming-book drop and pre-settlement exits are disabled. Current HEMI anchors remain demo `14131` / mainnet `1161`; native stops and explicit reductions remain active. The `FLOCKUSDT` exit tombstone for decision `1788825600000` remains intact; no new pre-settlement fires feed EXODUS |
| Runtime state | Engine PIDs demo `3378289` / mainnet `3379536`; worker PIDs `3378232` / `3379479`, all four from the 17:17/17:22 handovers. All four services are active with heartbeats under four seconds and zero restarts; `systemctl --failed` lists none. Both engines report `may_open=true` and `strategy_errors=[]`. Rolling-loss state is not re-read at 17:24; the 16:54 reading had mainnet `rolling_loss_tripped=true` with every sleeve `entries_enabled=false` and six positions per realm |
| Readiness boundary | No failed units at 17:24:50 and every Bybit manifest timer active in the 17:23:13 verify table; the mexc timer and units are inactive by design while the realm is `live-canary`. Worker `status=ready`, complete ticker coverage and the standing rolling-loss page are the 16:54 reading; the 70 host/account checks are the 16:16:53 observation. Earlier post-startup samples include bounded `recovering` states, including a mark-freshness lapse after gap repair. These are sampled observations, not uninterrupted-readiness or complete production-day proof |
| Execution | Embedded callbacks, default Bybit binary; both engines use `Type=notify`, `WatchdogSec=30s`. All four processes have zero swap. LONG uses 30-second PostOnly entry work in both realms; terminal order lookup and exact fill recovery precede an IOC remainder |
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
| Equity recorder | Rust `engine-tools record-equity` remains scheduled every minute; its 16:12:22 UTC job completes successfully after handover. Both engine heartbeats identify `cecff2e2` |
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

| Sleeve ID | Mainnet | Demo | MEXC (installed, stopped) | Repository authority |
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
