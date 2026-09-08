# Operational state

## Purpose

Record the latest verified host snapshot and the source of each operational setting.

## Spec Tables

| Observation | Value |
| --- | --- |
| Verified at | 2026-09-08 23:35 UTC over SSH: every engine, worker and liveness unit's state, enablement, PID and restart count, `systemctl --failed`, all three running engine heartbeats (`may_open`, `strategy_errors`), the hyperliquid realm's rendered files, directories and user, disk and swap; `engine venues` from the installed binary; the Hyperliquid account by the installed engine's read-only identity and inventory reads at 23:34 UTC. Bybit positions, stops, WAL inventory, worker readiness, rolling-loss, MEXC journal and wallet rows keep their 22:41 provenance and are not re-read here |
| Evidence | [Deploy run `34289829877`](https://github.com/rob435/liquidity-migration/actions/runs/34289829877) (`d00e82b2`, `deploy-ok` 23:33:01 UTC) for what is installed. Predecessors today: [run `34285402045`](https://github.com/rob435/liquidity-migration/actions/runs/34285402045) (`62234c95`, `deploy-ok` 22:39:02), [run `34282588846`](https://github.com/rob435/liquidity-migration/actions/runs/34282588846) (`3fd10dc4`, `deploy-ok` 22:06, mexc engine stopped by hand at 22:09), [run `34280348586`](https://github.com/rob435/liquidity-migration/actions/runs/34280348586) (`1c366b51`, failed at the mexc start), [run `34276974179`](https://github.com/rob435/liquidity-migration/actions/runs/34276974179) (`22794ad1`); durable evidence root `~/SHARED_DATA/bybit_full_pit/reports/carry_daily_20260908/`; independent infrastructure receipts under `reports/infra_20260908/` |
| Host | `208.84.103.4` |
| Deployed commit | Completed generation and checkout are `d00e82b253f3fe8060072c83b333a9841c8f2d6f`, `deploy-ok` 23:33:01 UTC; stored previous generation `62234c95ba7e1013614b5804bd7a5d65e31251f2`. All three running engines and workers were handed over in this run (engine tree changed: `hyperliquid` in the default features, the worker listing filter) at 23:26 demo, 23:31 mainnet, 23:32 mexc. The image-hash table below is the 22:41 reading of the previous generation and is not re-read. Later source and documentation commits are not evidence of installation |
| Source / installed boundary | The Hyperliquid realm (`a50007b5`) and the worker `universe.listed_on` filter (`d00e82b2`) are installed; the MEXC listing filter `d103108a` is pushed and not yet deployed, so `configs/signal-worker.mexc.json` on the host still carries its seven static exclusions. Every MEXC first-start repair is installed: never-run realm initialization (`22794ad1`), sorted contract rows and 510 as transport (`1c366b51`), the routine's envelope classifier (`adc97a18`), set-compare of checkpoint rows (`3fd10dc4`), request pacing, 600 s resync, unlisted-name admission and the worker exclusion (`62234c95`) |
| Hyperliquid realm | `hyperliquid_mainnet` is armed in `/etc/liquidity-migration/hyperliquid-mainnet.env` (master account address, approved API-wallet key, `REAL_MONEY=true`, Telegram trio defaulted from the demo pair by deploy) and the installed engine reports it `live-canary`, so `liquidity-migration-engine-hyperliquid`, `signal-worker-hyperliquid` and `hyperliquid-liveness` are installed, disabled and inactive; deploy printed `hyperliquid armed but the installed engine reports hyperliquid_mainnet readiness=live-canary: units stay stopped until the canary evidence promotes it`. `engine-hyperliquid.toml` is rendered with LONG entries on and CARRY/EXODUS entries off; `configs/signal-worker.hyperliquid.json` sets `universe.listed_on = "hyperliquid"`. Account `0xcef3…` (master; API wallet `0x396a…` named `test`, valid to 2027-03): `verify-account-identity` on the host prints `account-identity-ok`; `attest-flat` reports one `wallet_asset` blocker, 0.00230994 HYPE of spot dust, and no positions or orders. The account is in manual mode (`userAbstraction` = `disabled`, switched from `unifiedAccount` at 23:05 UTC by the owner); its 52.4 USDC sits in the `xyz` HIP-3 dex balance after a mis-directed transfer, and the main perps clearinghouse reads `accountValue 0.0`, so no order can rest until the owner moves it to Perps. No order has been sent; the canary is the owner's step and its receipt promotes the realm |
| MEXC realm | `mexc_mainnet` is armed and `live-proven`; `liquidity-migration-engine-mexc` (pid `3467643`), `signal-worker-mexc` (pid `3467585`) and `mexc-liveness.timer` are active and enabled since the 22:38:10 handover, zero restarts. The engine's heartbeat reads `may_open=true`, `strategy_errors=[]`, `stream_resets=0`, `orders_sent=0`, no positions; its journal since start holds eight WARN lines, seven of them the one-time `the venue does not list this instrument` notices, and no rate-limit or catalog line; the watchdog reads `ok scope=mexc units-and-heartbeats-healthy`. The venue's USDT futures wallet reads `equity 0`, `availableBalance 7.4e-9` at 22:40 UTC; it held 52.62 USDT at 17:05 UTC and nothing here traded, so no entry can size until the wallet is funded. `configs/signal-worker.mexc.json` excludes the seven Bybit names MEXC does not list |
| Funded permission | Mainnet remains armed with sole leverage authority; configured CARRY/LONG/EXODUS entries remain enabled in both Bybit realms, with demo PROBE enabled. Both Bybit engines report `may_open=true`. Both rolling-loss restrictions are tripped and every sleeve refuses entries under them: mainnet 13.31799509 USDT against a 10 USDT limit at 21:07 UTC, demo 178.95632852 against 161.012885862 at 20:42 UTC. `reduce_only` exits are admitted ahead of the check |
| CARRY holding | Both realms hold fixed quantity targets until the next daily decision; intraday funding, upcoming-book drop and pre-settlement exits are disabled. Current HEMI anchors remain demo `14131` / mainnet `1161`; native stops and explicit reductions remain active. The `FLOCKUSDT` exit tombstone for decision `1788825600000` remains intact; no new pre-settlement fires feed EXODUS |
| Runtime state | Engine PIDs demo `3482378` / mainnet `3483572` / mexc `3484134`; worker PIDs `3482320` / `3483514` / `3484075`, all from the 23:26–23:32 handovers. All six services active at 23:35 UTC with heartbeat ages under six seconds and zero restarts; `systemctl --failed` lists none; swap 0. All three engines report `may_open=true` and `strategy_errors=[]`. Position counts are the 22:41 reading: Bybit engines four each, mexc none |
| Readiness boundary | No failed units at 23:35 UTC; every Bybit manifest timer and the mexc timer active and enabled, the hyperliquid timer and units inactive and disabled by design while the realm is `live-canary`. The mexc realm has run under `62234c95` since 22:38:19 with its watchdog healthy; earlier the same evening it was stopped by hand three times (21:10, 21:46, 22:09) for the faults recorded in CHANGELOG. Worker readiness, ticker coverage and the rolling-loss pages are earlier readings. These are sampled observations, not uninterrupted-readiness or complete production-day proof |
| Execution | Embedded callbacks; every engine unit uses `Type=notify`, `WatchdogSec=30s`. Host swap is 0 at 23:35. LONG uses 30-second PostOnly entry work in every realm; terminal order lookup and exact fill recovery precede an IOC remainder |
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
| Final engine, loaded in all three realms | `6b280e5119efc98ebf8675de1a00c2aab8394c9b11d68c36103114f6552d15df` |
| Final signal worker, loaded in all three realms | `713bb9cd9118b3f6a91d2457392ac06901b5059246b020f3faa826fc6ab027aa` |
| Final engine tools, installed | `25596636f1325adaded21ac53da87ff252c3830737f4d0af01888e4950f97549` |
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

| Sleeve ID | Mainnet | Demo | MEXC (running since 21:05:56 UTC) | Hyperliquid (installed, stopped) | Repository authority |
| --- | --- | --- | --- | --- | --- |
| 0 | CARRY | CARRY | CARRY, entries off | CARRY, entries off | `configs/lane2_carry_hold_v7.json` |
| 1 | LONG | LONG | LONG, entries on | LONG, entries on | `configs/long_native_v12.json` |
| 2 | EXODUS | EXODUS | EXODUS, entries off | EXODUS, entries off | `configs/lane2_exodus_short_v1.json` |
| 3 | MAKER, quoting disabled | PROBE, enabled | none | none | `deploy/engine.mainnet.toml.template`, `deploy/engine.demo.toml.template`, `deploy/engine.mexc.toml.template`, `deploy/engine.hyperliquid.toml.template` |

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
