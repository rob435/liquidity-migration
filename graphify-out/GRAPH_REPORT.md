# Graph Report - liquidity-migration  (2026-06-28)

## Corpus Check
- 197 files · ~4,770,515 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 6356 nodes · 17901 edges · 63 communities detected
- Extraction: 57% EXTRACTED · 43% INFERRED · 0% AMBIGUOUS · INFERRED: 7701 edges (avg confidence: 0.65)
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
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 57|Community 57]]
- [[_COMMUNITY_Community 59|Community 59]]
- [[_COMMUNITY_Community 60|Community 60]]
- [[_COMMUNITY_Community 61|Community 61]]
- [[_COMMUNITY_Community 62|Community 62]]
- [[_COMMUNITY_Community 63|Community 63]]

## God Nodes (most connected - your core abstractions)
1. `ResearchConfig` - 606 edges
2. `EventWebSocketRiskEngine` - 290 edges
3. `ContinuousDemoCycleConfig` - 275 edges
4. `EventDemoCycleConfig` - 247 edges
5. `EventWebSocketRiskConfig` - 226 edges
6. `ContinuousRebalanceResizePlan` - 190 edges
7. `KlineStore` - 189 edges
8. `BybitMarketData` - 186 edges
9. `TickerCache` - 182 edges
10. `read_dataset()` - 154 edges

## Surprising Connections (you probably didn't know these)
- `Return manifest symbol-days whose 5m partition is missing or short.` --uses--> `ArchiveHourlyKlineApiDownloadConfig`  [INFERRED]
  scripts\backfill_5m_klines.py → liquidity_migration\archive_manifest.py
- `test_cli_klines_follow_root_parses_into_config()` --calls--> `build_parser()`  [INFERRED]
  tests\test_kline_follower.py → liquidity_migration\cli.py
- `test_cost_config_default_models_live_100pct_taker()` --calls--> `CostConfig`  [INFERRED]
  tests\test_liquidity_migration_config.py → liquidity_migration\config.py
- `test_cost_config_default_is_full_taker_not_maker_blend()` --calls--> `CostConfig`  [INFERRED]
  tests\test_liquidity_migration_config.py → liquidity_migration\config.py
- `data-download-4: a never-before-covered range that returns [] (a symbol that` --uses--> `ResearchConfig`  [INFERRED]
  tests\test_liquidity_migration_downloaders.py → liquidity_migration\config.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.01
Nodes (514): BybitMarketData, BybitPrivateWebSocketStream, BybitPublicTickerStream, Subscribe to wallet balance pushes. Bybit pushes a per-account         snapshot, Socket-level liveness of the private stream. pybit's WebSocket subclasses, BtcRiskLiveSizer, Persistent live state for ``CTRL_BTC_RISK_70_90_35``., Persist only newly scored decisions that reached accepted execution. (+506 more)

### Community 1 - "Community 1"
Cohesion: 0.01
Nodes (440): active_primary_pnl_gate_allows_addon(), _append_continuous_jsonl_event(), _append_continuous_lifecycle_event(), _append_continuous_risk_event(), _apply_btc_risk_sizing(), apply_continuous_demo_profile(), _btc_risk_decision_key(), _btc_risk_sizing_payload_fields() (+432 more)

### Community 2 - "Community 2"
Cohesion: 0.01
Nodes (362): date_boundary_ms(), Parse an ISO date/datetime to epoch milliseconds (UTC), or None if empty., _coerce_bool(), _coerce_field(), ensure_data_root_exists(), ExchangeConfig, load_config(), _merge_dataclass() (+354 more)

### Community 3 - "Community 3"
Cohesion: 0.03
Nodes (310): _cmd_event_risk_ws(), ResearchConfig, continuous_dataset_names(), run_continuous_protective_exit_cycle(), hedge_order_link_id(), download_market_data(), _order_link_id(), read_dataset() (+302 more)

### Community 4 - "Community 4"
Cohesion: 0.02
Nodes (242): _cmd_combined_book_telegram_report(), _cmd_event_risk_cycle(), _exclude_symbols(), finite_float(), pct(), Shared low-level helpers and constants for the liquidity_migration package.  C, The PIT *trading day* as a polars Date expression: ``date(ts_ms - 1ms)``., Coerce `value` to a finite float, returning `default` if missing/invalid. (+234 more)

### Community 5 - "Community 5"
Cohesion: 0.01
Nodes (319): BybitRestRateLimiter, Thread-safe sliding-window rate limiter shared across BybitMarketData     insta, _cmd_long_native_event_demo_cycle(), date_ms(), _date_range(), _iso_date(), _iso_month(), Parse an ISO date/datetime to epoch milliseconds (UTC). Raises on empty. (+311 more)

### Community 6 - "Community 6"
Cohesion: 0.01
Nodes (232): _collect_private_snapshots(), Return the cycle's private snapshot, preferring the WS-fed cache.      Returns, Bulk tickers from the WS cache when fresh, otherwise from REST.      Returns `, Fetch one cycle's three private REST snapshots — wallet equity, open     orders, _resolve_private_snapshot(), _resolve_ticker_snapshot(), ExecutionEventRouter, _message_rows() (+224 more)

### Community 7 - "Community 7"
Cohesion: 0.02
Nodes (281): HTMLParser, _archive_cache_is_complete(), ArchiveDownloadIncompleteError, ArchiveFileNotFoundError, _content_length(), download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive() (+273 more)

### Community 8 - "Community 8"
Cohesion: 0.02
Nodes (156): _assert_download_completeness(), build_binance_oos(), _days_between(), discover(), list_symbol_months(), list_usdm_usdt_daily_symbols(), list_usdm_usdt_symbols(), main() (+148 more)

### Community 9 - "Community 9"
Cohesion: 0.02
Nodes (144): BybitKlineStreamPool, _is_already_subscribed_error(), _KlineConnectionState, True for pybit's "You have already subscribed to this topic" error.      pybit, Per-connection bookkeeping for the pool., Multi-connection WebSocket pool for 1h kline subscriptions.      Splits a larg, Subscribe to ``symbols``. Idempotent: re-subscribing the same set         is a, Diff the current assignment against ``new_symbols``: subscribe to         adds, (+136 more)

### Community 10 - "Community 10"
Cohesion: 0.02
Nodes (163): build_parser(), _add_archive_download_klines_1h_api_parser(), _add_archive_download_klines_1h_parser(), _add_archive_download_klines_parser(), _add_archive_manifest_parser(), _add_combined_book_report_parser(), _add_continuous_addon_shadow_audit_parser(), _add_continuous_event_demo_cycle_parser() (+155 more)

### Community 11 - "Community 11"
Cohesion: 0.02
Nodes (177): account_key(), claim_symbol_reservation(), claim_symbols_reservation(), clamp_max_new_entries(), compute_im_used(), CrossSleeveAccountState, encode_control_row(), equal_split_budget() (+169 more)

### Community 12 - "Community 12"
Cohesion: 0.02
Nodes (138): FollowerKlineStreamManager, Read-only kline FOLLOWER — share one WS kline data plane across co-located sleev, Initial snapshot read + start the poll thread. Never blocks on a         bootst, Warn ONCE per staleness episode when the leader snapshot stops moving —, Drop follower-side symbols that are no longer in the leader snapshot         (t, Re-read the leader snapshot iff its (mtime, size) changed. Returns         True, ``KlineStreamManager`` drop-in that follows another root's flushed snapshot., _Bar (+130 more)

### Community 13 - "Community 13"
Cohesion: 0.02
Nodes (174): _cal_roll(), calendar_roll(), calendar_shift(), is_weekend_ms(), A per-symbol positional ``value.shift(periods).over("symbol")`` that is NULL unl, Calendar-aware rolling window over an integer timestamp grid.      Row-based `, BAC-1: calendar-aware rolling window over a per-symbol (or market) daily ts_ms g, True when the UTC day containing ``ts_ms`` is Saturday or Sunday.      Epoch d (+166 more)

### Community 14 - "Community 14"
Cohesion: 0.03
Nodes (155): component_source_paths(), ContinuousComponentSource, load_continuous_component_source(), Current frozen component source metadata for the continuous ensemble., build_full_ledger(), _config_path(), forward_readiness_summary(), ForwardUpdateResult (+147 more)

### Community 15 - "Community 15"
Cohesion: 0.04
Nodes (154): _active_trade_snapshots(), _aggregate_bars(), analyze(), _annualized_sharpe(), _append_forward_curve(), _atr_pct_before(), _bar_at_or_after(), _bars_dict_to_state_frame() (+146 more)

### Community 16 - "Community 16"
Cohesion: 0.04
Nodes (138): _cmd_continuous_addon_shadow_audit(), coerce_int(), Coerce `value` to an int, returning `default` (0) on a missing/invalid value., _active_exposure_contributor_rows(), _active_exposure_rows(), _active_exposure_summary(), _active_trade_rows_at(), _anatomy_drift() (+130 more)

### Community 17 - "Community 17"
Cohesion: 0.03
Nodes (119): BinanceDataError, BinanceUSDMData, _ceil_to_period(), _floor_to_period(), _http_error_detail(), _raise_if_suspicious_empty_page(), Seconds to wait after a 429/418, from the Retry-After header.          Falls b, Best-effort detail string from an HTTPError body (the Binance error JSON). (+111 more)

### Community 18 - "Community 18"
Cohesion: 0.03
Nodes (126): _cmd_continuous_forward_readiness(), _cmd_continuous_rebalance_cycle_audit(), _cmd_reconcile_continuous_paper_demo(), _cmd_reconcile_long_paper_demo(), audit_continuous_operational_metrics(), audit_continuous_rebalance_cycles(), _boolish(), _calendar_metrics() (+118 more)

### Community 19 - "Community 19"
Cohesion: 0.03
Nodes (107): fetch_daily_klines(), _fetch_expected_sha256(), fetch_month_klines(), parse_month_csv(), Parse a Binance Vision monthly 1h kline zip payload into kline rows.      Visi, Fetch the ``<zip>.CHECKSUM`` sidecar and return its leading sha256 hex.      d, Fail-closed integrity gate for a downloaded archive body.      A byte-corrupt, Download, integrity-verify, and parse one monthly 1h kline file.      Verifies (+99 more)

### Community 20 - "Community 20"
Cohesion: 0.04
Nodes (97): _date_symbol_set(), _float_or_nan(), _symbol_set(), _has_columns(), _btc_daily_close_series(), _chart_final_values(), _chart_font(), _date_axis_ticks() (+89 more)

### Community 21 - "Community 21"
Cohesion: 0.07
Nodes (53): BybitTradeRouter, Route order placement + cancellation through WS first, REST as fallback., Issue a WS call and block (with timeout) for the ack.          Returns the ack, Tests for BybitTradeRouter — WS-first / REST-fallback order submission.  The r, Bybit demo currently returns retCode != 0 for WS order entry; the     router mu, The double-submit race: WS submit reached Bybit but the ack     network-delayed, Probe checks open-orders first, then history. If neither has the     order, RES, A Rejected/Cancelled history row means the WS submit did NOT take effect,     s (+45 more)

### Community 22 - "Community 22"
Cohesion: 0.04
Nodes (69): Pin the Strictness Manifesto decision-rule analyzer.  Specifically validates a, Construct a cell that passes EVERY gate; verify it's labelled candidate., A near-miss is inconclusive (Bybit passes, Binance falls short on sharpe Δ)., M6: a status!=ok cell must be returned as an explicit exclusion (a     falsifie, The full_pit_universe_pass column is parsed into CellMetrics so the     analyze, At window_days = 365.25 (one calendar year), annualized = total_return.     San, At window_days = 182.625 (half year), annualized = (1+r)^2 - 1., The actual Round 1 / Phase 0 / R1 baseline at 1125 days (Bybit):     total_retu (+61 more)

### Community 23 - "Community 23"
Cohesion: 0.03
Nodes (53): Unit tests for the fast liveness/safety watchdog's pure decision logic., The collector unit can never reach systemd 'failed' (RestartSec spaces starts, The LONG sleeve runs on its own root with no rmom gate. gather_long_alerts must, Continuous-sleeve diagnosability: a zero universe / empty kline store is the sam, continuous_ensemble_v2 is intentionally no-stop demo/paper; default liveness mus, Paper evidence writes continuous_fade_paper_* datasets and submits no orders, so, The watchdog skips an intentionally-off sleeve. Explicit env always wins; the, REGRESSION (audit 2026-06-12): send_telegram_message returns False (no exception (+45 more)

### Community 24 - "Community 24"
Cohesion: 0.06
Nodes (65): _bust_demo_instruments_cache(), _concat_recent_klines(), _dedupe_recent_klines(), _demo_feature_cache_fingerprint(), _demo_feature_cache_paths(), _demo_instruments_cache_paths(), _demo_kline_compact_cache_paths(), _demo_kline_compact_metadata() (+57 more)

### Community 25 - "Community 25"
Cohesion: 0.06
Nodes (55): coverage_status(), CoverageStatus, _has_parquet(), latest_signal_trading_day(), _max_partition_date(), _per_symbol_manifest_lags(), Point-in-time coverage / staleness diagnostics for a data root.  The recurring, Return symbols with at least one parquet partition for ``day``.      The canonic (+47 more)

### Community 26 - "Community 26"
Cohesion: 0.05
Nodes (50): band_notionals(), build_arg_parser(), collect_cycle(), enforce_retention(), _file_day(), free_disk_bytes(), _get_json(), iter_jsonl_rows() (+42 more)

### Community 27 - "Community 27"
Cohesion: 0.11
Nodes (32): _decision_2f(), _open_hedge_row(), _resize_plan(), _setup_runner(), _single_decision(), test_all_legs_skipped_is_no_action_and_exits_zero(), test_armed_run_blocked_when_wallet_equity_unavailable(), test_armed_run_sizes_off_live_wallet_equity() (+24 more)

### Community 28 - "Community 28"
Cohesion: 0.19
Nodes (36): apply_rebalance_rule(), compute_continuous_hedge_ratio(), compute_hedge_beta(), compute_hedge_betas_2f(), OLS beta of per-unit book return on hedge return over trailing ledger days., Bivariate OLS betas of per-unit book return on two hedge legs.      Trailing `, Live/paper twin of the hedge sizing inside ``apply_rebalance_rule``.      Retu, Apply a causal daily scale rule and rebuild decomposed equity.      With ``hed (+28 more)

### Community 29 - "Community 29"
Cohesion: 0.08
Nodes (31): build_arg_parser(), bybit_linear_symbols(), connection_expired(), JsonlDayWriter, main(), parse_binance_event(), parse_bybit_event(), Live liquidation collectors — Bybit allLiquidation + Binance forceOrder (P3, 202 (+23 more)

### Community 30 - "Community 30"
Cohesion: 0.13
Nodes (28): _cli(), _have_rsync(), _have_scp(), _load_dotenv(), main(), pull_sleeve(), _py(), _quote() (+20 more)

### Community 31 - "Community 31"
Cohesion: 0.09
Nodes (31): _bookdepth_row(), _bookdepth_zip(), _load_backfill_bookdepth(), Regression tests for audit2 unit ``bookdepth_backfill``.  Ports the metrics-ba, When both fetch attempts exhaust retries, _day_rows yields the distinct     _TR, A genuine 404 (None blob) stays None — a real no-data day., Numerical-equivalence gate: a normal day with two snapshots in the same hour, When EVERY day fails transiently, the writer must NOT touch an empty marker (+23 more)

### Community 32 - "Community 32"
Cohesion: 0.13
Nodes (30): _load_module(), test_5m_timing_rejects_incomplete_post_entry_path(), test_adverse_limit_timing_fills_at_limit_price(), test_cluster_risk_of_ruin_bootstrap_writes_tail_injected_scenarios(), test_conditional_scale_in_tables_write_by_trade_and_summary(), test_conditional_scale_in_trade_row_models_threshold_addon(), test_delay_15m_timing_uses_5m_bar_close(), test_disaster_sizing_tables_compare_current_to_loss_budget() (+22 more)

### Community 33 - "Community 33"
Cohesion: 0.14
Nodes (28): _append(), _arm_entry(), compute_shadow_anchor(), Forward paper-shadow of the fade-completion dynamic exit (P5 follow-up, 2026-06-, Arm one entry row (fresh exec_entry OR a recovered open ledger trade).      Id, One per-cycle shadow sweep (pure bookkeeping; no orders).      Arms fresh entr, Replay the JSONL into (armed_by_trade_id, exited_trade_ids)., anchor = clip(max(runup24h, ret1), lo, hi) at the signal bar, causal.      Use (+20 more)

### Community 34 - "Community 34"
Cohesion: 0.07
Nodes (16): Pin the R1 robustness / Tier-2 demo-candidate analyzer's gate integrity.  Audi, A >=100% cumulative loss makes (1+total_return) non-positive; a fractional, audit-iter1 scripts-1: a zero-span (start==end) window floors _window_years to, audit-iter1 scripts-2: a truncated/malformed monthly CSV must signal unreadable, audit-iter5: the moving-block upper bound must be n-block+1 so a block can cover, re-audit rescan-rmom-funding-2: the non-circular moving-block bootstrap must, The max(n-block+1, 1) guard must not IndexError when n < block., test_annualize_and_engine_mar_stay_real_below_minus_100pct() (+8 more)

### Community 35 - "Community 35"
Cohesion: 0.12
Nodes (26): _load_backfill_metrics(), _metrics_row(), _metrics_zip(), Tests for scripts/backfill_binance_metrics_vision.py.  backfill-writers-2 (202, The core backfill-writers-1 fix: a previously-written parquet is UNIONED with, A re-fetched/corrected day supersedes the stale row (keep=last)., A prior parquet that already carried a `coverage` column must not break the, End-to-end through backfill_symbol: a second run that fetches a new day must (+18 more)

### Community 36 - "Community 36"
Cohesion: 0.12
Nodes (21): _mean(), Causal upper_wick entry-size multiplier retained for audit, flag-off.  The upper, Canonical pre-entry (upper_wick_mean, rv_30) from a 1m OHLC window.      upper_w, Causal per-symbol upper_wick size multiplier (flag-off audit helper).      ``pri, _std(), upper_wick_and_rv_from_ohlc(), upperwick_size_mult(), fetch_upper_wick_rv() (+13 more)

### Community 37 - "Community 37"
Cohesion: 0.15
Nodes (23): _annualize(), _block_bootstrap(), _compound(), _concentration(), _engine_mar(), _leave_one_out(), _load_json_metrics(), _load_monthly() (+15 more)

### Community 38 - "Community 38"
Cohesion: 0.16
Nodes (20): make_walk_forward_folds(), Expanding-window walk-forward folds for causal model fitting.  The rest of thi, Rows usable for training this fold (causal + embargoed)., Rows scored in this fold's test window., Expanding-window schedule, in calendar days., One expanding-window fold. All bounds are millisecond epoch.      Train rows:, Build expanding folds covering the window after the warm-up.      ``unique_ts_, select_test() (+12 more)

### Community 39 - "Community 39"
Cohesion: 0.14
Nodes (16): _ms(), Unit tests for the fill-level three-way price cross-check (scripts/reconcile_fil, test_aggregate_continuous_no_notional_column_equal_weights(), test_aggregate_continuous_notional_weights_component_legs(), test_aggregate_continuous_window_clips_and_floors_bar(), test_assemble_candidates_unions_components_and_floors_age(), test_assemble_candidates_window_clip(), test_classify_continuous_unmatched_d7_is_hard_d8_is_near() (+8 more)

### Community 40 - "Community 40"
Cohesion: 0.1
Nodes (5): _FakeJsonResponse, _Headers, _kline_row(), test_paged_kline_raises_on_suspicious_mid_range_empty_page(), test_paged_kline_terminal_empty_page_after_partial_is_benign()

### Community 41 - "Community 41"
Cohesion: 0.21
Nodes (18): diagnose(), is_tainted(), max_severity(), Named, backfillable backtest warnings — the single place that turns a run's int, True when any warning marks the result survivorship/look-ahead biased., A compact, loud block for stdout. Empty list → a single clean line., Build the warning list for a completed run from its integrity signals.      Pu, render() (+10 more)

### Community 42 - "Community 42"
Cohesion: 0.15
Nodes (12): btc_context_by_day(), _daily_closes_by_day(), ExpandingBtcRiskState, _finite(), _log_returns(), mean_present(), percentile_from_prior(), Causal BTC-risk entry-size overlay for the continuous demo book.  ``CTRL_BTC_RIS (+4 more)

### Community 43 - "Community 43"
Cohesion: 0.11
Nodes (13): _make_page(), audit2 regression: binance funding-vision backfill — two defects.  (1) In-prog, Within the publish-lag window the immediately-prior month is also protected., Build a JSON funding-rate page of n settlements starting at start_ts., Normal (unchanged) behaviour: a single short page returns exactly its rows., No data at all -> empty result (unchanged)., backfill-writers-4: vision-first ordering + unique(keep='first', maintain_order=, Normal (unchanged) behaviour: a strictly-older 404 month IS cached. (+5 more)

### Community 44 - "Community 44"
Cohesion: 0.17
Nodes (14): Functional tests for the per-sleeve kill-switch (deploy/lib_sleeves.sh + sleeves, # NOTE: the `enable` branch deliberately matches real `systemctl enable` (withou, Last-resort fallback (NEITHER sleeves.env present — a stripped checkout):     E, _run(), test_continuous_paper_split_keeps_demo_orders_off_runs_paper(), test_hedge_lifecycle_off_stops_armed_service(), test_hedge_lifecycle_off_verify_fails_when_service_active(), test_lib_fallback_defaults_every_sleeve_off() (+6 more)

### Community 45 - "Community 45"
Cohesion: 0.19
Nodes (16): CellMetrics, CellVerdict, compute_annualized_return(), compute_mar(), evaluate_cell(), evaluate_cell_investigation(), _index_by_cell(), main() (+8 more)

### Community 46 - "Community 46"
Cohesion: 0.12
Nodes (1): Regression tests for scripts/reconcile.py (audit bucket b15).  Findings covere

### Community 47 - "Community 47"
Cohesion: 0.23
Nodes (15): _continuous_payload_from_summary(), _delisted_traded(), _find_png(), _headline(), _infer_venue_from_root(), _label(), main(), _pit_verdict() (+7 more)

### Community 48 - "Community 48"
Cohesion: 0.18
Nodes (9): _drive_main(), _eq_df(), Tests for scripts/equity_curves.py (+ scripts/continuous_deployed_equity_refresh, Run ``main`` for the continuous sleeve and capture run_venue's kwargs., test_continuous_inherits_frozen_start_when_window_unset(), test_continuous_uses_explicit_start_when_given(), test_continuous_uses_rolling_start_when_years_given(), test_stats_no_drawdown_returns_none_mar() (+1 more)

### Community 49 - "Community 49"
Cohesion: 0.23
Nodes (13): backfill_symbol(), _day_rows(), _days(), _fetch(), _kline_spans(), main(), _max_date_from_file(), _merge_with_existing() (+5 more)

### Community 50 - "Community 50"
Cohesion: 0.21
Nodes (8): _mk(), Tests for the PIT coverage / staleness diagnostics (FIX C)., test_coverage_flags_per_symbol_manifest_lag_when_global_dates_match(), test_coverage_flags_per_symbol_manifest_missing_within_tail_lookback(), test_coverage_fresh_manifest_is_not_stale(), test_coverage_missing_manifest(), test_coverage_stale_manifest_flagged(), test_symbols_on_date_handles_date_first_and_symbol_first_layouts()

### Community 51 - "Community 51"
Cohesion: 0.16
Nodes (5): _naive(), audit2 regression: regenerate_hedge_warmstart.daily_returns gap-guard.  The na, The pre-fix builder: pairs adjacent present days, no calendar guard., test_contiguous_series_identical_to_naive(), test_gap_day_pair_is_dropped()

### Community 52 - "Community 52"
Cohesion: 0.3
Nodes (11): backfill_symbol(), backfill_symbol_missing_days(), _bookdepth_frame(), _day_rows(), _days(), _fetch(), _kline_spans(), main() (+3 more)

### Community 53 - "Community 53"
Cohesion: 0.33
Nodes (9): _cache_cutoff_month(), _fapi_topup(), _fetch(), _kline_spans(), main(), _month_rows(), _months(), Oldest month whose empty-404 archive must NOT be permanently cached.      The (+1 more)

### Community 54 - "Community 54"
Cohesion: 0.36
Nodes (9): _ms(), Unit tests for the three-way reconcile pairing logic (scripts/reconcile_three_wa, test_backtest_keys_missing_csv_is_empty(), test_backtest_keys_reads_entry_signal_column(), test_format_long_cadence_diagnostic_summarizes_references(), test_key_set_buckets_by_symbol_side_signal_day(), test_key_set_clips_to_window_half_open(), test_key_set_handles_missing_side_and_empty() (+1 more)

### Community 55 - "Community 55"
Cohesion: 0.31
Nodes (8): _load_orchestrator(), Tests for scripts/continuous_forward_replay_orchestrator.py.  forward-replay-5, A drift RuntimeError in one venue must be captured as a per-venue 'drift' status, A non-drift failure is reported as 'error' (still isolated), not silently swallo, A stalled clock (a venue that drifted/failed) must make main() exit non-zero so, test_orchestrator_main_exits_nonzero_on_stall(), test_orchestrator_run_venue_isolates_drift(), test_orchestrator_run_venue_isolates_generic_error()

### Community 56 - "Community 56"
Cohesion: 0.32
Nodes (6): _apply(), Causality tests for the residual-momentum precompute (scripts/precompute_residua, residual_momentum[D] = sum residual_return[D-9 .. D-3] (rolling 7, shift 3)., Look-ahead guard: residual_momentum[D] must be INVARIANT to residual_return at d, test_residual_momentum_does_not_read_future_residuals(), test_residual_momentum_is_causal_shift3()

### Community 57 - "Community 57"
Cohesion: 0.25
Nodes (1): Pin the active profile registry (liquidity_migration.promoted).  These assertion

### Community 59 - "Community 59"
Cohesion: 0.7
Nodes (4): _load_module(), test_arm_specs_pin_control_and_primary_on(), test_primary_acceptance_rejects_single_venue_mar_failure(), test_summary_metrics_gap_fills_calendar_days()

### Community 60 - "Community 60"
Cohesion: 0.5
Nodes (3): Canonical tests for liquidity_migration.momentum_signals.  test-gaps-6 (reloca, test-gaps-6: _attach_residual_momentum (orphaned SHORT-engine code whose     do, test_residual_momentum_dead_join_is_removed()

### Community 61 - "Community 61"
Cohesion: 0.67
Nodes (1): Guards against circular-import regressions in the post-refactor module split.

### Community 62 - "Community 62"
Cohesion: 1.0
Nodes (1): The manifest cannot PIT-validate the most recent daily-close signal.

### Community 63 - "Community 63"
Cohesion: 1.0
Nodes (1): Extract (orderId, orderLinkId) — accept Bybit's camelCase or the         snake_

## Knowledge Gaps
- **612 isolated node(s):** `The requested archive URL returned HTTP 404.      Distinct from transient netw`, `A downloaded archive failed an integrity check (truncated body / corrupt     co`, `Parse the response's Content-Length header to an int, or None when absent/unpars`, `archive-integrity-3: cheaply confirm a cached/just-written archive is COMPLETE b`, `Config for ``build_archive_trade_manifest``.      The manifest is always built` (+607 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 46`** (17 nodes): `_load()`, `test_scripts_reconcile.py`, `Regression tests for scripts/reconcile.py (audit bucket b15).  Findings covere`, `test_continuous_signal_check_allows_empty_or_clean_windows()`, `test_continuous_signal_check_exits_nonzero_on_hard_miss()`, `test_pull_sleeve_clears_continuous_paper_mirror_when_remote_absent()`, `test_pull_sleeve_clears_local_mirror_when_remote_dataset_empty()`, `test_pull_sleeve_refuses_stale_local_mirror_without_transfer_tool()`, `test_pull_sleeve_uses_rsync_delete_to_mirror_live_ledgers()`, `test_pull_sleeve_uses_scp_fallback_when_rsync_missing()`, `test_py_prefers_windows_venv_python()`, `test_reconcile_continuous_runs_paper_demo_gate()`, `test_reconcile_main_returns_nonzero_when_a_leg_fails()`, `test_reconcile_main_returns_zero_when_leg_clean()`, `test_summarize_leg_flags_missing_summary_as_failed_not_no_output()`, `test_summarize_leg_flags_nonzero_exit_as_failed()`, `test_summarize_leg_passes_only_on_clean_leg()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 57`** (8 nodes): `test_promoted_profiles.py`, `Pin the active profile registry (liquidity_migration.promoted).  These assertion`, `test_continuous_profile_is_deployed_book_with_regime_hedge()`, `test_long_profile_is_v11a()`, `test_promoted_trading_logic_doc_exists_and_names_lifecycles()`, `test_registry_covers_long_and_continuous()`, `test_registry_does_not_export_research_candidate_manifests()`, `test_windowing_sets_dates_on_all_sleeves()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 61`** (3 nodes): `test_package_import_integrity.py`, `Guards against circular-import regressions in the post-refactor module split.`, `test_split_sibling_imports_cold_in_fresh_process()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 62`** (1 nodes): `The manifest cannot PIT-validate the most recent daily-close signal.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 63`** (1 nodes): `Extract (orderId, orderLinkId) — accept Bybit's camelCase or the         snake_`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ResearchConfig` connect `Community 3` to `Community 0`, `Community 1`, `Community 2`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 17`?**
  _High betweenness centrality (0.137) - this node is a cross-community bridge._
- **Why does `write_dataset()` connect `Community 3` to `Community 0`, `Community 2`, `Community 4`, `Community 5`, `Community 7`, `Community 8`, `Community 39`, `Community 11`, `Community 16`, `Community 17`, `Community 18`, `Community 20`, `Community 53`, `Community 23`, `Community 24`?**
  _High betweenness centrality (0.086) - this node is a cross-community bridge._
- **Why does `ContinuousDemoCycleConfig` connect `Community 1` to `Community 0`, `Community 3`, `Community 4`, `Community 36`, `Community 7`, `Community 10`, `Community 11`, `Community 12`, `Community 18`?**
  _High betweenness centrality (0.062) - this node is a cross-community bridge._
- **Are the 604 inferred relationships involving `ResearchConfig` (e.g. with `Coverage table + (when stale) a prominent WARNING that download-data does not` and `Resolve the data root for a CLI command: commands that don't use it get the path`) actually correct?**
  _`ResearchConfig` has 604 INFERRED edges - model-reasoned connections that need verification._
- **Are the 208 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateWebSocketStream` and `BybitPublicTickerStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 208 INFERRED edges - model-reasoned connections that need verification._
- **Are the 269 inferred relationships involving `ContinuousDemoCycleConfig` (e.g. with `Coverage table + (when stale) a prominent WARNING that download-data does not` and `Resolve the data root for a CLI command: commands that don't use it get the path`) actually correct?**
  _`ContinuousDemoCycleConfig` has 269 INFERRED edges - model-reasoned connections that need verification._
- **Are the 246 inferred relationships involving `EventDemoCycleConfig` (e.g. with `ContinuousDemoCycleConfig` and `LivePanelCache`) actually correct?**
  _`EventDemoCycleConfig` has 246 INFERRED edges - model-reasoned connections that need verification._