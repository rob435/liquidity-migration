# Graph Report - liquidity-migration  (2026-06-06)

## Corpus Check
- 145 files · ~424,603 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 4853 nodes · 15673 edges · 40 communities detected
- Extraction: 45% EXTRACTED · 55% INFERRED · 0% AMBIGUOUS · INFERRED: 8561 edges (avg confidence: 0.61)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 41|Community 41]]

## God Nodes (most connected - your core abstractions)
1. `ResearchConfig` - 958 edges
2. `EventDemoCycleConfig` - 634 edges
3. `TickerCache` - 384 edges
4. `VolumeEventResearchConfig` - 372 edges
5. `BybitMarketData` - 348 edges
6. `ContinuousDemoCycleConfig` - 332 edges
7. `EventWebSocketRiskEngine` - 248 edges
8. `ContinuousDemoDaemon` - 241 edges
9. `LivePanelCache` - 228 edges
10. `PrivateStateCache` - 221 edges

## Surprising Connections (you probably didn't know these)
- `ArchiveKlineDownloadConfig` --calls--> `test_archive_kline_default_requires_dense_utc_day()`  [INFERRED]
  liquidity_migration\archive_manifest.py → tests\test_liquidity_migration_archive.py
- `ArchiveHourlyKlineDownloadConfig` --calls--> `test_archive_hourly_kline_default_resumes_written_partitions()`  [INFERRED]
  liquidity_migration\archive_manifest.py → tests\test_liquidity_migration_archive.py
- `ArchiveHourlyKlineApiDownloadConfig` --calls--> `test_archive_hourly_api_kline_default_resumes_written_partitions()`  [INFERRED]
  liquidity_migration\archive_manifest.py → tests\test_liquidity_migration_archive.py
- `ArchiveHourlyKlineApiDownloadConfig` --calls--> `test_download_api_hourly_group_returns_empty_for_no_rows()`  [INFERRED]
  liquidity_migration\archive_manifest.py → tests\test_liquidity_migration_archive_manifest.py
- `BybitPrivateClient` --uses--> `A sleeve is active unless its kill-switch toggle (deploy/sleeves.env, loaded int`  [INFERRED]
  liquidity_migration\bybit.py → scripts\check_demo_liveness.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.01
Nodes (473): build_ws_trade_client(), BybitDataError, BybitMarketData, BybitPrivateClient, BybitRestRateLimiter, BybitTradeRouter, BybitWebSocketTradeClient, _env_flag() (+465 more)

### Community 1 - "Community 1"
Cohesion: 0.01
Nodes (492): calendar_shift(), date_boundary_ms(), date_ms(), Shared low-level helpers and constants for the liquidity_migration package.  C, The PIT *trading day* as a polars Date expression: ``date(ts_ms - 1ms)``., Scalar ``%Y-%m-%d`` form of :func:`trading_day_expr`: ``date(ts_ms - 1ms)``., Parse an ISO date/datetime to epoch milliseconds (UTC), or None if empty., Parse an ISO date/datetime to epoch milliseconds (UTC). Raises on empty. (+484 more)

### Community 2 - "Community 2"
Cohesion: 0.02
Nodes (334): active_primary_pnl_gate_allows_addon(), build_confirmed_entry_state(), build_live_continuous_state(), continuous_dataset_names(), _continuous_entry_candidates_with_signal_metadata(), _continuous_entry_link_prefix(), _continuous_exit_link_prefix(), _continuous_order_link_id() (+326 more)

### Community 3 - "Community 3"
Cohesion: 0.02
Nodes (332): _compute_pipeline_diagnostics(), _demo_event_config(), _execute_entries(), _execute_exits(), _execute_risk_exits(), _fetch_account_closed_pnl(), _orphan_close_pnl_backfill(), _orphan_close_pnl_from_records() (+324 more)

### Community 4 - "Community 4"
Cohesion: 0.01
Nodes (261): BybitPublicTradeStream, _default_kline_websocket_factory(), _patch_pybit_daemon_ping_timer(), Create a fresh pybit WebSocket client tuned for kline streams., Guard automated order submission: explicit confirm flag and demo account only., validate_order_submit_allowed(), Resolve the shared account/control root from a sleeve's own data root (mirrors, shared_account_root() (+253 more)

### Community 5 - "Community 5"
Cohesion: 0.01
Nodes (266): ContinuousEventConfig, _additive_equity(), _additive_summary(), _btc_trend_returns(), build_continuous_panel(), _entry_vol(), _feature_tag(), _fresh_entries() (+258 more)

### Community 6 - "Community 6"
Cohesion: 0.02
Nodes (240): ArchiveFileNotFoundError, ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig, Config for ``build_archive_trade_manifest``.      The manifest is always built, The requested archive URL returned HTTP 404.      Distinct from transient netw, BybitPrivateWebSocketStream (+232 more)

### Community 7 - "Community 7"
Cohesion: 0.03
Nodes (207): read_dataset(), write_dataset(), _ensure_sleeve_column(), EventWebSocketRiskConfig, EventWebSocketRiskEngine, run_event_ws_risk(), _validate_trade_row_invariants(), _validate_ws_risk_config() (+199 more)

### Community 8 - "Community 8"
Cohesion: 0.02
Nodes (178): build_parser(), _csv_float(), _csv_int(), _csv_str(), _download_manifest_staleness_lines(), _event_demo_timing_text(), _event_risk_payload_material(), _event_risk_report_path() (+170 more)

### Community 9 - "Community 9"
Cohesion: 0.02
Nodes (149): _build_demo_features(), _bust_demo_instruments_cache(), _concat_recent_klines(), _dedupe_recent_klines(), _demo_feature_cache_fingerprint(), _demo_feature_cache_paths(), _demo_instruments(), _demo_instruments_cache_paths() (+141 more)

### Community 10 - "Community 10"
Cohesion: 0.03
Nodes (156): HTMLParser, download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _archive_kline_skip_rows(), build_archive_trade_manifest(), _bybit_api_kline_url(), _date_from_ts_ms() (+148 more)

### Community 11 - "Community 11"
Cohesion: 0.03
Nodes (124): _Bar, _empty_klines_frame(), KlineStore, _parse_ws_kline_event(), In-memory 1h kline store for the WS-driven kline-delivery path.  The cross-sec, One 1h bar. Stored per-symbol keyed by ``ts_ms`` in the store's dict., Best-effort parse of a single bar dict from pybit's WS kline payload.      pyb, Thread-safe in-memory 1h klines per symbol with periodic disk flush.      Appe (+116 more)

### Community 12 - "Community 12"
Cohesion: 0.03
Nodes (141): account_key(), claim_symbol_reservation(), clamp_max_new_entries(), compute_im_used(), CrossSleeveAccountState, encode_control_row(), equal_split_budget(), _loads_budget() (+133 more)

### Community 13 - "Community 13"
Cohesion: 0.03
Nodes (101): BinanceDataError, BinanceUSDMData, _ceil_to_period(), _floor_to_period(), _recent_history_start(), _archive_filename(), _archive_outputs_exist(), _dates_between() (+93 more)

### Community 14 - "Community 14"
Cohesion: 0.06
Nodes (113): coerce_int(), Coerce `value` to an int, returning `default` (0) on a missing/invalid value., _active_exposure_contributor_rows(), _active_exposure_rows(), _active_exposure_summary(), _active_trade_rows_at(), _anatomy_drift(), _anatomy_summary() (+105 more)

### Community 15 - "Community 15"
Cohesion: 0.04
Nodes (92): _daily_pnl_metrics(), MAR/Sharpe/DD from any daily PnL series (cols: ts_ms, equity, drawdown, basket_r, _combined_rows(), _concentration_summary(), _fmt(), main(), _markdown(), _metrics_from_returns() (+84 more)

### Community 16 - "Community 16"
Cohesion: 0.04
Nodes (59): BybitKlineStreamPool, _close_state(), _is_already_subscribed_error(), _KlineConnectionState, Internal signal that the WS submission failed — the router decides     whether, True for pybit's "You have already subscribed to this topic" error.      pybit, Per-connection bookkeeping for the pool., Multi-connection WebSocket pool for 1h kline subscriptions.      Splits a larg (+51 more)

### Community 17 - "Community 17"
Cohesion: 0.03
Nodes (65): check_entries(), _last_coverage_gap(), _latest_cycle_parquet(), main(), maybe_telegram(), Return the universe_coverage_gap from the most recent submit cycle.      The c, Fraction of cycles served by the WS-fed caches vs the REST fallback.      The, ws_first_ratio() (+57 more)

### Community 18 - "Community 18"
Cohesion: 0.08
Nodes (55): send_telegram_message(), TelegramConfig, Alert, evaluate_cycle_liveness(), evaluate_exchange_errors(), evaluate_rmom_staleness(), evaluate_stop_protection(), evaluate_unit_states() (+47 more)

### Community 19 - "Community 19"
Cohesion: 0.04
Nodes (57): Pin the Strictness Manifesto decision-rule analyzer.  Specifically validates a, Construct a cell that passes EVERY gate; verify it's labelled candidate., A near-miss is inconclusive (Bybit passes, Binance falls short on sharpe Δ)., M6: a status!=ok cell must be returned as an explicit exclusion (a     falsifie, The full_pit_universe_pass column is parsed into CellMetrics so the     analyze, At window_days = 365.25 (one calendar year), annualized = total_return.     San, At window_days = 182.625 (half year), annualized = (1+r)^2 - 1., The actual Round 1 / Phase 0 / R1 baseline at 1125 days (Bybit):     total_retu (+49 more)

### Community 20 - "Community 20"
Cohesion: 0.07
Nodes (49): finite_float(), parse_date(), pct(), Coerce `value` to a finite float, returning `default` if missing/invalid., Format a fraction as a 2-decimal percentage, or `invalid` if not finite., Parse an ISO date/datetime string to a UTC `date`, or None if empty., _covered_pairs(), _dataset_notes() (+41 more)

### Community 21 - "Community 21"
Cohesion: 0.08
Nodes (10): LongNativeDemoDaemon, Direct behavioral tests for LongNativeDemoDaemon.  The long sleeve runs live o, In-memory WS stream: records the execution callback, supports inject + close., A venue-pushed execution event must reach the daemon's ExecutionEventRouter, IND-1 (long sleeve mirror): a genuinely DOWN private socket, continuously down, _RecordingWsStream, _stub_long_cycle_runner(), test_long_daemon_force_reconnects_private_stream_when_socket_down() (+2 more)

### Community 22 - "Community 22"
Cohesion: 0.09
Nodes (36): Tests for BybitTradeRouter — WS-first / REST-fallback order submission.  The r, Bybit demo currently returns retCode != 0 for WS order entry; the     router mu, The double-submit race: WS submit reached Bybit but the ack     network-delayed, Probe checks open-orders first, then history. If neither has the     order, RES, A Rejected/Cancelled history row means the WS submit did NOT take effect,     s, A WS exception (lost ack), like a timeout, may have reached Bybit — the     ded, The probe is gated on failure.kind == 'timeout'. A WS reject     (Bybit demo's, Minimal stand-in for BybitPrivateClient. (+28 more)

### Community 23 - "Community 23"
Cohesion: 0.04
Nodes (20): H4: the funding total must not be truncated to the first 200-row page —     fol, BybitMarketData must call rate_limiter.acquire() before each pybit HTTP     cal, BybitPrivateClient must acquire the shared rate limiter before every     pybit, When _call retries on a failed pybit call, each attempt must hit the     limite, The multi-daemon demo connect-storm fix: a transient connect failure is     ret, All attempts fail -> raise so the caller falls back to REST (seatbelt)., Permanent errors (no creds) raise immediately with NO jitter/backoff —     keep, EXC-3: a definite (non-rate-limit) venue reject must raise IMMEDIATELY, not burn (+12 more)

### Community 24 - "Community 24"
Cohesion: 0.09
Nodes (34): _assert_download_completeness(), build_binance_oos(), discover(), fetch_month_klines(), list_symbol_months(), list_usdm_usdt_symbols(), main(), parse_month_csv() (+26 more)

### Community 25 - "Community 25"
Cohesion: 0.15
Nodes (29): auto_kline_fill(), backtest_window(), _cli(), find_backtest_csv(), _first_summary_line(), _have_rsync(), _load_dotenv(), main() (+21 more)

### Community 26 - "Community 26"
Cohesion: 0.11
Nodes (9): Unit tests for the fast liveness/safety watchdog's pure decision logic., Orchestration coverage (audit 2026-06-02 #54): gather_alerts must feed the live, The LONG sleeve runs on its own root with no rmom gate. gather_long_alerts must, Continuous-sleeve diagnosability: a zero universe / empty kline store is the sam, The watchdog skips an intentionally-off sleeve. Explicit env always wins; the, test_gather_alerts_wires_stop_protection_and_unavailable(), test_gather_continuous_alerts_warns_on_empty_universe_and_unverified_stop(), test_gather_long_alerts_covers_cycle_age_and_stop_protection() (+1 more)

### Community 27 - "Community 27"
Cohesion: 0.19
Nodes (16): CellMetrics, CellVerdict, compute_annualized_return(), compute_mar(), evaluate_cell(), evaluate_cell_investigation(), _index_by_cell(), main() (+8 more)

### Community 28 - "Community 28"
Cohesion: 0.12
Nodes (7): Pin the R1 robustness / Tier-2 demo-candidate analyzer's gate integrity.  Audi, A >=100% cumulative loss makes (1+total_return) non-positive; a fractional, re-audit rescan-rmom-funding-2: the non-circular moving-block bootstrap must, The max(n-block+1, 1) guard must not IndexError when n < block., test_annualize_and_engine_mar_stay_real_below_minus_100pct(), test_block_bootstrap_indices_handle_series_shorter_than_block(), test_block_bootstrap_indices_never_straddle_the_boundary()

### Community 29 - "Community 29"
Cohesion: 0.17
Nodes (12): _is_link_or_junction(), _make_source_root(), Pin the legacy-archive manifest side-copy builder.  The script lives at scripts/, The reports/ subdir is intentionally NOT link-mirrored — Phase 1 cells     land, Build a synthetic source data root mimicking ~/SHARED_DATA/bybit_full_pit., Treat both symlinks and Windows directory junctions as 'linked'.      On Windows, test_build_legacy_archive_manifest_dry_run_makes_no_changes(), test_build_legacy_archive_manifest_filters_source_tag() (+4 more)

### Community 30 - "Community 30"
Cohesion: 0.17
Nodes (15): _atomic_print(), Cell, _compute_window_days(), Shared parallel-sweep runtime for the research program's cell sweeps.  Reusabl, Run one cell on one venue, return per-cell metrics dict.      Shells out to ``, Dispatch (cell × venue) work to ThreadPoolExecutor; flush summary.csv     after, Days between ``start_date`` (inclusive) and ``end_date`` (exclusive),     match, Best-effort total system RAM in GiB (POSIX sysconf, then /proc/meminfo). (+7 more)

### Community 31 - "Community 31"
Cohesion: 0.31
Nodes (9): Functional tests for the per-sleeve kill-switch (deploy/lib_sleeves.sh + sleeves, _run(), test_lib_fallback_defaults_continuous_off_others_on(), test_loaded_toggles_short_long_papers_on_continuous_off(), test_off_sleeve_is_disabled_on_sleeve_is_enabled(), test_short_paper_split_keeps_demo_retires_paper(), test_short_paper_unit_moved_out_of_short_sleeve_units(), test_verify_fails_when_on_sleeve_is_not_running() (+1 more)

### Community 32 - "Community 32"
Cohesion: 0.31
Nodes (5): _mk(), Tests for the PIT coverage / staleness diagnostics (FIX C)., test_coverage_fresh_manifest_is_not_stale(), test_coverage_missing_manifest(), test_coverage_stale_manifest_flagged()

### Community 33 - "Community 33"
Cohesion: 0.32
Nodes (6): _apply(), Causality tests for the residual-momentum precompute (scripts/precompute_residua, residual_momentum[D] = sum residual_return[D-9 .. D-3] (rolling 7, shift 3)., Look-ahead guard: residual_momentum[D] must be INVARIANT to residual_return at d, test_residual_momentum_does_not_read_future_residuals(), test_residual_momentum_is_causal_shift3()

### Community 34 - "Community 34"
Cohesion: 0.29
Nodes (1): Pin the single-source-of-truth promoted profiles (liquidity_migration.promoted).

### Community 35 - "Community 35"
Cohesion: 0.6
Nodes (5): load_entries(), load_klines(), load_rmom(), main(), _ts()

### Community 37 - "Community 37"
Cohesion: 0.67
Nodes (1): Guards against circular-import regressions in the post-refactor module split.

### Community 38 - "Community 38"
Cohesion: 1.0
Nodes (1): The manifest cannot PIT-validate the most recent daily-close signal.

### Community 39 - "Community 39"
Cohesion: 1.0
Nodes (1): Extract (orderId, orderLinkId) — accept Bybit's camelCase or the         snake_

### Community 41 - "Community 41"
Cohesion: 1.0
Nodes (1): The exit/risk-side orderLinkId vocabulary lives in ONE registry (quality-dup-12)

## Knowledge Gaps
- **403 isolated node(s):** `The requested archive URL returned HTTP 404.      Distinct from transient netw`, `Config for ``build_archive_trade_manifest``.      The manifest is always built`, `Fetch currently-listed Bybit v5 perpetual symbols + their launchTime (ms).`, `Build manifest rows from v5-Trading listings for ``(symbol, date)``     pairs m`, `Build the Bybit PIT trade manifest by merging two sources:      1. ``public.by` (+398 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 34`** (7 nodes): `test_promoted_profiles.py`, `Pin the single-source-of-truth promoted profiles (liquidity_migration.promoted).`, `test_continuous_overlay_candidate_is_documented_but_not_promoted()`, `test_long_profile_is_v11a()`, `test_registry_covers_two_sleeves()`, `test_short_profile_is_drop_all_4_age300_ff6()`, `test_windowing_sets_dates_on_all_sleeves()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 37`** (3 nodes): `test_package_import_integrity.py`, `Guards against circular-import regressions in the post-refactor module split.`, `test_split_sibling_imports_cold_in_fresh_process()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 38`** (1 nodes): `The manifest cannot PIT-validate the most recent daily-close signal.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 39`** (1 nodes): `Extract (orderId, orderLinkId) — accept Bybit's camelCase or the         snake_`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 41`** (1 nodes): `The exit/risk-side orderLinkId vocabulary lives in ONE registry (quality-dup-12)`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ResearchConfig` connect `Community 0` to `Community 1`, `Community 2`, `Community 3`, `Community 6`, `Community 7`, `Community 9`, `Community 10`, `Community 13`, `Community 21`?**
  _High betweenness centrality (0.165) - this node is a cross-community bridge._
- **Why does `main()` connect `Community 8` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 10`, `Community 13`, `Community 14`, `Community 18`, `Community 20`, `Community 21`?**
  _High betweenness centrality (0.118) - this node is a cross-community bridge._
- **Why does `VolumeEventResearchConfig` connect `Community 6` to `Community 0`, `Community 1`, `Community 3`, `Community 4`, `Community 7`, `Community 8`?**
  _High betweenness centrality (0.116) - this node is a cross-community bridge._
- **Are the 956 inferred relationships involving `ResearchConfig` (e.g. with `ContinuousDemoCycleConfig` and `LivePanelCache`) actually correct?**
  _`ResearchConfig` has 956 INFERRED edges - model-reasoned connections that need verification._
- **Are the 631 inferred relationships involving `EventDemoCycleConfig` (e.g. with `One-line `event demo cycle ...` summary used by both the legacy bash-loop     r` and `Coverage table + (when stale) a prominent WARNING that download-data does not`) actually correct?**
  _`EventDemoCycleConfig` has 631 INFERRED edges - model-reasoned connections that need verification._
- **Are the 369 inferred relationships involving `TickerCache` (e.g. with `EventDemoDaemon` and `Long-running demo entry/exit daemon with WS-driven fill confirmation.  The leg`) actually correct?**
  _`TickerCache` has 369 INFERRED edges - model-reasoned connections that need verification._
- **Are the 369 inferred relationships involving `VolumeEventResearchConfig` (e.g. with `One-line `event demo cycle ...` summary used by both the legacy bash-loop     r` and `Coverage table + (when stale) a prominent WARNING that download-data does not`) actually correct?**
  _`VolumeEventResearchConfig` has 369 INFERRED edges - model-reasoned connections that need verification._