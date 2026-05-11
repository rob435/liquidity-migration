# Graph Report - MODEL050426  (2026-05-10)

## Corpus Check
- 75 files · ~97,925 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1188 nodes · 2964 edges · 38 communities detected
- Extraction: 78% EXTRACTED · 22% INFERRED · 0% AMBIGUOUS · INFERRED: 642 edges (avg confidence: 0.78)
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
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 45|Community 45]]

## God Nodes (most connected - your core abstractions)
1. `read_dataset()` - 69 edges
2. `DailyCloseFadeConfig` - 49 edges
3. `main()` - 48 edges
4. `write_dataset()` - 48 edges
5. `ResearchConfig` - 46 edges
6. `CostConfig` - 34 edges
7. `run_daily_close_fade()` - 34 edges
8. `_FakeExecution` - 30 edges
9. `run_volume_trade_backtest()` - 29 edges
10. `BybitMarketData` - 27 edges

## Surprising Connections (you probably didn't know these)
- `test_trade_parser_handles_websocket_aliases_and_string_booleans()` --calls--> `trades_to_frame()`  [INFERRED]
  tests/test_aggression_carry_ingestion.py → aggression_carry/ingestion.py
- `main()` --calls--> `run_volume_grid()`  [INFERRED]
  scripts/run_volume_bucket_sweep.py → aggression_carry/volume_backtest.py
- `main()` --calls--> `run_volume_grid()`  [INFERRED]
  scripts/run_volume_grid_splits.py → aggression_carry/volume_backtest.py
- `test_volume_alpha_isolated_daily_research_path()` --calls--> `run_volume_alpha()`  [INFERRED]
  tests/test_aggression_carry_volume_alpha.py → aggression_carry/volume_alpha.py
- `test_volume_alpha_isolated_daily_research_path()` --calls--> `build_volume_features()`  [INFERRED]
  tests/test_aggression_carry_volume_alpha.py → aggression_carry/volume_alpha.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.04
Nodes (91): download_archive_bytes(), download_public_trade_archive(), _archive_outputs_ready(), ArchiveKlineDownloadConfig, ArchiveManifestConfig, build_archive_trade_manifest(), _download_one_archive_kline(), _download_result() (+83 more)

### Community 1 - "Community 1"
Cohesion: 0.06
Nodes (88): ResearchConfig, _as_utc(), build_demo_sync_orders(), _build_limit_order(), _build_probe_order(), cancel_stale_demo_orders(), _candidate_order_row(), _cap_candidate_order_rows() (+80 more)

### Community 2 - "Community 2"
Cohesion: 0.05
Nodes (95): VolumeBacktestConfig, VolumeGridConfig, _demo_cycle_lock(), generate_fixture_data(), _attribution_rows(), backtest_volume_trades(), _bar_at_close(), _bar_excursion() (+87 more)

### Community 3 - "Community 3"
Cohesion: 0.05
Nodes (94): _all_context_rate(), _apply_basket_stop_to_rows(), apply_close_fade_basket_stop(), _attach_archive_membership(), attach_close_fade_optional_context(), attach_close_fade_position_sizing(), _attach_funding_context(), _attach_instrument_age() (+86 more)

### Community 4 - "Community 4"
Cohesion: 0.1
Nodes (55): CostConfig, DailyCloseFadeConfig, DailyCloseFadeGridConfig, DailyCloseFadeDiagnosticsConfig, run_daily_close_fade(), read_dataset(), _feature_candidate(), test_daily_close_fade_basket_stop_exits_open_basket() (+47 more)

### Community 5 - "Community 5"
Cohesion: 0.09
Nodes (54): attach_close_fade_coin_market_context(), _as_utc(), _basket_already_opened(), _bool_value(), build_forward_scan_features(), build_forward_universe(), _concat(), _count_reason() (+46 more)

### Community 6 - "Community 6"
Cohesion: 0.09
Nodes (35): _as_utc(), _compact_token(), _demo_sync_compat_context(), _demo_sync_supports_entry_pause(), DemoCycleConfig, _existing_active_state(), _failed_sleeve_result(), format_demo_cycle_report() (+27 more)

### Community 7 - "Community 7"
Cohesion: 0.12
Nodes (43): _as_utc(), _avg_fill_price(), build_forward_demo_audit_rows(), build_forward_demo_daily_summary(), _demo_realized_pnl(), _entry_slippage_bps(), _eod_ready(), _eod_telegram_events() (+35 more)

### Community 8 - "Community 8"
Cohesion: 0.1
Nodes (43): archive_coverage_blockers(), build_archive_coverage_summary(), build_context_coverage_summary(), build_dataset_status(), build_filtered_archive_manifest(), _capacity_enabled(), context_coverage_blockers(), _context_gate_ok() (+35 more)

### Community 9 - "Community 9"
Cohesion: 0.09
Nodes (14): BybitDataError, BybitMarketData, BybitPrivateClient, _demo_executor(), _age_filter_label(), build_current_universe_table(), _empty_universe_table(), format_universe_report() (+6 more)

### Community 10 - "Community 10"
Cohesion: 0.13
Nodes (36): _add_forward_fade_args(), _add_forward_runtime_args(), _add_universe_backtest_args(), _apply_universe_backtest_args(), _backtest_config_from_args(), build_parser(), _close_fade_base_from_diagnostics_args(), _close_fade_base_from_grid_args() (+28 more)

### Community 11 - "Community 11"
Cohesion: 0.13
Nodes (33): build_archive_pit_coverage(), _coverage_aggs(), _coverage_rates(), _coverage_status(), _coverage_thresholds_pass(), _csv_symbols(), _filtered_manifest(), format_archive_pit_coverage_report() (+25 more)

### Community 12 - "Community 12"
Cohesion: 0.14
Nodes (26): ExchangeConfig, ForwardTestConfig, load_config(), _merge_daily_close_fade_config(), _merge_daily_close_fade_grid_config(), _merge_dataclass(), _merge_forward_test_config(), _merge_universe_config() (+18 more)

### Community 13 - "Community 13"
Cohesion: 0.15
Nodes (28): _base_config(), build_btc_signal_context(), build_context_bucket_summary(), build_day_audit_rows(), build_exit_summary(), build_monthly_summary(), build_win_loss_contrast(), _candidate_context() (+20 more)

### Community 14 - "Community 14"
Cohesion: 0.17
Nodes (23): apply_coin_filter(), attach_coin_market_context(), _base_config(), _basket_sets_by_allocation(), build_coin_filter_specs(), CoinFilterSpec, _csv_float(), _csv_int() (+15 more)

### Community 15 - "Community 15"
Cohesion: 0.15
Nodes (19): _ordinal_rank(), rank_correlation(), _add_cross_sectional_z(), _add_liquidity_rank(), attach_volume_forward_returns(), _best_base_portfolio(), build_volume_features(), compute_volume_metrics() (+11 more)

### Community 16 - "Community 16"
Cohesion: 0.17
Nodes (23): _base_config(), build_prior_daily_ema_regime(), build_prior_daily_ema_regime_from_klines(), _calendar_daily_returns(), _csv_float(), _csv_int(), _csv_str(), _date_range() (+15 more)

### Community 17 - "Community 17"
Cohesion: 0.18
Nodes (21): build_sizing_specs(), build_weighted_baskets(), capped_proportional_weights(), _csv_float(), _csv_str(), _equity_from_returns(), evaluate_sizing_sweep(), _filter_signal_window() (+13 more)

### Community 18 - "Community 18"
Cohesion: 0.18
Nodes (20): build_filter_specs(), ContextFilterSpec, _csv_float(), _csv_int(), _csv_str(), _equity_from_returns(), evaluate_filter_sweep(), _filter_split() (+12 more)

### Community 19 - "Community 19"
Cohesion: 0.24
Nodes (18): audit_label(), AuditGate, close_fade_audit(), close_fade_lifecycle(), config_hash(), _context_datasets_present(), data_identity(), _finite() (+10 more)

### Community 20 - "Community 20"
Cohesion: 0.28
Nodes (16): _base_config(), _csv_bool(), _csv_float(), _csv_int(), _csv_str(), _date_ms(), format_volume_grid_split_summary(), _grid_config() (+8 more)

### Community 21 - "Community 21"
Cohesion: 0.3
Nodes (14): _base_config(), _csv_float(), _csv_int(), _csv_signal_minutes(), _csv_str(), format_grid_split_summary(), _format_signal_minute(), _grid_config() (+6 more)

### Community 22 - "Community 22"
Cohesion: 0.26
Nodes (14): _artifact_line(), build_research_run_record(), _bullet_section(), _compact_record(), _data_root_row(), format_research_log(), format_research_run_record(), _gate_rows() (+6 more)

### Community 23 - "Community 23"
Cohesion: 0.26
Nodes (9): ArchiveLiquidityUniverseConfig, _csv_symbols(), _format_report(), main(), parse_args(), _rank_manifest_rows_by_content_length(), select_archive_liquidity_universe(), _start_date_manifest() (+1 more)

### Community 24 - "Community 24"
Cohesion: 0.29
Nodes (12): build_promotion_table(), _empty_promotion_table(), format_promotion_report(), _format_signal_minute(), main(), _num(), _number(), parse_args() (+4 more)

### Community 25 - "Community 25"
Cohesion: 0.38
Nodes (12): evaluate_close_fade_profit_protection(), evaluate_pit_coverage(), evaluate_promotion(), format_readiness_report(), main(), _missing_check(), _number(), overall_status() (+4 more)

### Community 26 - "Community 26"
Cohesion: 0.35
Nodes (12): _base_config(), _csv_int(), _csv_signal_minutes(), _csv_str(), _format_signal_minute(), format_split_summary(), main(), _num() (+4 more)

### Community 27 - "Community 27"
Cohesion: 0.32
Nodes (12): build_volume_promotion_table(), _empty_promotion_table(), format_volume_promotion_report(), main(), _num(), _number(), parse_args(), _pct() (+4 more)

### Community 28 - "Community 28"
Cohesion: 0.32
Nodes (11): _artifact_key(), _artifact_path_list(), artifact_row(), build_research_manifest(), format_research_manifest(), _git(), git_metadata(), main() (+3 more)

### Community 30 - "Community 30"
Cohesion: 0.47
Nodes (3): _feature(), test_day_audit_joins_pre_signal_context_without_using_post_trade_path(), _trade()

### Community 31 - "Community 31"
Cohesion: 0.47
Nodes (4): _bar(), _result(), test_prior_daily_ema_regime_uses_previous_completed_daily_close(), test_regime_stability_prefers_split_survival_over_big_single_window()

### Community 33 - "Community 33"
Cohesion: 0.5
Nodes (2): test_build_weighted_baskets_applies_equal_concentration_cap(), _trade()

### Community 34 - "Community 34"
Cohesion: 0.6
Nodes (4): _day(), _result(), test_filter_summary_prefers_stable_positive_splits(), test_filter_sweep_uses_zero_return_for_skipped_days()

### Community 37 - "Community 37"
Cohesion: 0.83
Nodes (3): _diagnostic(), _grid(), test_promotion_requires_raw_and_exit_split_survival()

### Community 38 - "Community 38"
Cohesion: 0.67
Nodes (2): _scenario(), test_split_summary_prefers_scenarios_that_survive_every_split()

### Community 39 - "Community 39"
Cohesion: 0.67
Nodes (2): test_volume_grid_split_summary_prefers_stable_variants(), _variant()

### Community 43 - "Community 43"
Cohesion: 1.0
Nodes (2): _candidate(), test_volume_promotion_requires_split_survival_and_drawdown()

### Community 45 - "Community 45"
Cohesion: 1.0
Nodes (2): test_grid_split_summary_prefers_stable_variants(), _variant()

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit volume-alpha research package.  This package is the stripped-down rebuild`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 33`** (5 nodes): `test_daily_close_fade_sizing_sweep_script.py`, `test_build_weighted_baskets_applies_equal_concentration_cap()`, `test_capped_proportional_weights_leaves_cash_when_all_names_hit_cap()`, `test_capped_proportional_weights_redistribute_until_cap()`, `_trade()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 38`** (4 nodes): `test_daily_close_fade_split_diagnostics_script.py`, `_scenario()`, `test_parse_splits_requires_ordered_windows()`, `test_split_summary_prefers_scenarios_that_survive_every_split()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 39`** (4 nodes): `test_volume_grid_splits_script.py`, `test_parse_splits_requires_ordered_windows()`, `test_volume_grid_split_summary_prefers_stable_variants()`, `_variant()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 43`** (3 nodes): `_candidate()`, `test_volume_promotion_script.py`, `test_volume_promotion_requires_split_survival_and_drawdown()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 45`** (3 nodes): `test_daily_close_fade_grid_splits_script.py`, `test_grid_split_summary_prefers_stable_variants()`, `_variant()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `main()` connect `Community 10` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 9`, `Community 12`, `Community 15`?**
  _High betweenness centrality (0.100) - this node is a cross-community bridge._
- **Why does `load_config()` connect `Community 12` to `Community 1`, `Community 2`, `Community 8`, `Community 10`, `Community 13`, `Community 14`, `Community 16`, `Community 17`, `Community 20`, `Community 21`, `Community 26`?**
  _High betweenness centrality (0.092) - this node is a cross-community bridge._
- **Why does `parse_date_ms()` connect `Community 0` to `Community 8`, `Community 10`, `Community 13`, `Community 14`, `Community 16`, `Community 17`, `Community 18`, `Community 21`, `Community 26`?**
  _High betweenness centrality (0.068) - this node is a cross-community bridge._
- **Are the 67 inferred relationships involving `read_dataset()` (e.g. with `run_volume_trade_backtest()` and `run_volume_grid()`) actually correct?**
  _`read_dataset()` has 67 INFERRED edges - model-reasoned connections that need verification._
- **Are the 47 inferred relationships involving `DailyCloseFadeConfig` (e.g. with `DemoCycleConfig` and `DailyCloseFadeDiagnosticsConfig`) actually correct?**
  _`DailyCloseFadeConfig` has 47 INFERRED edges - model-reasoned connections that need verification._
- **Are the 32 inferred relationships involving `main()` (e.g. with `load_config()` and `generate_fixture_data()`) actually correct?**
  _`main()` has 32 INFERRED edges - model-reasoned connections that need verification._
- **Are the 43 inferred relationships involving `write_dataset()` (e.g. with `generate_fixture_data()` and `_replace_dataset()`) actually correct?**
  _`write_dataset()` has 43 INFERRED edges - model-reasoned connections that need verification._