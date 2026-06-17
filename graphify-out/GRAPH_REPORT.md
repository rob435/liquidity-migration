# Graph Report - liquidity-migration  (2026-06-17)

## Corpus Check
- 171 files · ~443,528 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 5733 nodes · 16293 edges · 59 communities detected
- Extraction: 55% EXTRACTED · 45% INFERRED · 0% AMBIGUOUS · INFERRED: 7388 edges (avg confidence: 0.64)
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
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 57|Community 57]]
- [[_COMMUNITY_Community 58|Community 58]]
- [[_COMMUNITY_Community 59|Community 59]]

## God Nodes (most connected - your core abstractions)
1. `ResearchConfig` - 610 edges
2. `EventWebSocketRiskEngine` - 290 edges
3. `EventDemoCycleConfig` - 262 edges
4. `ContinuousDemoCycleConfig` - 242 edges
5. `EventWebSocketRiskConfig` - 226 edges
6. `KlineStore` - 188 edges
7. `BybitMarketData` - 179 edges
8. `TickerCache` - 174 edges
9. `ContinuousRebalanceResizePlan` - 172 edges
10. `EventRiskCycleConfig` - 148 edges

## Surprising Connections (you probably didn't know these)
- `build_parser()` --calls--> `test_cli_klines_follow_root_parses_into_config()`  [INFERRED]
  liquidity_migration\cli.py → tests\test_kline_follower.py
- `_cmd_reconcile_continuous_paper_demo()` --calls--> `run_continuous_paper_demo_reconciliation()`  [INFERRED]
  liquidity_migration\cli.py → liquidity_migration\reconciliation.py
- `_cmd_continuous_forward_readiness()` --calls--> `run_continuous_forward_readiness()`  [INFERRED]
  liquidity_migration\cli.py → liquidity_migration\reconciliation.py
- `_cmd_continuous_vs_daily_forward()` --calls--> `run_continuous_vs_daily_forward_comparison()`  [INFERRED]
  liquidity_migration\cli.py → liquidity_migration\reconciliation.py
- `ResearchConfig` --uses--> `data-download-4: a never-before-covered range that returns [] (a symbol that`  [INFERRED]
  liquidity_migration\config.py → tests\test_liquidity_migration_downloaders.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.01
Nodes (424): BybitMarketData, _cmd_continuous_event_demo_cycle(), finite_float(), Coerce `value` to a finite float, returning `default` if missing/invalid., active_primary_pnl_gate_allows_addon(), apply_continuous_demo_profile(), _btc_trend_gate_allows_entries(), build_confirmed_entry_state() (+416 more)

### Community 1 - "Community 1"
Cohesion: 0.01
Nodes (296): build_ws_trade_client(), BybitDataError, BybitPrivateClient, BybitPublicTradeStream, BybitWebSocketTradeClient, _default_kline_websocket_factory(), _env_flag(), _is_duplicate_order_link() (+288 more)

### Community 2 - "Community 2"
Cohesion: 0.02
Nodes (331): BybitPrivateWebSocketStream, BybitPublicTickerStream, Subscribe to wallet balance pushes. Bybit pushes a per-account         snapshot, Socket-level liveness of the private stream. pybit's WebSocket subclasses, ContinuousHedgeConfig, HedgeDecision, HedgeDecision2F, _collect_private_snapshots() (+323 more)

### Community 3 - "Community 3"
Cohesion: 0.01
Nodes (338): TradeLifecycleConfig, _additive_equity(), _additive_summary(), _apply_entry_order(), _assert_funding_one_per_settlement(), _assert_rmom_covers_window(), _btc_trend_returns(), build_continuous_panel() (+330 more)

### Community 4 - "Community 4"
Cohesion: 0.02
Nodes (278): HTMLParser, _archive_cache_is_complete(), ArchiveDownloadIncompleteError, ArchiveFileNotFoundError, _content_length(), download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive() (+270 more)

### Community 5 - "Community 5"
Cohesion: 0.03
Nodes (276): ResearchConfig, hedge_order_link_id(), download_market_data(), _order_link_id(), _run_reconciliation(), read_dataset(), write_dataset(), EventWebSocketRiskConfig (+268 more)

### Community 6 - "Community 6"
Cohesion: 0.02
Nodes (259): build_full_ledger(), _config_path(), forward_readiness_summary(), ForwardUpdateResult, frozen_config_hash(), frozen_hedge_instruments(), frozen_hedge_mode(), frozen_hedge_regime() (+251 more)

### Community 7 - "Community 7"
Cohesion: 0.02
Nodes (190): _follower_continuous_kline_stream_manager_factory(), FollowerKlineStreamManager, Read-only kline FOLLOWER — share one WS kline data plane across co-located sleev, Initial snapshot read + start the poll thread. Never blocks on a         bootst, Warn ONCE per staleness episode when the leader snapshot stops moving —, Drop follower-side symbols that are no longer in the leader snapshot         (t, Re-read the leader snapshot iff its (mtime, size) changed. Returns         True, ``KlineStreamManager`` drop-in that follows another root's flushed snapshot. (+182 more)

### Community 8 - "Community 8"
Cohesion: 0.02
Nodes (233): BybitRestRateLimiter, Thread-safe sliding-window rate limiter shared across BybitMarketData     insta, _cmd_discover_universe(), _cmd_long_native_event_demo_cycle(), _cmd_reconcile_long_paper_demo(), _universe_config_from_args(), UniverseConfig, _build_demo_universe() (+225 more)

### Community 9 - "Community 9"
Cohesion: 0.02
Nodes (221): _cal_roll(), date_boundary_ms(), date_ms(), _date_range(), _date_symbol_set(), _exclude_symbols(), _iso_date(), _iso_month() (+213 more)

### Community 10 - "Community 10"
Cohesion: 0.02
Nodes (149): BybitKlineStreamPool, _close_state(), _close_ws_client(), _is_already_subscribed_error(), _KlineConnectionState, Close a pybit WS client with a hard timeout.      pybit's exit/close/stop meth, True for pybit's "You have already subscribed to this topic" error.      pybit, Per-connection bookkeeping for the pool. (+141 more)

### Community 11 - "Community 11"
Cohesion: 0.02
Nodes (141): _cmd_combined_book_telegram_report(), _cmd_event_risk_cycle(), _active_position_by_symbol(), _bool(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot(), build_position_pnl_snapshot(), _build_private_client() (+133 more)

### Community 12 - "Community 12"
Cohesion: 0.02
Nodes (192): calendar_roll(), calendar_shift(), is_weekend_ms(), A per-symbol positional ``value.shift(periods).over("symbol")`` that is NULL unl, Calendar-aware rolling window over an integer timestamp grid.      Row-based `, True when the UTC day containing ``ts_ms`` is Saturday or Sunday.      Epoch d, build_factor_panel(), compute_btc_beta() (+184 more)

### Community 13 - "Community 13"
Cohesion: 0.02
Nodes (156): build_parser(), _add_archive_download_klines_1h_api_parser(), _add_archive_download_klines_1h_parser(), _add_archive_download_klines_parser(), _add_archive_manifest_parser(), _add_combined_book_report_parser(), _add_continuous_addon_shadow_audit_parser(), _add_continuous_event_demo_cycle_parser() (+148 more)

### Community 14 - "Community 14"
Cohesion: 0.02
Nodes (163): account_key(), claim_symbol_reservation(), claim_symbols_reservation(), clamp_max_new_entries(), compute_im_used(), CrossSleeveAccountState, encode_control_row(), equal_split_budget() (+155 more)

### Community 15 - "Community 15"
Cohesion: 0.04
Nodes (136): _cmd_continuous_addon_shadow_audit(), coerce_int(), Coerce `value` to an int, returning `default` (0) on a missing/invalid value., _active_exposure_contributor_rows(), _active_exposure_rows(), _active_exposure_summary(), _active_trade_rows_at(), _anatomy_drift() (+128 more)

### Community 16 - "Community 16"
Cohesion: 0.03
Nodes (111): BinanceDataError, BinanceUSDMData, _ceil_to_period(), _floor_to_period(), _http_error_detail(), _raise_if_suspicious_empty_page(), Seconds to wait after a 429/418, from the Retry-After header.          Falls b, Best-effort detail string from an HTTPError body (the Binance error JSON). (+103 more)

### Community 17 - "Community 17"
Cohesion: 0.04
Nodes (93): _cmd_continuous_rebalance_cycle_audit(), audit_continuous_rebalance_cycles(), _calendar_metrics(), _clean_trades(), _continuous_beats_daily_mar(), _continuous_cycle_daily_returns(), _cycle_zero_daily_returns(), _daily_forward_returns() (+85 more)

### Community 18 - "Community 18"
Cohesion: 0.03
Nodes (77): _max_start_corrected(), _max_start_old(), Pin the Strictness Manifesto decision-rule analyzer.  Specifically validates a, Construct a cell that passes EVERY gate; verify it's labelled candidate., A near-miss is inconclusive (Bybit passes, Binance falls short on sharpe Δ)., M6: a status!=ok cell must be returned as an explicit exclusion (a     falsifie, The full_pit_universe_pass column is parsed into CellMetrics so the     analyze, At window_days = 365.25 (one calendar year), annualized = total_return.     San (+69 more)

### Community 19 - "Community 19"
Cohesion: 0.07
Nodes (53): BybitTradeRouter, Route order placement + cancellation through WS first, REST as fallback., Issue a WS call and block (with timeout) for the ack.          Returns the ack, Tests for BybitTradeRouter — WS-first / REST-fallback order submission.  The r, Bybit demo currently returns retCode != 0 for WS order entry; the     router mu, The double-submit race: WS submit reached Bybit but the ack     network-delayed, Probe checks open-orders first, then history. If neither has the     order, RES, A Rejected/Cancelled history row means the WS submit did NOT take effect,     s (+45 more)

### Community 20 - "Community 20"
Cohesion: 0.04
Nodes (73): _assert_download_completeness(), build_binance_oos(), discover(), _fetch_expected_sha256(), fetch_month_klines(), list_symbol_months(), list_usdm_usdt_symbols(), main() (+65 more)

### Community 21 - "Community 21"
Cohesion: 0.07
Nodes (67): coefficient_sign_consistency(), FoldCoefficients, _rank(), rank_ic(), Walk-forward ridge combiner: frozen causal features -> one forward-return score., The fitted model for one walk-forward fold (for stability auditing)., Closed-form ridge with an UNPENALIZED intercept.      ``x`` is (n, k) standard, Per-column mean/std from training rows. Zero-variance columns get std=1 so (+59 more)

### Community 22 - "Community 22"
Cohesion: 0.07
Nodes (62): _format_book_line(), format_age_ms(), format_pct(), format_usd(), format_utc_time_ms(), _rate_limit_retry_seconds(), Seconds to wait before the single 429 retry, or None when the error is     not, True when the token + chat-id env vars are both present (a send can be     atte (+54 more)

### Community 23 - "Community 23"
Cohesion: 0.03
Nodes (51): Unit tests for the fast liveness/safety watchdog's pure decision logic., The collector unit can never reach systemd 'failed' (RestartSec spaces starts, The LONG sleeve runs on its own root with no rmom gate. gather_long_alerts must, Continuous-sleeve diagnosability: a zero universe / empty kline store is the sam, Paper evidence writes continuous_fade_paper_* datasets and submits no orders, so, The watchdog skips an intentionally-off sleeve. Explicit env always wins; the, REGRESSION (audit 2026-06-12): send_telegram_message returns False (no exception, Run main() with every root skipped and no explicit --state-file, so the     sta (+43 more)

### Community 24 - "Community 24"
Cohesion: 0.06
Nodes (54): _float_or_nan(), _parse_day(), _has_columns(), _btc_daily_close_series(), _chart_final_values(), _chart_font(), _date_axis_ticks(), _draw_monthly_return_table() (+46 more)

### Community 25 - "Community 25"
Cohesion: 0.07
Nodes (49): parse_date(), pct(), Format a fraction as a 2-decimal percentage, or `invalid` if not finite., Parse an ISO date/datetime string to a UTC `date`, or None if empty., _covered_pairs(), _dataset_notes(), _dataset_row(), DatasetCoverageSnapshot (+41 more)

### Community 26 - "Community 26"
Cohesion: 0.06
Nodes (46): band_notionals(), build_arg_parser(), collect_cycle(), enforce_retention(), _file_day(), free_disk_bytes(), _get_json(), iter_jsonl_rows() (+38 more)

### Community 27 - "Community 27"
Cohesion: 0.08
Nodes (31): build_arg_parser(), bybit_linear_symbols(), connection_expired(), JsonlDayWriter, main(), parse_binance_event(), parse_bybit_event(), Live liquidation collectors — Bybit allLiquidation + Binance forceOrder (P3, 202 (+23 more)

### Community 28 - "Community 28"
Cohesion: 0.22
Nodes (25): reconcile_continuous_snipes(), _cfg(), _entry_row(), FakeClient, _orders_df(), Sniper live-wiring tests (S1 Amendment 6 → demo cycle, 2026-06-10).  Covers: p, REGRESSION (audit 2026-06-12): a PartiallyFilled order missing from a torn, REGRESSION (audit 2026-06-11, empty-fetch-as-deletion class): an order absent (+17 more)

### Community 29 - "Community 29"
Cohesion: 0.14
Nodes (28): _append(), _arm_entry(), compute_shadow_anchor(), Forward paper-shadow of the fade-completion dynamic exit (P5 follow-up, 2026-06-, Arm one entry row (fresh exec_entry OR a recovered open ledger trade).      Id, One per-cycle shadow sweep (pure bookkeeping; no orders).      Arms fresh entr, Replay the JSONL into (armed_by_trade_id, exited_trade_ids)., anchor = clip(max(runup24h, ret1), lo, hi) at the signal bar, causal.      Use (+20 more)

### Community 30 - "Community 30"
Cohesion: 0.11
Nodes (25): _bookdepth_row(), _bookdepth_zip(), _load_backfill_bookdepth(), Regression tests for audit2 unit ``bookdepth_backfill``.  Ports the metrics-ba, When both fetch attempts exhaust retries, _day_rows yields the distinct     _TR, A genuine 404 (None blob) stays None — a real no-data day., Numerical-equivalence gate: a normal day with two snapshots in the same hour, When EVERY day fails transiently, the writer must NOT touch an empty marker (+17 more)

### Community 31 - "Community 31"
Cohesion: 0.16
Nodes (21): _cli(), _have_rsync(), _load_dotenv(), main(), pull_sleeve(), _py(), _quote(), Pretty, predictable step banners + command echo. (+13 more)

### Community 32 - "Community 32"
Cohesion: 0.12
Nodes (24): _load_backfill_metrics(), _metrics_row(), _metrics_zip(), Tests for scripts/backfill_binance_metrics_vision.py.  backfill-writers-2 (202, The core backfill-writers-1 fix: a previously-written parquet is UNIONED with, A re-fetched/corrected day supersedes the stale row (keep=last)., A prior parquet that already carried a `coverage` column must not break the, End-to-end through backfill_symbol: a second run that fetches a new day must (+16 more)

### Community 33 - "Community 33"
Cohesion: 0.1
Nodes (20): _is_link_or_junction(), _make_source_root(), Pin the legacy-archive manifest side-copy builder.  The script lives at script, The reports/ subdir is intentionally NOT link-mirrored — Phase 1 cells     land, Fails on OLD code: the false 'verify ... below' promise must be gone., The corrected comment is accurate: a pre-existing target is assumed     correct, A pre-existing plain directory at the target is also assumed correct and     le, Normal path (no pre-existing target): a real symlink is created pointing     at (+12 more)

### Community 34 - "Community 34"
Cohesion: 0.09
Nodes (10): Pin the R1 robustness / Tier-2 demo-candidate analyzer's gate integrity.  Audi, A >=100% cumulative loss makes (1+total_return) non-positive; a fractional, re-audit rescan-rmom-funding-2: the non-circular moving-block bootstrap must, The max(n-block+1, 1) guard must not IndexError when n < block., test_annualize_and_engine_mar_stay_real_below_minus_100pct(), test_block_bootstrap_indices_handle_series_shorter_than_block(), test_block_bootstrap_indices_never_straddle_the_boundary(), test_load_monthly_dedups_duplicate_months() (+2 more)

### Community 35 - "Community 35"
Cohesion: 0.1
Nodes (5): _FakeJsonResponse, _Headers, _kline_row(), test_paged_kline_raises_on_suspicious_mid_range_empty_page(), test_paged_kline_terminal_empty_page_after_partial_is_benign()

### Community 36 - "Community 36"
Cohesion: 0.19
Nodes (16): CellMetrics, CellVerdict, compute_annualized_return(), compute_mar(), evaluate_cell(), evaluate_cell_investigation(), _index_by_cell(), main() (+8 more)

### Community 37 - "Community 37"
Cohesion: 0.12
Nodes (13): _make_page(), audit2 regression: binance funding-vision backfill — two defects.  (1) In-prog, Within the publish-lag window the immediately-prior month is also protected., Build a JSON funding-rate page of n settlements starting at start_ts., Normal (unchanged) behaviour: a single short page returns exactly its rows., No data at all -> empty result (unchanged)., backfill-writers-4: vision-first ordering + unique(keep='first', maintain_order=, Normal (unchanged) behaviour: a strictly-older 404 month IS cached. (+5 more)

### Community 38 - "Community 38"
Cohesion: 0.18
Nodes (9): _drive_main(), _eq_df(), Tests for scripts/equity_curves.py (+ scripts/continuous_deployed_equity.py)., Run ``main`` for the continuous sleeve and capture run_venue's kwargs., test_continuous_inherits_frozen_start_when_window_unset(), test_continuous_uses_explicit_start_when_given(), test_continuous_uses_rolling_start_when_years_given(), test_stats_no_drawdown_returns_none_mar() (+1 more)

### Community 39 - "Community 39"
Cohesion: 0.15
Nodes (13): continuous_profile(), ContinuousOverlayCandidate, ContinuousRebalanceCandidate, ContinuousStandaloneCandidate, long_profile(), THE single source of truth for what each sleeve runs in the live demo.  If you, Return cfg with start_date/end_date overridden when provided (both sleeve     c, The deployed LONG v11a profile (the `div` risk-engineering: universe 50,     ma (+5 more)

### Community 40 - "Community 40"
Cohesion: 0.23
Nodes (11): Functional tests for the per-sleeve kill-switch (deploy/lib_sleeves.sh + sleeves, Last-resort fallback (NEITHER sleeves.env present — a stripped checkout):     E, # NOTE: the `enable` branch deliberately matches real `systemctl enable` (withou, _run(), test_continuous_paper_split_keeps_demo_orders_off_runs_paper(), test_lib_fallback_defaults_every_sleeve_off(), test_loaded_toggles_long_continuous_and_paper_on(), test_off_sleeve_is_disabled_on_sleeve_is_enabled() (+3 more)

### Community 41 - "Community 41"
Cohesion: 0.26
Nodes (11): backfill_symbol(), _day_rows(), _days(), _fetch(), _kline_spans(), main(), _merge_with_existing(), Rows for one symbol-day. Returns ``None`` for a genuine 404 / empty / bad zip (+3 more)

### Community 42 - "Community 42"
Cohesion: 0.29
Nodes (11): analyze_venue(), block_boot_p(), build_factor(), _daily_panel_from_klines(), load_daily_panel(), main(), _mask(), WP1a: does trailing alt-vs-BTC relative strength predict continuous-short squeez (+3 more)

### Community 43 - "Community 43"
Cohesion: 0.29
Nodes (10): daily_closes(), daily_returns(), main(), overwrite_blocked(), Regenerate deploy/hedge_warmstart/{venue}_warmstart.csv from current data.  TH, Compare the regenerated series against the banked CSV on overlapping dates., Reason the overwrite must be refused, or None to allow it.      Guards the liv, regenerate() (+2 more)

### Community 44 - "Community 44"
Cohesion: 0.18
Nodes (1): Pin the single-source-of-truth promoted profiles (liquidity_migration.promoted).

### Community 45 - "Community 45"
Cohesion: 0.22
Nodes (5): _naive(), audit2 regression: regenerate_hedge_warmstart.daily_returns gap-guard.  The na, The pre-fix builder: pairs adjacent present days, no calendar guard., test_contiguous_series_identical_to_naive(), test_gap_day_pair_is_dropped()

### Community 46 - "Community 46"
Cohesion: 0.53
Nodes (9): _load_module(), _payload(), ROUND 4 (pins the round-3 fix): the watchdog monitors this oneshot on     the p, test_collecting_when_both_legs_fresh_and_window_immature(), test_continuous_only_when_daily_leg_dead(), test_main_exits_nonzero_when_attempted_send_fails(), test_pass_after_common_window_matures(), test_stale_only_when_continuous_itself_lags_today() (+1 more)

### Community 47 - "Community 47"
Cohesion: 0.27
Nodes (5): _mk(), Tests for the PIT coverage / staleness diagnostics (FIX C)., test_coverage_fresh_manifest_is_not_stale(), test_coverage_missing_manifest(), test_coverage_stale_manifest_flagged()

### Community 48 - "Community 48"
Cohesion: 0.36
Nodes (8): _cache_cutoff_month(), _fapi_topup(), _fetch(), _kline_spans(), main(), _month_rows(), _months(), Oldest month whose empty-404 archive must NOT be permanently cached.      The

### Community 49 - "Community 49"
Cohesion: 0.31
Nodes (8): _load_orchestrator(), Tests for scripts/continuous_forward_replay_orchestrator.py.  forward-replay-5, A drift RuntimeError in one venue must be captured as a per-venue 'drift' status, A non-drift failure is reported as 'error' (still isolated), not silently swallo, A stalled clock (a venue that drifted/failed) must make main() exit non-zero so, test_orchestrator_main_exits_nonzero_on_stall(), test_orchestrator_run_venue_isolates_drift(), test_orchestrator_run_venue_isolates_generic_error()

### Community 50 - "Community 50"
Cohesion: 0.39
Nodes (7): backfill_symbol(), _day_rows(), _days(), _fetch(), _kline_spans(), main(), Rows for one symbol-day. Returns ``None`` for a genuine 404 / empty / bad zip

### Community 51 - "Community 51"
Cohesion: 0.32
Nodes (6): _apply(), Causality tests for the residual-momentum precompute (scripts/precompute_residua, residual_momentum[D] = sum residual_return[D-9 .. D-3] (rolling 7, shift 3)., Look-ahead guard: residual_momentum[D] must be INVARIANT to residual_return at d, test_residual_momentum_does_not_read_future_residuals(), test_residual_momentum_is_causal_shift3()

### Community 52 - "Community 52"
Cohesion: 0.25
Nodes (1): Regression tests for scripts/reconcile.py (audit bucket b15).  Findings covere

### Community 54 - "Community 54"
Cohesion: 0.33
Nodes (5): Cross-module integration-completion regression tests from the audit buckets., Every owned _float must produce exactly what `finite_float(v, default=0.0), The whole point of finite_float: NaN/inf never leak into PnL/size math., test_owned_float_matches_canonical_finite_float(), test_owned_float_nan_inf_are_coerced_to_zero()

### Community 55 - "Community 55"
Cohesion: 0.5
Nodes (2): test_monthly_trade_counts_dedupes_component_overlap(), _write_trades()

### Community 56 - "Community 56"
Cohesion: 0.5
Nodes (3): Canonical tests for liquidity_migration.momentum_signals.  test-gaps-6 (reloca, test-gaps-6: _attach_residual_momentum (orphaned SHORT-engine code whose     do, test_residual_momentum_dead_join_is_removed()

### Community 57 - "Community 57"
Cohesion: 0.67
Nodes (1): Guards against circular-import regressions in the post-refactor module split.

### Community 58 - "Community 58"
Cohesion: 1.0
Nodes (1): The manifest cannot PIT-validate the most recent daily-close signal.

### Community 59 - "Community 59"
Cohesion: 1.0
Nodes (1): Extract (orderId, orderLinkId) — accept Bybit's camelCase or the         snake_

## Knowledge Gaps
- **576 isolated node(s):** `The requested archive URL returned HTTP 404.      Distinct from transient netw`, `A downloaded archive failed an integrity check (truncated body / corrupt     co`, `Parse the response's Content-Length header to an int, or None when absent/unpars`, `archive-integrity-3: cheaply confirm a cached/just-written archive is COMPLETE b`, `Config for ``build_archive_trade_manifest``.      The manifest is always built` (+571 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 44`** (11 nodes): `test_promoted_profiles.py`, `Pin the single-source-of-truth promoted profiles (liquidity_migration.promoted).`, `test_continuous_merged_rebalance_candidate_is_not_promoted()`, `test_continuous_overlay_candidate_is_documented_but_not_promoted()`, `test_continuous_performance_frontier_is_not_promoted()`, `test_continuous_profile_is_deployed_book_with_regime_hedge()`, `test_continuous_rebalance_candidate_is_not_promoted()`, `test_continuous_standalone_return_candidate_is_not_promoted()`, `test_long_profile_is_v11a()`, `test_registry_covers_long_and_continuous()`, `test_windowing_sets_dates_on_all_sleeves()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 52`** (8 nodes): `_load()`, `test_scripts_reconcile.py`, `Regression tests for scripts/reconcile.py (audit bucket b15).  Findings covere`, `test_reconcile_main_returns_nonzero_when_a_leg_fails()`, `test_reconcile_main_returns_zero_when_leg_clean()`, `test_summarize_leg_flags_missing_summary_as_failed_not_no_output()`, `test_summarize_leg_flags_nonzero_exit_as_failed()`, `test_summarize_leg_passes_only_on_clean_leg()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 55`** (5 nodes): `test_continuous_deployed_equity_refresh.py`, `test_chart_leverages_always_include_one_x()`, `test_monthly_returns_with_trades_uses_trade_counts()`, `test_monthly_trade_counts_dedupes_component_overlap()`, `_write_trades()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 57`** (3 nodes): `test_package_import_integrity.py`, `Guards against circular-import regressions in the post-refactor module split.`, `test_split_sibling_imports_cold_in_fresh_process()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 58`** (1 nodes): `The manifest cannot PIT-validate the most recent daily-close signal.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 59`** (1 nodes): `Extract (orderId, orderLinkId) — accept Bybit's camelCase or the         snake_`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ResearchConfig` connect `Community 5` to `Community 0`, `Community 1`, `Community 2`, `Community 4`, `Community 8`, `Community 9`, `Community 11`, `Community 16`?**
  _High betweenness centrality (0.122) - this node is a cross-community bridge._
- **Why does `build_parser()` connect `Community 13` to `Community 4`, `Community 7`?**
  _High betweenness centrality (0.072) - this node is a cross-community bridge._
- **Why does `BybitKlineStreamPool` connect `Community 10` to `Community 1`, `Community 7`?**
  _High betweenness centrality (0.063) - this node is a cross-community bridge._
- **Are the 608 inferred relationships involving `ResearchConfig` (e.g. with `Coverage table + (when stale) a prominent WARNING that download-data does not` and `Resolve the data root for a CLI command: commands that don't use it get the path`) actually correct?**
  _`ResearchConfig` has 608 INFERRED edges - model-reasoned connections that need verification._
- **Are the 209 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateWebSocketStream` and `BybitPublicTickerStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 209 INFERRED edges - model-reasoned connections that need verification._
- **Are the 261 inferred relationships involving `EventDemoCycleConfig` (e.g. with `ContinuousDemoCycleConfig` and `LivePanelCache`) actually correct?**
  _`EventDemoCycleConfig` has 261 INFERRED edges - model-reasoned connections that need verification._
- **Are the 236 inferred relationships involving `ContinuousDemoCycleConfig` (e.g. with `Coverage table + (when stale) a prominent WARNING that download-data does not` and `Resolve the data root for a CLI command: commands that don't use it get the path`) actually correct?**
  _`ContinuousDemoCycleConfig` has 236 INFERRED edges - model-reasoned connections that need verification._