# Changelog

The dated operational log: deploys, incidents, repairs, and change points,
newest first. One entry per change or per first report of a fault, updated in
place when the same matter moves on; a refused deploy, a re-fire of a known
incident, or a check that changed nothing gets no entry. Current truth lives
in [STATE.md](STATE.md); when something happens, add the dated entry here and
edit STATE.md to match.

Older history: Retained in git history (pre-September 6).

- **2026-09-10 — Owner direction: Bybit mainnet stopped, Bybit demo opens nothing, MEXC and Hyperliquid start as a forward test. The `live-canary` boot refusal is gone; readiness stays derived from the matrix and the boot log names what is unproven.**
  - Owner direction 11:50 UTC: stop the Bybit demo and mainnet trading, start mexc and hyperliquid as forward testing to capture all errors and fix them.
  - `deploy/realms.tsv`: `mainnet posture=stopped` (the deploy stops and disables its engine, worker, liveness timer and the `execution-study` pair; `REAL_MONEY` stays armed and nothing is flattened: the INJUSDT long 4.6 with its native stop 5.805 stays on the venue, unmanaged); `demo` entries `false|false|false` (the practice realm stays `running` for the deploy soak, manages its INJUSDT 59.5 long's exit and keeps PROBE); `mexc` and `hyperliquid` `posture=running`. No generated file changed.
  - `engine/engine-public/src/registry.rs`: `VenueReadiness::permits_engine_run` accepts `LiveCanary`; `production-blocked` and `read-only` still refuse, and readiness is still derived from the capability row, never assigned. `engine/engine-core/src/runner.rs` logs `WARN forward test` at boot with the unproven capabilities. `scripts/vps/deploy_remote.sh` `realm_run_ready` accepts `live-proven|live-canary`; the unready line reads `units stay stopped, the engine refuses to run at that readiness`. `scripts/ops.sh` `CANARY_REALMS` is the practice realm plus every funded realm on a venue with no practice sibling, no longer read from posture.
  - Canaries on the installed `6e5ca627`, both PASS. MEXC 11:59:17–11:59:29 UTC: `client_id=lmcan-1a08b2fd4a5-087e-0000 venue_order_id=853000482766018560`, `order_px=77462.1 qty=0.0001 stop=65842.8` under `bid=77851.4 ask=77851.5`, `create=accepted`, `private_order=New`, `cancel=accepted`, `private_order=Cancelled`, `order_status=Cancelled cumulative_filled_qty=0`, four clean scans. Hyperliquid 11:59:20–11:59:28 UTC: `client_id=lmcan-1a08b2fcd82-08b6-0000 venue_order_id=541177774027`, `order_px=77460 qty=0.00014 stop=65841` under `bid=77850 ask=77851`, the same lifecycle. Both matrix rows carry `observed 2026-09-10` for submit, cancel and post-only; fill-attribution, protection-place, protection-trigger and reconnect-history-recovery stay `implemented`, so both realms remain `live-canary` and the forward test is what gathers them.
  - Followers of the table: `deploy/grafana/liquidity-migration-fleet.json` re-rendered (the realm variable narrows to `demo|mexc|hyperliquid`); the Telegram fleet status line for a funded realm with no owner heartbeat reads `no heartbeat, entry state unknown` instead of `not armed`, because a stopped realm can still be armed.
  - Tests: `a_submit_cancel_canary_does_not_qualify_general_protected_position_trading`, `the_alt_realms_owe_what_a_submit_cancel_canary_cannot_show` (engine-public); `production_readiness_is_explicit_for_every_registered_realm`, `a_live_canary_realm_takes_the_canary_and_runs_as_the_owners_forward_test`, `conformance_local_fixtures_do_not_promote_dormant_realms` (engine-venue); `only_the_practice_realm_and_a_live_canary_realm_can_reach_the_command` (engine-tools); `test_a_funded_handover_waits_for_a_readiness_the_engine_will_run`, `test_posture_drives_the_deploy_and_the_table_names_the_realms_that_run`, `test_canary_order_keeps_the_arming_switch_and_refuses_the_funded_bybit_account`.
  - The Mac's disk, not the host's: the pre-push gate hit ENOSPC on 7 GiB of headroom because `engine/target/debug/deps` held 95.2 GiB across 236,051 files, almost all the ten workspace crates' own debug artifacts from repeated gates (third-party dependency builds were 2.2 GB); the host's hourly `storage-reclaim` never covered the Mac. `scripts/dev.sh prune` now runs `cargo clean --profile dev -p` for every workspace member and drops `target/debug/incremental`, and `check` runs it first when the target volume has under `LM_TARGET_FREE_GIB` (30) GiB free. Tests `test_dev_prune_cleans_each_workspace_member_and_keeps_dependency_builds`, `test_dev_check_prunes_under_the_target_volume_floor_before_the_rust_stages`.
  - [Deploy run `34477667791`](https://github.com/rob435/liquidity-migration/actions/runs/34477667791), dispatched 12:36:24 UTC on `e7f3ca52`: `ci` 12:38:52, `rust` 12:47:27, release artifact 12:50:25, `vps` 12:50:29–12:57:01 **failed**. Demo handed over to `e7f3ca52` and soaked 300 s (12:51:41–12:56:41, the rolling-loss NOTICE only); `mainnet posture=stopped in deploy/realms.tsv: units stay stopped` at 12:56:44 (engine, worker, liveness timer and `execution-study` pair disabled and inactive); then `atomic mexc handover` 12:56:45 failed at 12:56:59 with `mexc native checkpoint configuration change is incompatible`. Rollback refused (`6e5ca627` has different runtime inputs); `e7f3ca52` stayed installed for a forward repair, `deployed-commit` stayed `6e5ca627`; `mexc-liveness.timer` was re-enabled by `restore_realm_timers` and has paged CRITICAL on the stopped realm since 12:57:41 (Telegram on the 30 min cooldown; the incident routine answered `Routine is paused`). Hyperliquid was never reached. Incident `mexc` realm down, open from 12:56:59 UTC.
  - Root cause, two halves. (1) `ensure_native_strategy_state` offered the last deployed commit's retained render (`checkpoint-configs/6e5ca627/engine.mexc.toml`) as the config the realm's state was written under; the realm last ran on `7f0214f7` (2026-09-09 08:52–15:27 UTC) and sat stopped through five later deploys that each re-rendered it, so the engine answered `checkpoint identity (1, "a9efbf93…") does not match configured (1, "32814936…")`. (2) With the right render (`7f0214f7`'s) the rebind still refused, `checkpoint rebind changes "carry" decision rules`: the mexc worker moved to MEXC's own public data on 2026-09-09 (`42dd7446`), which changed the carry `feature_contract_sha256` (`d9a96b4c…` → `32246961…`), part of CARRY's decision identity, while the realm was stopped. The carry sleeve on MEXC holds no exposure, no order in flight and no callback work (heartbeat 2026-09-09 15:27: `positions []`, `working_entries []`, `fills 0`), so there was nothing the old checkpoint protected.
  - Fix. `engine-tools/src/takeover/rebind.rs`: `compatible_config` answers whether the rules changed instead of refusing; a sleeve whose rules changed is re-initialised with its strategy's canonical initial checkpoint under the new identity when the replayed log attributes it nothing (`Attribution::rows`, `LedgerOfOrders` in-flight orders, `CallbackState` committed processes and queued inputs), printed as `reinitialized "carry": decision rules changed while it held no exposure, working orders or callback work`; one that holds anything is refused with `changes … decision rules while it holds exposure or pending work`; a wider EXODUS stop stays refused. `scripts/vps/deploy_remote.sh` `ensure_native_strategy_state` tries every retained render (`retained_realm_configs`: the deployed commit's first, then newest first) with a dry-run rebind and executes the first the engine accepts, printing `result=rebound previous=PATH`. Tests: `a_rule_change_on_a_sleeve_that_holds_nothing_reinitialises_its_checkpoint`, `a_rule_change_on_a_sleeve_with_exposure_or_pending_work_is_refused`, `rebind_refuses_unknown_source_identities_and_a_reordered_sleeve_table` (engine-tools); `test_native_rebind_tries_every_retained_render_and_keeps_the_one_the_engine_accepts`, `test_native_rebind_uses_retained_config_without_initializing_state` (deploy). Verified before the deploy on a copy of the realm's WAL with the new binary: the `6e5ca627` render is refused on identity; the `7f0214f7` render re-initialises carry and long (LONG's `feature_contract_sha256` moved with the same worker change, `1b9f6fc2…` → `2d80f005…`, and the old binary had stopped at carry) and reports `compatible checkpoint rebinds 2`; the installed `e7f3ca52` binary refuses the same render on the rule change. The log holds 1,472 records and no fill at all (`engine-tools fills`: `nothing has filled yet`), so neither fresh checkpoint discards a cooldown, an anchor or a holding.
  - [Deploy run `34482182228`](https://github.com/rob435/liquidity-migration/actions/runs/34482182228), dispatched 13:21:56 UTC on `ceac3c9b`: `ci` 13:24:39, `rust` 13:33:41, release artifact 13:37:23, `vps` 13:37:28–13:48:40 **failed**, one step further. Demo handed over and soaked 300 s; mainnet stayed stopped; the mexc handover's `ensure_native_strategy_state` walked the retained renders, the `7f0214f7` render re-initialised carry and long (`reinitialized "carry"`, `reinitialized "long"`, `compatible checkpoint rebinds 2`, `native-state-ok realm=mexc result=rebound previous=…/7f0214f7…/engine.mexc.toml` at 13:44:54) and the mexc engine started on `ceac3c9b` (pid `4032860`, `WARN forward test … unproven="fill-attribution, protection-place, protection-trigger, reconnect-history-recovery"`, `may_open=true`, `private_stream_ready=true`, lease taken 13:45:01). The mexc signal worker then exited five times in 40 s with `signal-worker: state: checkpoint public source contract has drifted; a new cold start is required` (exit 2) until systemd stopped restarting it at 13:45:33, and the deploy failed at 13:48:37 with `liquidity-migration-signal-worker-mexc.service restarts after each heartbeat`. Rollback refused again; `ceac3c9b` stayed installed; the mexc engine kept running with no signal source; Hyperliquid was not reached.
  - Root cause, the worker's half of the same 2026-09-09 change: `/var/lib/liquidity-migration-signal-worker-mexc/checkpoint.json` (74 MB, 2026-09-09 15:01 UTC) and `hot-input-journal.jsonl` were written under the Bybit public-source contract; `SignalWorker::restore` refuses a checkpoint whose `source_contract_sha256` is not the config's, and nothing performed the cold start it asked for, so the unit crash-looped. Fix in `engine/signal-worker/src/worker.rs`: `DurableSignalWorker::open_with_universe` archives a checkpoint whose contract drifted, with the journal and both pending files, into `<state_dir>/drifted-source-<sha8>-<unix_ms>/`, prints `checkpoint public source contract drifted from … to …; archived … and rebuilding the history under producer generation …`, and seeds a fresh checkpoint that keeps the producer identity the engine's registry holds (`source_generation`, `signal_lifecycle` epoch, both destinations, `last_input_sequence`, both output sequences) while the history, coverage and features start over. Test `a_checkpoint_from_another_public_source_is_archived_and_the_worker_cold_starts` (fails on the unfixed worker with the exit-2 refusal).
  - The first cut of that fix (`1d526b8d`, [deploy run `34486402411`](https://github.com/rob435/liquidity-migration/actions/runs/34486402411), `deploy-ok` 14:22:40 UTC) minted a random generation at the cold start. The mexc worker then sat at `waiting for engine destination registry: state: successor grant does not follow the durable producer seal`, `status=starting`, universe empty, and the 173 old spool files untouched: the engine's request still named `ff7ad772…` at epoch 1 as the active unsealed producer, `plan_producer_report` has no path to adopt an unknown generation while that one is unsealed, and the worker refuses to answer a grant that is not its own, so neither side could move. Repair on the host before the next handover: the mexc worker stopped, its random-generation checkpoint moved aside, and the archived `ff7ad772…` checkpoint restored to `checkpoint.json` so the corrected binary rebuilds under the identity the registry knows.
  - Hyperliquid, first minutes on `1d526b8d` (engine pid `4048771`, up 14:22:14 UTC, `may_open=true`, `private_stream_ready=true`, equity 52.4 USDC, no position): the worker's first universe refresh logged `universe refresh left a sleeve with no eligible symbol` and its retry a few seconds later succeeded (`status=ready`, 178 listed symbols, 2 LONG and 4 CARRY eligible, ticker coverage complete, carry outputs flowing). The engine then reported `strategy_errors: carry: CARRY decision universe has fewer than 50 symbols` on every carry batch, and `hyperliquid-liveness` paged CRITICAL on it from 14:31:30 UTC every 30 s (Telegram on the 30 min cooldown; routine paused). Root cause: `native_carry/scorer.rs` refuses a decision universe under `MIN_DECISION_SYMBOLS` (50) as an error, and the plug records it as a strategy fault, even for a sleeve whose entries are rendered off and that holds nothing, which is CARRY on both alt realms by design. Fix: `plan.rs` `reduce_signal` and `reduce_scorer_catchup` consume the batch without a decision when the universe is thin, entries are off and the sleeve holds no exposure, no working order and no opening order (`Skipped::DecisionUniverseBelowMinimum { symbol: "*", symbols }`, shown in the heartbeat's `entry_blockers` as `decision_universe_below_minimum`); with entries on or anything held the scorer's refusal stands. `ScorerCatchupInput` gains `holds_nothing`. Test `a_thin_decision_universe_is_consumed_without_a_decision_only_when_off_and_flat`. LONG's 2 eligible symbols are the cold start's kline backfill (`cold_start_lookback_days` 100 across 178 symbols) and are re-read after the backfill.
  - [Deploy run `34490257039`](https://github.com/rob435/liquidity-migration/actions/runs/34490257039) on `8d9915aa` (the identity-preserving cold start) was cancelled at 14:48 UTC before its host job started, so the CARRY fix below could ship in one handover instead of two.
  - Deploy receipt: [run `34491454825`](https://github.com/rob435/liquidity-migration/actions/runs/34491454825), dispatched 14:48:21 UTC on `859723b1`: `ci` 14:50:45, `rust` 14:59:34, release artifact 14:59:54, `vps` 14:59:59–15:07:57. Demo handed over (engine pid `4062319`, worker `4062227`) and soaked 300 s to 15:06:09; `mainnet posture=stopped in deploy/realms.tsv: units stay stopped` 15:06:13; mexc handover 15:06:14, `native-state-ok realm=mexc result=already-complete`, worker pid `4065991` (`checkpoint public source contract drifted from 37a18797… to 68e59719…; archived …/drifted-source-37a18797-1789052783211 and rebuilding the history under producer generation ff7ad772…`), engine pid `4066050`; hyperliquid handover 15:07:19, `native-state-ok realm=hyperliquid result=already-complete`, worker pid `4066983`, engine pid `4067043`; `deploy-ok commit=859723b1…` 15:07:46, `rollback-target 1d526b8d…`, `mainnet readiness=live-proven`, `mexc readiness=live-canary`, `hyperliquid readiness=live-canary`.
  - Verified on the host 15:09 UTC: `deployed-commit` `859723b1`; every fleet timer and both recorders `enabled active`; mainnet engine, worker, liveness timer and `execution-study` pair `disabled inactive`; `systemctl --failed` empty; watchdogs `ok scope=demo warnings-present-no-critical`, `ok scope=mexc units-and-heartbeats-healthy`, `ok scope=hyperliquid units-and-heartbeats-healthy`, `ok scope=host units-and-heartbeats-healthy`. MEXC: engine `may_open=true`, `private_stream_ready=true`, `strategy_errors=[]`, equity 30 USDT, no position, LONG entries on; the engine drained the 173 stale spool files (7 unlisted names dropped: `1000PEPEUSDT`, `ARCUSDT`, `FILUSDT`, `MONUSDT`, `RAYDIUMUSDT`, `SHIB1000USDT`, `TRUMPUSDT`) and the worker answered the lifecycle request at 15:09:10 as generation `ff7ad772` epoch 1 with frontiers long 21 / carry 208, `status=recovering`, 1,024 listed symbols, 119 LONG and 149 CARRY eligible. Hyperliquid: engine `may_open=true`, `private_stream_ready=true`, `strategy_errors=[]`, equity 52.4 USDC, no position, LONG entries on; worker `status=ready`, 178 listed, 2 LONG / 4 CARRY eligible, sequences long 4 / carry 629. Demo: engine `may_open=true`, every sleeve's entries off, no position (the INJUSDT 59.5 long exited during the afternoon; `orders_sent 1 fills 1` since its 15:00 boot is the probe). Bybit mainnet, read on the venue by `attest-flat` 15:09: INJUSDT long 4.6 with its native stop order still working, plus wallet dust (SOL, WLD, GRAM, MNT) and 2.4308 USDT in Easy Earn; nobody manages the exit.
  - Hyperliquid worker, two boot faults fixed (owner: "hyperliquid uses USDC pairs"; the engine's spelling stays `BTCUSDT`, the venue's is `BTC`, settle coin USDC). (1) `kline repair through …: Hyperliquid lists no coin for BTCUSDT` (also `ETHUSDT`, `ATOMUSDT`): the venue's symbol-to-coin table is filled only by a `meta` read, and a boot that restores its universe from the checkpoint never made one before the first kline repair. `HyperliquidPublicVenue::coin_of` now reads `meta` itself when the table is empty, once per process. (2) `universe refresh left a sleeve with no eligible symbol` once per boot: launch times come from a one-coin-at-a-time listing-history pass that started empty on every boot, so the first refresh saw no ages and both sleeves fell under `min_listing_age_days`. `PublicVenue::seed_listing_history` (default no-op) lets the live runner seed the venue from the checkpoint's `instruments[*].launch_time_ms` at start, so a warm boot has its ages and re-reads no coin. Tests `the_first_lane_read_fills_the_coin_table_from_meta`, `a_seeded_listing_history_is_not_read_again`; `a_coin_the_venue_stopped_listing_fails_only_its_own_lane` now seeds the table it expects a miss against. The one `HTTP 429` on Hyperliquid account recovery (15:08:59 UTC) was retried by the existing path and needs no change.
  - `attest-flat` and the Bybit inventory scan (owner: "if wallet balance is tiny then just say its flat"). Before: every non-cash wallet coin and every positive USDT holding outside the unified or funding wallet was a position, so mainnet's SOL 0.00004797, WLD 0.0042, GRAM 0.0232, MNT 0.00012169 and 2.4308 USDT in Easy Earn each blocked the proof. Now `parse_inventory_wallet` reads the venue's own `usdValue` and labels a positive holding under `DUST_USD_VALUE` (1 USD, under the venue's smallest sellable value) `wallet_dust`; `label_dust_holdings` gives the same coin's asset-overview rows `asset_account_dust:*`; positive USDT/USDC is cash in every account type except `TradingBot` and `CopyTrading`, where it is deployed capital and stays inventory; `AccountInventory::is_flat` ignores dust and `attest-flat` prints each dust row as `flat-dust …` before saying flat. A holding without a venue valuation counts in full. Canary guards ignore dust like wallet assets. Tests `wallet_inventory_ignores_cash_but_keeps_assets_and_liabilities`, `a_dust_coin_is_dust_on_every_surface_that_lists_it`, `dust_alone_reads_flat_and_anything_else_does_not`; the two asset-overview tests carry the cash rule. MEXC and Hyperliquid balance endpoints report no USD value, so their spot rows still count in full.
  - Deploy receipt: pending.

- **2026-09-10 — Storage headroom for the recorders: tape window 6 h, recorder floor 12 GiB, release keep 1 day, and the host watchdog warns on a stale reclaim receipt.**
  - Owner direction (scheduled task, applied 03:34–04:12 UTC): apply every lever and keep the recorders writing. Before, at 03:34 UTC: 34.9 GB free on `/` (125.7 GB), low water 33.29 GB (tape floor 25 GiB + 6 GiB writer headroom), the 24 h tape window holding 20 GB of Bybit and 2.8 GB of Binance tape, 38 release directories (2.3 GB) with their staged tarballs (0.9 GB), 27.7 GB of verified sealed WAL retained across 103 segments, backlog 0, receipt 02:41:31 UTC.
  - `deploy/systemd/liquidity-migration-market-tape-upload.service`: `--keep-hours 24` → `6`. Drive keeps every hour permanently either way; a shipped hour now leaves the host six hours after it ended. About 15 GB returned over the first upload runs.
  - `deploy/capture/bybit-linear.toml`, `deploy/capture/binance-usdm.toml`, and the `market_tape.config.StorageSettings` default: `min_free_disk_gb` 25 → 12. The reclaimer reads the floor from the configs, so low water becomes `max(reserve 14.04 GiB, 12 GiB) + 6 GiB` = 21.5 GB: the engine reserve binds, and the gap between the reclaimer's mark and the recorders' floor widens from 6 GiB to 8.6 GB, over the 7.4 GB quarantine dump of Sep 6. Each recorder restarts once, by the deploy's `capture_fingerprint`.
  - `scripts/runtime/reclaim_host_storage.py`: `--release-age-days` 3 → 1.
  - `scripts/runtime/check_fleet_liveness.py`: `evaluate_reclaim_stamp`, alert key `storage-reclaim`, WARNING when `receipts/storage-reclaim.last-success` is missing or older than `--max-reclaim-age-hours` (3 h: the reclaimer stamps only a clean hourly run, so two failed runs in a row page and one does not). `deploy/systemd/liquidity-migration-host-liveness.service` passes `--reclaim-stamp-file`. Test `test_reclaim_receipt_ages_into_a_warning`; `test_the_flagless_defaults_are_the_ones_the_deployed_unit_relies_on` pins the new floor and keep.
  - Not applied: `/opt/liquidity-migration/data` (1.7 GB) is not research data. It is the signal workers' event roots (`deploy/lib_realms.sh` `long_root`/`carry_root`/`exodus_root`, owned by `liquidity-signal-worker`: `event_demo_klines_1h` 1.2 GB, `reports` 224 MB, `.cache`, the strategy event tapes), and `scripts/vps/deploy_remote.sh` (`ensure_native_strategy_state`) falls back to `carry_sizing_anchors.json` and `exodus_state.json` there when a realm's native strategy state does not verify. Nothing under it has been written since 2026-09-01 12:23 UTC and the workers' `ReadWritePaths` no longer include it. Deleting it is the owner's call, not a storage lever.
  - Deploy receipt: [run `34434792960`](https://github.com/rob435/liquidity-migration/actions/runs/34434792960), dispatched 03:49:22 UTC on `6e5ca627` (push 03:48:56 behind a green pre-push gate), `ci` 03:52:09, `rust` 04:00:24, release artifact 04:03:25, `vps` 04:03:29–04:11:54: both recorders `result=restarted` (Bybit 04:04:52 with `capture-ready` in 2 s, Binance 04:05:31 in 0 s), the 300 s demo soak from 04:06:06, `atomic mainnet handover` 04:11:08 with both INJUSDT longs held through it, `deploy-ok` 04:11:44, rollback target `ae95fedf`. The 04:10 upload already ran on the new unit: `keep_hours=6.0`, `pruned_hours=38`, `pruned_bytes=15671418894`; free 34.9 → 53.1 GB. `storage plan` at 04:13: low water 21.52 GB (the 15.08 GB reserve binds over the 12.88 GB tape floor), high water 33.99 GB, runway 437,010 s, backlog 0, 30 release directories and 30 tarballs (2.6 GB) due at 04:41. Host watchdog 04:12:35: `ok scope=host units-and-heartbeats-healthy` with the receipt row live. 04:41 run on the new floor: exit 0, `reclaimed_bytes=2615627776` (30 release directories and 30 tarballs; 9 of each remain), `wal_segments_reclaimed=0`, `unverified_backlog_bytes=0`, `estimated_runway_s=438480`, receipt 04:41:31 UTC; free 54.9 GB at 05:04.

- **2026-09-09 — Alt-realm readiness, 23:16–23:25 UTC: MEXC's identity is bound and proven flat; Hyperliquid's second canary placed and cancelled a real order the venue confirmed, but the harness could not recognise its own order because the adapter hashed the canary's client id. Fixed in the cloid codec; both reruns are the owner's.**
  - MEXC. The owner supplied account UID `19445654`. Written on the host at
    23:17 UTC: `/etc/liquidity-migration/mexc-account-bindings.json` (root,
    0644, schema 1, the key's sha256 fingerprint bound to `19445654`) and
    `EXPECTED_ENGINE_ACCOUNT_USER_ID=uid-19445654` in `engine-mexc.env` (the
    retired `key-…` value backed up at `/root/engine-mexc.env.bak-20260910`).
    `verify-account-identity` answers `account-identity-ok account=uid-19445654`;
    `attest-flat` reads `flat=true samples=2 positions=0 open_orders=0`;
    `real-money preflight-mexc` passes every precondition. The owner's canary
    at 23:24:22 reached `private_feed=ready` and was stopped by systemd one
    second later when the operator's shell disconnected (`Stopping
    liquidity-migration-canary-order-mexc-3838236.service`); no order was
    created, and `attest-flat` at 23:26 reads flat again. Rerun pending.
  - Hyperliquid, canary run 2 at 23:24:37 UTC. `verify-account-identity`
    answers `0xcef3cc6085897672efc4bf5d8401f757c9b85b17`; preflight passes;
    `attest-flat` reads not flat on `wallet_asset HYPE Buy 0.00230994` in the
    spot wallet (the credential-wide scan counts wallet assets; the canary's
    precheck is derivative-and-order flatness and does not). The canary
    priced `order_px=77698 qty=0.00014 stop=66044` against `bid=78089
    ask=78090`, the venue accepted the create (`venue_order_id=540726821427`)
    and the cancel, `orderStatus` by oid read `Cancelled cumulative_filled_qty=0`
    and four cleanup scans read `derivative_positions=0 open_orders=0`. The
    command still ended in `create returned an id, but neither the private
    feed nor open-order inventory proved New`: no `New`, no `Cancelled` on the
    private feed, no match in `frontendOpenOrders`.
  - Root cause, fixed. Hyperliquid's client id is a fixed 16-byte `cloid`.
    `engine-venue/src/venues/hyperliquid/cloid.rs` packed only
    `eng-<ms>-<n>` reversibly and hashed every other shape one way, so the
    venue's `orderUpdates` rows and open-order list came back with a cloid
    `from_cloid` could not read, the feed dropped them as not ours, and the
    inventory row carried the raw hex instead of `lmcan-…`. Everything keyed
    by the venue's own oid worked, which is why the order cleaned up. The
    canary's `lmcan-<hex ms>-<pid>-<nonce>` and `lmcls-…` ids are now packed
    under scheme bytes `0x03` and `0x04` with a five-byte zero tail as the
    proof of origin, and print back byte for byte or are not packed. Engine
    ids and the hashed fallback are unchanged. Tests:
    `a_canary_id_survives_the_round_trip`,
    `a_canary_shaped_id_that_would_print_back_differently_is_not_packed`,
    `a_stranger_id_that_starts_with_the_canary_scheme_byte_is_not_ours`
    (`cloid.rs`), and `a_canary_client_id_packs_into_the_cloid_and_looks_itself_up`
    (`tests/venue/hyperliquid_requests.rs`, which pinned the hashed
    behaviour and failed on the fix until rewritten). `hyperliquid_mainnet`
    and `mexc_mainnet` stay `live-canary` until a rerun passes on the
    deployed fix.

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
  - Sealed WAL segments below `engine_wal::retention_floor` pruned once verified against Google Drive backup manifests under `engine-state/sealed/`. Pruning budgets free space via `statvfs`: reserve `max(12 % of capacity, 8 GiB)`, low water a 6 GiB writer headroom above it. Old release packages, temporary files, and apt caches cleaned automatically.
  - Deploy receipt: Installed on host under `42dd7446`. First run reclaimed ~3.2 GB of disk space. It exited 1 on the hyperliquid family, which has no log; fixed in `14311d1c` (`no log yet`, not an error), on the host only at 22:43 UTC with `e899ea21`, so the 16:41–22:41 runs exited 1 too.
  - Low water sat under the tape recorders' floor (fixed in `930d3be2`, deploy-ok 23:30:37 UTC): 20.04 GiB against `min_free_disk_gb = 25` in both `deploy/capture/*.toml`, so a recorder under 25 GiB counted every frame and wrote none while its own retention pass freed tape, 5 GiB before any verified WAL was reclaimed. That is the `disk_dropped_frames` mechanism on the fleet dashboard: 8,816,049 Bybit and 2,487,759 Binance frames since the recorders' Sep 6 boot, all between 22:0x Sep 7 and 02:0x Sep 8 (the `capture-disk` incident), none since; free had fallen 56.6 → 28.1 GB from 02:00 Sep 8 to 15:00 Sep 9. Fix: `tape_free_floor_bytes` reads the highest `[storage].min_free_disk_gb` under `deploy/capture/` (`--tape-floor-config`, `RECLAIM_TAPE_FLOOR_CONFIG`), low water = writer headroom above `max(reserve, tape floor)` = 31 GiB, `tape_floor_bytes` in `status.json` and the journal line; three tests. First timer run on it, 23:41 UTC: exit 0, first `storage-reclaim.last-success`, free 35.4 GB, low water 33.29 GB, WAL reclamation due in ~5.5 h.
  - The dashboard's `Tape loss · 5m` besides the floor episodes: `dropped_frames` 48, Bybit, 22:09–22:10 UTC Sep 9 — eleven shards lost their Bybit sockets within 90 s (`ping/pong timed out`, `Connection to remote host was lost`), their resubscribe snapshots landed on a 3.9 k → 10.1 k frames/s burst, and the 262,144-frame queue overran against a writer that clears 7–8 k frames/s on one core. `gap` is shard reconnects, venue-side: 349 Bybit (168 lost connections, 77 ping timeouts) and 33 Binance since the Sep 6 boot. Not changed.

- **2026-09-09 — Manifest-driven realm plumbing via `deploy/realms.tsv`.**
  - Unified realm configuration into `deploy/realms.tsv` (# realm-table-v1) defining realm properties, venue mappings, modes, and enabled states.
  - Templated generation of systemd unit files, environment configurations, and Grafana dashboard (`render_dashboard.py`). `deploy/lib_sleeves.sh` dynamically reads realm table.
  - MEXC and Hyperliquid realms held provisioned but stopped (`live-canary`).
  - Dashboard (`930d3be2`): the `Realm` variable's `regex` and `allValue` are the table's `running` realms, so the stopped mexc and hyperliquid realms, which push `up=0` every minute by design, are no longer red `DOWN` / `ATTENTION` cards (owner: "they are just backups, remove them from grafana"). Dashboard `version` 12; re-import the JSON. Test `test_the_realm_variable_lists_only_the_realms_the_table_holds_running`.
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
