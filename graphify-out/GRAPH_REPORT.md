# Graph Report - MODEL050426  (2026-05-05)

## Corpus Check
- 69 files · ~77,369 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1010 nodes · 2563 edges · 33 communities detected
- Extraction: 78% EXTRACTED · 22% INFERRED · 0% AMBIGUOUS · INFERRED: 572 edges (avg confidence: 0.77)
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
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 37|Community 37]]

## God Nodes (most connected - your core abstractions)
1. `read_dataset()` - 53 edges
2. `main()` - 47 edges
3. `ResearchConfig` - 46 edges
4. `DailyCloseFadeConfig` - 40 edges
5. `write_dataset()` - 34 edges
6. `_FakeExecution` - 30 edges
7. `CostConfig` - 29 edges
8. `run_volume_trade_backtest()` - 27 edges
9. `run_bybit_demo_sync()` - 27 edges
10. `run_daily_close_fade()` - 26 edges

## Surprising Connections (you probably didn't know these)
- `trades_to_frame()` --calls--> `test_trade_parser_handles_websocket_aliases_and_string_booleans()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py
- `run_volume_grid()` --calls--> `main()`  [INFERRED]
  aggression_carry/volume_backtest.py → scripts/run_volume_bucket_sweep.py
- `run_volume_grid()` --calls--> `main()`  [INFERRED]
  aggression_carry/volume_backtest.py → scripts/run_volume_grid_splits.py
- `run_volume_alpha()` --calls--> `test_volume_alpha_isolated_daily_research_path()`  [INFERRED]
  aggression_carry/volume_alpha.py → tests/test_aggression_carry_volume_alpha.py
- `build_volume_features()` --calls--> `test_volume_alpha_isolated_daily_research_path()`  [INFERRED]
  aggression_carry/volume_alpha.py → tests/test_aggression_carry_volume_alpha.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.04
Nodes (84): download_archive_bytes(), download_public_trade_archive(), ArchiveKlineDownloadConfig, ArchiveManifestConfig, build_archive_trade_manifest(), _download_one_archive_kline(), _download_result(), _empty_download_results() (+76 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (88): VolumeBacktestConfig, VolumeGridConfig, _demo_cycle_lock(), generate_fixture_data(), _attribution_rows(), backtest_volume_trades(), _bar_at_close(), _bar_excursion() (+80 more)

### Community 2 - "Community 2"
Cohesion: 0.07
Nodes (67): _apply_basket_stop_to_rows(), apply_close_fade_basket_stop(), _attach_archive_membership(), attach_close_fade_coin_market_context(), attach_close_fade_position_sizing(), _attach_instrument_age(), backtest_daily_close_fade(), _baseline_liquidity_filter_expr() (+59 more)

### Community 3 - "Community 3"
Cohesion: 0.1
Nodes (36): BybitDataError, BybitMarketData, BybitPrivateClient, ResearchConfig, _demo_executor(), DemoCancelAllConfig, DemoFlattenConfig, DemoProbeConfig (+28 more)

### Community 4 - "Community 4"
Cohesion: 0.1
Nodes (47): CostConfig, DailyCloseFadeConfig, DailyCloseFadeGridConfig, DailyCloseFadeDiagnosticsConfig, run_daily_close_fade(), read_dataset(), _feature_candidate(), test_daily_close_fade_basket_stop_exits_open_basket() (+39 more)

### Community 5 - "Community 5"
Cohesion: 0.08
Nodes (54): _as_utc(), build_demo_sync_orders(), _build_limit_order(), _build_probe_order(), cancel_stale_demo_orders(), _candidate_order_row(), _cap_candidate_order_rows(), _capped_order_qty() (+46 more)

### Community 6 - "Community 6"
Cohesion: 0.09
Nodes (52): _as_utc(), _basket_already_opened(), _bool_value(), build_forward_scan_features(), build_forward_universe(), _concat(), _count_reason(), _count_status() (+44 more)

### Community 7 - "Community 7"
Cohesion: 0.09
Nodes (35): _as_utc(), _compact_token(), _demo_sync_compat_context(), _demo_sync_supports_entry_pause(), DemoCycleConfig, _existing_active_state(), _failed_sleeve_result(), format_demo_cycle_report() (+27 more)

### Community 8 - "Community 8"
Cohesion: 0.12
Nodes (43): _as_utc(), _avg_fill_price(), build_forward_demo_audit_rows(), build_forward_demo_daily_summary(), _demo_realized_pnl(), _entry_slippage_bps(), _eod_ready(), _eod_telegram_events() (+35 more)

### Community 9 - "Community 9"
Cohesion: 0.14
Nodes (34): _add_forward_fade_args(), _add_forward_runtime_args(), _add_universe_backtest_args(), _apply_universe_backtest_args(), _backtest_config_from_args(), build_parser(), _close_fade_base_from_diagnostics_args(), _close_fade_base_from_grid_args() (+26 more)

### Community 10 - "Community 10"
Cohesion: 0.12
Nodes (28): ExchangeConfig, ForwardTestConfig, load_config(), _merge_daily_close_fade_config(), _merge_daily_close_fade_grid_config(), _merge_dataclass(), _merge_forward_test_config(), _merge_universe_config() (+20 more)

### Community 11 - "Community 11"
Cohesion: 0.15
Nodes (28): _base_config(), build_btc_signal_context(), build_context_bucket_summary(), build_day_audit_rows(), build_exit_summary(), build_monthly_summary(), build_win_loss_contrast(), _candidate_context() (+20 more)

### Community 12 - "Community 12"
Cohesion: 0.16
Nodes (26): build_archive_pit_coverage(), _coverage_aggs(), _coverage_rates(), _coverage_status(), _coverage_thresholds_pass(), _csv_symbols(), _filtered_manifest(), format_archive_pit_coverage_report() (+18 more)

### Community 13 - "Community 13"
Cohesion: 0.16
Nodes (24): summarize_close_fade_baskets(), apply_coin_filter(), attach_coin_market_context(), _base_config(), _basket_sets_by_allocation(), build_coin_filter_specs(), CoinFilterSpec, _csv_float() (+16 more)

### Community 14 - "Community 14"
Cohesion: 0.14
Nodes (20): summarize_close_fade_diagnostic_ic(), _ordinal_rank(), rank_correlation(), _add_cross_sectional_z(), _add_liquidity_rank(), attach_volume_forward_returns(), _best_base_portfolio(), build_volume_features() (+12 more)

### Community 15 - "Community 15"
Cohesion: 0.17
Nodes (23): _base_config(), build_prior_daily_ema_regime(), build_prior_daily_ema_regime_from_klines(), _calendar_daily_returns(), _csv_float(), _csv_int(), _csv_str(), _date_range() (+15 more)

### Community 16 - "Community 16"
Cohesion: 0.18
Nodes (21): build_sizing_specs(), build_weighted_baskets(), capped_proportional_weights(), _csv_float(), _csv_str(), _equity_from_returns(), evaluate_sizing_sweep(), _filter_signal_window() (+13 more)

### Community 17 - "Community 17"
Cohesion: 0.18
Nodes (21): build_volume_promotion_table(), _empty_promotion_table(), format_volume_promotion_report(), main(), _num(), _number(), parse_args(), _pct() (+13 more)

### Community 18 - "Community 18"
Cohesion: 0.2
Nodes (19): build_filter_specs(), ContextFilterSpec, _csv_float(), _csv_int(), _csv_str(), _equity_from_returns(), evaluate_filter_sweep(), _filter_split() (+11 more)

### Community 19 - "Community 19"
Cohesion: 0.28
Nodes (16): _base_config(), _csv_bool(), _csv_float(), _csv_int(), _csv_str(), _date_ms(), format_volume_grid_split_summary(), _grid_config() (+8 more)

### Community 20 - "Community 20"
Cohesion: 0.3
Nodes (14): _base_config(), _csv_float(), _csv_int(), _csv_signal_minutes(), _csv_str(), format_grid_split_summary(), _format_signal_minute(), _grid_config() (+6 more)

### Community 21 - "Community 21"
Cohesion: 0.29
Nodes (12): build_promotion_table(), _empty_promotion_table(), format_promotion_report(), _format_signal_minute(), main(), _num(), _number(), parse_args() (+4 more)

### Community 22 - "Community 22"
Cohesion: 0.35
Nodes (12): _base_config(), _csv_int(), _csv_signal_minutes(), _csv_str(), _format_signal_minute(), format_split_summary(), main(), _num() (+4 more)

### Community 23 - "Community 23"
Cohesion: 0.32
Nodes (11): _artifact_key(), _artifact_path_list(), artifact_row(), build_research_manifest(), format_research_manifest(), _git(), git_metadata(), main() (+3 more)

### Community 24 - "Community 24"
Cohesion: 0.44
Nodes (9): evaluate_pit_coverage(), evaluate_promotion(), format_readiness_report(), main(), _missing_check(), _number(), overall_status(), parse_args() (+1 more)

### Community 26 - "Community 26"
Cohesion: 0.47
Nodes (3): _feature(), test_day_audit_joins_pre_signal_context_without_using_post_trade_path(), _trade()

### Community 27 - "Community 27"
Cohesion: 0.47
Nodes (4): _bar(), _result(), test_prior_daily_ema_regime_uses_previous_completed_daily_close(), test_regime_stability_prefers_split_survival_over_big_single_window()

### Community 28 - "Community 28"
Cohesion: 0.6
Nodes (4): _day(), _result(), test_filter_summary_prefers_stable_positive_splits(), test_filter_sweep_uses_zero_return_for_skipped_days()

### Community 30 - "Community 30"
Cohesion: 0.83
Nodes (3): _diagnostic(), _grid(), test_promotion_requires_raw_and_exit_split_survival()

### Community 31 - "Community 31"
Cohesion: 0.67
Nodes (2): _scenario(), test_split_summary_prefers_scenarios_that_survive_every_split()

### Community 32 - "Community 32"
Cohesion: 0.67
Nodes (2): test_volume_grid_split_summary_prefers_stable_variants(), _variant()

### Community 35 - "Community 35"
Cohesion: 1.0
Nodes (2): _candidate(), test_volume_promotion_requires_split_survival_and_drawdown()

### Community 37 - "Community 37"
Cohesion: 1.0
Nodes (2): test_grid_split_summary_prefers_stable_variants(), _variant()

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit volume-alpha research package.  This package is the stripped-down rebuild`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 31`** (4 nodes): `test_daily_close_fade_split_diagnostics_script.py`, `_scenario()`, `test_parse_splits_requires_ordered_windows()`, `test_split_summary_prefers_scenarios_that_survive_every_split()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 32`** (4 nodes): `test_volume_grid_splits_script.py`, `test_parse_splits_requires_ordered_windows()`, `test_volume_grid_split_summary_prefers_stable_variants()`, `_variant()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 35`** (3 nodes): `_candidate()`, `test_volume_promotion_script.py`, `test_volume_promotion_requires_split_survival_and_drawdown()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 37`** (3 nodes): `test_daily_close_fade_grid_splits_script.py`, `test_grid_split_summary_prefers_stable_variants()`, `_variant()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `main()` connect `Community 9` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 8`, `Community 10`, `Community 14`?**
  _High betweenness centrality (0.141) - this node is a cross-community bridge._
- **Why does `load_config()` connect `Community 10` to `Community 1`, `Community 3`, `Community 9`, `Community 11`, `Community 13`, `Community 15`, `Community 16`, `Community 17`, `Community 19`, `Community 20`, `Community 22`?**
  _High betweenness centrality (0.108) - this node is a cross-community bridge._
- **Why does `parse_date_ms()` connect `Community 9` to `Community 0`, `Community 11`, `Community 13`, `Community 15`, `Community 16`, `Community 18`, `Community 20`, `Community 22`?**
  _High betweenness centrality (0.081) - this node is a cross-community bridge._
- **Are the 51 inferred relationships involving `read_dataset()` (e.g. with `run_volume_trade_backtest()` and `run_volume_grid()`) actually correct?**
  _`read_dataset()` has 51 INFERRED edges - model-reasoned connections that need verification._
- **Are the 32 inferred relationships involving `main()` (e.g. with `load_config()` and `generate_fixture_data()`) actually correct?**
  _`main()` has 32 INFERRED edges - model-reasoned connections that need verification._
- **Are the 44 inferred relationships involving `ResearchConfig` (e.g. with `DemoCycleConfig` and `DemoProbeConfig`) actually correct?**
  _`ResearchConfig` has 44 INFERRED edges - model-reasoned connections that need verification._
- **Are the 38 inferred relationships involving `DailyCloseFadeConfig` (e.g. with `DemoCycleConfig` and `DailyCloseFadeDiagnosticsConfig`) actually correct?**
  _`DailyCloseFadeConfig` has 38 INFERRED edges - model-reasoned connections that need verification._