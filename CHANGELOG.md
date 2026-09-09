# Changelog

The dated operational log: deploys, incidents, repairs, and change points,
newest first. One entry per change or per first report of a fault, updated in
place when the same matter moves on; a refused deploy, a re-fire of a known
incident, or a check that changed nothing gets no entry. Current truth lives
in [STATE.md](STATE.md); when something happens, add the dated entry here and
edit STATE.md to match.

Older history: Retained in git history (pre-September 6).

- **2026-09-09 — Audit deferred items completed: restart markouts (F20), opportunity cohort (F16), production day reconciliation (F17), and research standards (F18, F19, F21).**
  - **F20, owed markouts**: `Fills.pending` restated at rotation as `SegmentBase.owed_markouts` (`engine-types/src/wal.rs`, `#[serde(default)]`) and rebuilt at boot for fills younger than 305 s. `filled_ns` signed stamp reconstructed from venue `fill_ts_ms` against wall clock. Fills report splits late marks into restart-gap and stall. 10 execution/fill-cost tests pass; `engine sim --twice` passes on seeds 7, 26, 31, 42, 166.
  - **F16, opportunity cohort**: Added `engine-tools cohort --wal PATH [--json]` (`engine-tools/src/cohort.rs`). Tracks source rows keyed by `(destination, source, sequence, observation_id)` and order intents by `intent` with settle status. Reports latencies at p50/p90/p99/p99.9. Mainnet 2026-09-08 census (segments 000064–000074): 52 source rows consumed, 11 intents (9 sent, 2 refused StaleQuote), decision-to-wire p50 0.755 ms across 114,809 records.
  - **F17, production day reconciliation**: Implemented `python -m liquidity_migration.research.day_reconciliation` bounding samples between midnight readings against venue transaction logs and copied WAL segments. Same-ms venue rows chained by `cashBalance - change`. Equity recorder (`engine-tools/src/equity_recorder.rs`) updated to write `unrealised_pnl_usdt`, `wallet_cash_usdt`, and `positions`. Mainnet 2026-09-08 reconciled with 0 cash residual.
  - **F18, F19, F21, research standards**: Registered maker experiment `lane2_toxic_flow_quoter_v1` (net -0.171 bp vs -0.248 bp control), execution comparison standard (fixed cohort, decision-time benchmark, unfilled quantity at common horizon), and cross-venue signal standard in [governance.md](docs/research/governance.md). Added rules #36 and #37 to `docs/research/backtesting_errors_we_never_repeat.md`.
  - **Deferred**: F13/F14 fleet capital coordinator deferred per Milestone E sequence pending alt-realm qualification.
  - **Deploy receipt**: Commit `e899ea21` deployed via run `34412394718` at 22:28:33 UTC. Both Bybit engines active on host (`may_open=true`, `strategy_errors=[]`); all watchdogs healthy.

- **2026-09-09 — `floor_usdt` removed from capital reference on owner direction.**
  - Capital reference and derived caps follow verified equity down; 100 USDT in profile serves only as scale reference.
  - Removed `EnvelopeConfig::floor_usdt`, `Envelope::viable_for_new_exposure()`, and `require_viable_reference()` (`engine-risk/src/{config,envelope,kernel}.rs`). `capital_reference.floor_usdt` removed from profile schema (`operational_profile.py`, `real_money_profile.py`, `configs/operational.json` sha256 `0dd6be5a...`).
  - Retained `DenyReason::LossGuardTripped { equity_usdt, floor_usdt }` in `engine-types/src/risk.rs` for log reader backward compatibility.
  - Verified by contracts in `engine-risk/tests/contracts/envelope.rs` and `tests/research/backtest/test_long_live_physics.py`.

- **2026-09-09 — `engine sim` heavy sweep: fixed three replay and accounting discrepancies.**
  - **Root cause 1**: Attribution fill reducers diverged for `amounts: None` fills. Replay (`commit_prepared`) dropped sub-FLAT dust rows, while live path (`commit_portfolio_fill`) retained them, causing sleeve-stop validation failures on seed 7. Fixed by sharing `commit_prepared` legacy tail in `commit_portfolio_fill`.
  - **Root cause 2 & 3**: Recorded allocations over binary64 fills without `ExecutionAmounts` were treated as exact quantities by `commit_portfolio_fill`, `Lots::on_fill_with_economics`, and `position_state_with_adoption`. Boot grid adoption left a floating-point residual (`1/180143985094819840`), causing FIFO rederivation failures (seed 42) and ledger mismatches (seed 166). Fixed by treating binary64 quantities as legacy regardless of durable allocation and settling sub-FLAT residues.
  - Verified: Seeds 1–44, 123, 166, 240, 243, 247 pass `--seconds 300 --symbols 2 --crashes 2 --faults heavy --twice` with byte-for-byte replay agreement. No funded log contains `amounts: None` fills.

- **2026-09-09 — Source audit first hardening batch (commits `98a4be7a`, `4c531869`, `1e2cfc22`, `f6620ff9`).**
  - **F05, asynchronous leverage dispatch** (`98a4be7a`): Removed synchronous `set_leverage` from account owner loop. Leverages dispatched asynchronously via `VenueClient::dispatch_leverage` → `MutationCompletion::Leverage`. Orders with `exact_terms` require exact cumulative fill equality to retire.
  - **F11, equity contraction** (`4c531869`): Reference follows verified equity down (superseded by removal of `floor_usdt`).
  - **MEXC hardening** (`1e2cfc22`):
    - *F01, account binding*: Registry at `/etc/liquidity-migration/mexc-account-bindings.json` validates `uid-<account_uid>` identity before socket creation or mutation.
    - *F04, classified rate pacing*: `mexc/rest.rs` pacer reserves 4 protective slots out of 16 for emergency order cancels and stop reductions.
    - *F06/F07/F08/F24*: Integer leverage validation, separate opening eligibility from cleanup, and removed unproven `live-proven` claim from MEXC.
  - **F10, archive qualification** (`f6620ff9`): Deployable archives enforce embedded qualification evidence for candidate binaries.
  - **F02, F03, F09, F12, F15**: Pre-wire authority validation, priority queue draining in `venue_runtime`, capability matrix readiness, stop charge naming, and backtest evidence metadata.

- **2026-09-09 — Host storage reclamation and sealed WAL pruning.**
  - Implemented `scripts/runtime/reclaim_host_storage.py` running hourly as root systemd service.
  - Sealed WAL segments below `engine_wal::retention_floor` pruned once verified against Google Drive backup manifests under `engine-state/sealed/`. Pruning budgets free space via `statvfs` against a 25 GB reserve floor. Old release packages, temporary files, and apt caches cleaned automatically.
  - Deploy receipt: Installed on host under `42dd7446`. First run reclaimed ~3.2 GB of disk space.

- **2026-09-09 — Manifest-driven realm plumbing via `deploy/realms.tsv`.**
  - Unified realm configuration into `deploy/realms.tsv` (# realm-table-v1) defining realm properties, venue mappings, modes, and enabled states.
  - Templated generation of systemd unit files, environment configurations, and Grafana dashboard (`render_dashboard.py`). `deploy/lib_sleeves.sh` dynamically reads realm table.
  - MEXC and Hyperliquid realms held provisioned but stopped (`live-canary`).
  - Deploy receipt: Commit `42dd7446` deployed via run `34368381679`. Verified by 19 tests in `tests/policy/test_realms.py`.

- **2026-09-09 — Hyperliquid canary run 1 fixes in `engine-tools/src/canary.rs`.**
  - Resolved three canary defects: quantized order prices to venue tick size and 5 significant figures; handled unacknowledged execution branches; switched order cleanup from cloid queries to address-based order status checks to prevent `unknownOid` retry loops.
  - Deploy receipt: Installed in `42dd7446`. Tested by five unit tests in `canary.rs`.

- **2026-09-09 — Incident `mexc-signal-intake`: unlisted symbol batch froze signal lane.**
  - Root cause: Signal batch containing seven instruments unlisted on MEXC blocked `queue_signal_observation` indefinitely because unlisted names never produced catalog entries.
  - Fix in `engine-core`: Symbols missing from venue catalog with no open orders or inventory are dropped with a warning instead of blocking signal intake.
  - Deploy receipt: Deployed in `42dd7446`.

- **2026-09-09 — Signal worker modular public data sources (`PublicVenueKind`).**
  - Modularized signal worker venue fetching into `engine/signal-worker/src/venue/` (`bybit.rs`, `mexc.rs`, `hyperliquid.rs`).
  - MEXC and Hyperliquid workers stream native tickers, klines, and funding rates directly from venue APIs rather than proxying Bybit data.
  - Deploy receipt: Deployed in `42dd7446`. Verified by 225 signal worker tests.

- **2026-09-09 — MEXC market feed funding clock alignment.**
  - `engine-marketdata/src/mexc.rs` updated to read contract declared funding intervals and next settlement timestamps directly instead of assuming standard 8-hour UTC epochs.

- **2026-09-09 — Incident `host-22826ce0bb838311`: MEXC signal worker LONG lane degraded by spool cap.**
  - Root cause: MEXC signal worker `current` spool class reached the 8-file cap (`CURRENT_SPOOL_FILE_CAP = 8`), refusing `LongWatermark` commits silently while `CarryWatermark` coalesced into existing paths. Heartbeat published aggregate `spool_backpressured=false`, hiding the blocked class.
  - Fix: `check_fleet_liveness.py` updated to extract `spool_backpressured_classes` and name blocked classes in alerts. Extended diagnose workflow digest to report per-class spool file and byte counts. Handover in `7f0214f` restarted MEXC processes and cleared the stall.

- **2026-09-09 — Incident `host-51b05439c4f09794`: Telegram alert failures obscured by missing error details.**
  - Root cause: Watchdog Telegram alerts failed with HTTP 400 `PEER_ID_INVALID` due to an invalid target chat ID. Generic HTTP error handling dropped the response JSON `description`. Watchdog also printed healthy sign-off text while exiting 1 on delivery error.
  - Fix: `liquidity_migration/ops/telegram.py` parses and logs redacted venue error description from response JSON. Liveness runner suppresses healthy sign-off when alert routing fails.

- **2026-09-09 — Incident `mexc-a361f5d18861421a`: false CRITICAL pages during 600 s private stream resync.**
  - Root cause: MEXC private stream 600 s resync performs sequential history sweep across followed symbols, setting `may_open=false` during the 10–30 s sweep window. The 30 s watchdog sampled during this transient state and paged CRITICAL 8 times.
  - Fix: Separated operator latch (`operator_may_open`) from private stream readiness (`private_stream_ready`) in engine telemetry. Watchdog only alerts if private stream stays unready past 180 s (`private_stream_unready_ms > 180_000`). Registered `private-stream:` alert reference in diagnose and watchdog tables.
  - Deploy receipt: Deployed in `d835b62`. Resync at 03:50:51 UTC verified clean with no alert.

- **2026-09-09 — Incidents `demo-0922e9f30da3bf98`, `mainnet-014ec4a90a2fde5f`, `mexc-d62940e951288d4c`: false CARRY freshness page at UTC roll.**
  - Root cause: Signal workers wait 5–8 minutes at 00:00 UTC for venue funding settlement. Liveness check evaluated CARRY freshness against a flat 180 s threshold from last completion, ignoring the scheduled roll frontier.
  - Fix: Freshness evaluated relative to `carry_cycle_not_before_wall_ts_ms` published by worker, measuring lateness only after the cycle is actually due.
  - Deploy receipt: Deployed in `beef5bc5`.

- **2026-09-08 — Incident `mexc-a361f5d18861421a`: MEXC recovery sweep stalled on rowless history.**
  - Root cause: `RecoveryClient::executions` treated an empty execution history page as lack of forward progress, aborting recovery. Liveness diagnostic also lacked MEXC systemd unit mapping.
  - Fix: Recovery sweep advances progress counter per answered page regardless of row count. Added MEXC unit mappings to diagnose tool.
  - Deploy receipt: Deployed in `62234c95` via run `34291380453`.

- **2026-09-08 — Filter signal worker universe to venue listed symbols.**
  - Added `universe.listed_on` filter in `signal-worker` config to exclude instruments not listed on the engine venue (e.g. Hyperliquid), preventing unlisted symbols from clogging queue.

- **2026-09-08 — Wire Hyperliquid perpetuals as fourth realm.**
  - Added Hyperliquid venue adapter, private stream feed, and signal worker configs. Deployed via run `34289829877`. Held in stopped `live-canary` posture.

- **2026-09-08 — Reclassify rolling-loss restriction as `NOTICE`.**
  - Downgraded `rolling-loss:<unit>` alert from `CRITICAL` to `NOTICE` in `check_fleet_liveness.py`, as envelope loss restriction is an expected risk constraint rather than an infrastructure fault.

- **2026-09-08 — Incident `mexc-first-start`: catalog sort mismatch and REST rate-limit latch.**
  - Root cause: `rules()` and `symbol_pairs()` returned unsorted maps, failing symbol admission checks; resulting rapid re-fetch loops triggered MEXC REST rate limits (HTTP 429/510).
  - Fix: Symbol pairs sorted alphabetically in catalog loaders; added shared rolling-window request pacer in MEXC REST client. Deployed in runs `34282588846` and `34285402045`.

- **2026-09-08 — Wire MEXC USDT perpetuals as third realm.**
  - Integrated MEXC gateway, websocket private order feed, and signal worker. Verified via owner canary order execution (receipt `32f27d4b` at 20:16:27 UTC). Deployed via run `34255009973`.

- **2026-09-08 — Worked-entry test timing fix.**
  - Isolated test clock to avoid wall-clock dependency in 1 ms reprice gate test.

- **2026-09-08 — Incident `mainnet-014ec4a90a2fde5f`: signal worker boot repair page.**
  - Root cause: Cold-start signal worker historical kline backfill exceeded 180 s liveness threshold before finishing catchup.
  - Fix: Added `boot_repair_acceptable` allowing up to 600 s for initial cold-start catchup before paging CRITICAL. Deployed in run `34251758369`.

- **2026-09-08 — Incident `host-51b05439c4f09794`: failed handover left realm timers disabled.**
  - Root cause: Handover script disabled realm systemd timers during deployment; an aborted deployment left watchdog and study timers inactive.
  - Fix: Added `restore_realm_timers` trap to re-enable timers on deployment failure or abort.

- **2026-09-08 — Incident `mainnet-ac90e31c207bc0da`: venue timeout latched funded openings.**
  - Root cause: Bybit REST timeouts during market volatility tripped the engine order recovery latch (`may_open=false`).
  - Fix: Enhanced transient network error handling and retry logic in `bybit/execution.rs`; added explicit journal logging for latch triggers.

- **2026-09-08 — Restore CARRY daily holding on owner direction.**
  - Disabled intraday funding and pre-settlement exits; restored daily holding model with 24-hour quantity persistence. Deployed in `34247719025`.

- **2026-09-08 — Repair execution recovery, account limits, and research costs.**
  - Updated research fee defaults (`configs/bybit_fee_rates.json`). Configured 30 s PostOnly rest for LONG entries. Added 100 bp mark collar on orders. Isolated Bybit book sequence gap recovery to affected L1/L50 topic. Deployed `cecff2e2` via run `34247719025`.

- **2026-09-08 — Work LONG demo entries for 30 s and recover resting entries across restart.**
  - Maintained working-order supervisor across process restarts; demo entries join near touch for 30 s. Deployed `441811eb` via run `34213632476`.

- **2026-09-08 — Check clock units without assuming sub-millisecond scheduling.**
  - Local push gate clock assertions adjusted for variable CI scheduling granularity.

- **2026-09-08 — Measure one-sided execution against actual order intentions.**
  - Added `engine-tools execution-study` command and 15-minute scheduled timer. Compares decision-time benchmarks, fill prices, and slippage. Deployed `06220e56`.

- **2026-09-07 22:56 UTC — Incident `host-681737fd16e1f806`: capture-disk page at 25 GB reserve floor.**
  - Root cause: Host disk space reached 25 GB reserve floor due to accumulated `/var/lib/liquidity-migration/backup` (31.8 GiB) and build artifacts, triggering market tape backpressure.
  - Fix: Added directory disk reporting to `deploy_remote.sh::report_disk_usage`. Implemented backup directory retention and link cleanup. Fixed cross-device link error (`EXDEV`) in storage reclaimer. Deployed in `f1fbe34` and run `34173874312`.

- **2026-09-07 — Historical source adapters and explicit sparse-data execution.**
  - Decoupled recorder decoding from book reconstruction; added trade and bar models with explicit fill timing; verified Bybit CSV and Parquet normalization equivalence.

- **2026-09-07 — Current-WAL accounting and sustained resource qualification.**
  - Reconstructed complete Sep 6 USDT linear cash and execution accounting. Validated WAL v7 rotation across segment boundaries.

- **2026-09-07 — Remove obsolete audits and Python runtime code.**
  - Deleted four obsolete Tier-1 audit docs; removed unused Bybit REST limiter and sleeve file rewriters; moved equity sampling to `engine-tools`.

- **2026-09-06 — Round-3 embedded execution.**
  - Replaced child strategy subprocesses with embedded Rust execution in `engine-core`.
  - Combined checkpoint, intent, verdict, and order dispatch into single main event loop with durable barriers.
  - Introduced WAL v7 format (50 writable kinds, backward-compatible readers) and offline `wal-convert-v5` tool.
  - Enforced exact decimal instrument constraints; removed Python state importers; optimized stop-loss maintenance and portfolio routing paths.
  - Deployed commits `8c92c964` (Stage A) and `937ba60d` (Stage B) via workflows `34114063829` and `34119441979`.

- **2026-09-06 — Round-2 runtime cleanup and integration.**
  - Separated runtime crates into `engine`, `engine-tools`, and `signal-worker`.
  - Fixed in-flight quantity tracking, single callback decoding, and separated risk admission timestamp from decision timestamp.
  - Implemented fair event-loop rotation across market, timer, maintenance, signal, and control lanes.
  - Deployed commits `8f96e603`, `93404ff6`, and `bb4bc3d3` via workflows `34043450919` and `34055716541`.

- **2026-09-06 08:07 UTC — Worker recovery, recorder finalization, and rollback repair.**
  - Retained full daily CARRY scorer windows across hourly source pruning. Fixed quiet recorder symbol hourly finalization under continuous traffic.
  - Hardened automated rollback to preserve candidate binaries unless runtime inputs match. Refreshed Google OAuth credentials for backup and tape sync. Retired legacy signal sources.
  - Deployed `420c7347`.

- **2026-09-06 — Tier-1 exact ownership and recovery qualification.**
  - Implemented 42 audit items: exact sleeve exits, account/risk quantities, bounded terminal order cache, and disk-sorted execution history fold. Passed 2,300 debug and release tests.
