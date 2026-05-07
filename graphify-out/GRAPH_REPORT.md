# Graph Report - MODEL050426  (2026-05-08)

## Corpus Check
- 45 files · ~74,977 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 963 nodes · 2940 edges · 19 communities detected
- Extraction: 72% EXTRACTED · 28% INFERRED · 0% AMBIGUOUS · INFERRED: 810 edges (avg confidence: 0.76)
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
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 20|Community 20]]

## God Nodes (most connected - your core abstractions)
1. `ResearchConfig` - 91 edges
2. `read_dataset()` - 90 edges
3. `write_dataset()` - 57 edges
4. `main()` - 49 edges
5. `_FakeExecution` - 44 edges
6. `run_bybit_demo_sync()` - 43 edges
7. `DemoSyncConfig` - 41 edges
8. `_FakeMarket` - 37 edges
9. `BybitMarketData` - 35 edges
10. `CostConfig` - 32 edges

## Surprising Connections (you probably didn't know these)
- `load_config()` --calls--> `test_volume_alpha_controls_load_from_yaml()`  [INFERRED]
  aggression_carry/config.py → tests/test_aggression_carry_config.py
- `trades_to_frame()` --calls--> `test_trade_parser_handles_websocket_aliases_and_string_booleans()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py
- `run_volume_grid()` --calls--> `main()`  [INFERRED]
  aggression_carry/volume_backtest.py → scripts/run_volume_bucket_sweep.py
- `run_volume_grid()` --calls--> `main()`  [INFERRED]
  aggression_carry/volume_backtest.py → scripts/run_volume_grid_splits.py
- `DemoCycleConfig` --calls--> `main()`  [INFERRED]
  aggression_carry/demo_cycle.py → scripts/run_hourly_demo_full_system_test.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.04
Nodes (102): download_archive_bytes(), download_public_trade_archive(), ArchiveKlineDownloadConfig, ArchiveManifestConfig, build_archive_trade_manifest(), _delete_local_archive(), _download_one_archive_kline(), _download_result() (+94 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (88): VolumeBacktestConfig, VolumeGridConfig, generate_fixture_data(), _attribution_rows(), backtest_volume_trades(), _bar_at_close(), _bar_excursion(), _bar_exit_hits() (+80 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (79): _as_utc(), _bounded_exit_limit_price(), build_demo_sync_orders(), _build_limit_order(), _build_probe_order(), _cancel_open_entry_orders_for_symbols(), cancel_stale_demo_orders(), _candidate_order_row() (+71 more)

### Community 3 - "Community 3"
Cohesion: 0.08
Nodes (69): CostConfig, DailyCloseFadeConfig, DailyCloseFadeGridConfig, ForwardTestConfig, DailyCloseFadeDiagnosticsConfig, Return canonical entry child timestamps for the daily-close fade., run_daily_close_fade(), run_forward_once() (+61 more)

### Community 4 - "Community 4"
Cohesion: 0.07
Nodes (69): _apply_basket_stop_to_rows(), apply_close_fade_basket_stop(), _attach_archive_membership(), attach_close_fade_position_sizing(), _attach_instrument_age(), backtest_daily_close_fade(), _bar_open(), _baseline_liquidity_filter_expr() (+61 more)

### Community 5 - "Community 5"
Cohesion: 0.08
Nodes (67): attach_close_fade_coin_market_context(), close_fade_entry_child_schedule_ts_ms(), _mfe_giveback_stop_price(), _vwap_reversion_exit_price(), _mfe_giveback_stop_price(), _short_return(), _as_utc(), _basket_already_opened() (+59 more)

### Community 6 - "Community 6"
Cohesion: 0.13
Nodes (50): ResearchConfig, DemoCancelAllConfig, DemoFlattenConfig, DemoProbeConfig, DemoSyncConfig, run_bybit_demo_sync(), write_dataset(), _CountingExecution (+42 more)

### Community 7 - "Community 7"
Cohesion: 0.09
Nodes (63): _aggregate_orders(), _aggregate_status(), _as_utc(), _avg_fill_price(), build_forward_demo_audit_rows(), build_forward_demo_audit_slice_rows(), build_forward_demo_daily_summary(), build_forward_demo_slice_daily_summary() (+55 more)

### Community 8 - "Community 8"
Cohesion: 0.1
Nodes (50): BybitPublicTradeStream, _active_trade_states(), _arm_profit_protection(), _as_utc(), _clear_state(), DemoFastProtectionConfig, _dt_from_ms(), _entry_trade_ids_with_exposure() (+42 more)

### Community 9 - "Community 9"
Cohesion: 0.1
Nodes (43): _as_utc(), _demo_cycle_lock(), DemoCycleConfig, _existing_active_state(), _failed_sleeve_result(), format_demo_cycle_report(), _inactive_sleeve_result(), _is_active_window() (+35 more)

### Community 10 - "Community 10"
Cohesion: 0.12
Nodes (39): _add_forward_fade_args(), _add_forward_runtime_args(), _add_universe_backtest_args(), _apply_universe_backtest_args(), _arg(), _backtest_config_from_args(), build_parser(), _close_fade_base_from_diagnostics_args() (+31 more)

### Community 11 - "Community 11"
Cohesion: 0.11
Nodes (8): BybitDataError, BybitMarketData, BybitPrivateClient, _is_rate_limit(), _leverage_text(), _demo_executor(), Submit one idempotent reduce-only exit through the shared demo ledger., Submit one idempotent reduce-only exit through the shared demo ledger.

### Community 12 - "Community 12"
Cohesion: 0.17
Nodes (23): ExchangeConfig, load_config(), _merge_daily_close_fade_config(), _merge_daily_close_fade_grid_config(), _merge_dataclass(), _merge_forward_test_config(), _merge_universe_config(), _merge_volume_alpha_config() (+15 more)

### Community 13 - "Community 13"
Cohesion: 0.18
Nodes (19): _ordinal_rank(), rank_correlation(), _add_cross_sectional_z(), _add_liquidity_rank(), attach_volume_forward_returns(), _best_base_portfolio(), build_volume_features(), compute_volume_metrics() (+11 more)

### Community 14 - "Community 14"
Cohesion: 0.28
Nodes (16): _base_config(), _csv_bool(), _csv_float(), _csv_int(), _csv_str(), _date_ms(), format_volume_grid_split_summary(), _grid_config() (+8 more)

### Community 16 - "Community 16"
Cohesion: 0.25
Nodes (12): _all_paper_trades_closed(), _append_event(), _candidate_count(), _compact(), _compact_sleeve(), _demo_execution_resolved(), main(), _signal_datetime() (+4 more)

### Community 17 - "Community 17"
Cohesion: 0.29
Nodes (13): build_volume_promotion_table(), _empty_promotion_table(), format_volume_promotion_report(), main(), _num(), _number(), parse_args(), _pct() (+5 more)

### Community 18 - "Community 18"
Cohesion: 0.67
Nodes (2): test_volume_grid_split_summary_prefers_stable_variants(), _variant()

### Community 20 - "Community 20"
Cohesion: 1.0
Nodes (2): _candidate(), test_volume_promotion_requires_split_survival_and_drawdown()

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit volume-alpha research package.  This package is the stripped-down rebuild`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 18`** (4 nodes): `test_volume_grid_splits_script.py`, `test_parse_splits_requires_ordered_windows()`, `test_volume_grid_split_summary_prefers_stable_variants()`, `_variant()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 20`** (3 nodes): `_candidate()`, `test_volume_promotion_script.py`, `test_volume_promotion_requires_split_survival_and_drawdown()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `read_dataset()` connect `Community 3` to `Community 0`, `Community 1`, `Community 2`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 8`, `Community 9`, `Community 13`?**
  _High betweenness centrality (0.114) - this node is a cross-community bridge._
- **Why does `main()` connect `Community 10` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 9`, `Community 12`, `Community 13`?**
  _High betweenness centrality (0.106) - this node is a cross-community bridge._
- **Why does `ResearchConfig` connect `Community 6` to `Community 0`, `Community 2`, `Community 3`, `Community 8`, `Community 9`, `Community 11`, `Community 12`?**
  _High betweenness centrality (0.102) - this node is a cross-community bridge._
- **Are the 89 inferred relationships involving `ResearchConfig` (e.g. with `DemoCycleConfig` and `DemoProbeConfig`) actually correct?**
  _`ResearchConfig` has 89 INFERRED edges - model-reasoned connections that need verification._
- **Are the 88 inferred relationships involving `read_dataset()` (e.g. with `run_volume_trade_backtest()` and `run_volume_grid()`) actually correct?**
  _`read_dataset()` has 88 INFERRED edges - model-reasoned connections that need verification._
- **Are the 52 inferred relationships involving `write_dataset()` (e.g. with `generate_fixture_data()` and `_replace_dataset()`) actually correct?**
  _`write_dataset()` has 52 INFERRED edges - model-reasoned connections that need verification._
- **Are the 31 inferred relationships involving `main()` (e.g. with `load_config()` and `generate_fixture_data()`) actually correct?**
  _`main()` has 31 INFERRED edges - model-reasoned connections that need verification._