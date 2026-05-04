# Graph Report - MODEL050426  (2026-05-04)

## Corpus Check
- 38 files · ~64,271 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 634 nodes · 1741 edges · 13 communities detected
- Extraction: 73% EXTRACTED · 27% INFERRED · 0% AMBIGUOUS · INFERRED: 466 edges (avg confidence: 0.77)
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

## God Nodes (most connected - your core abstractions)
1. `main()` - 46 edges
2. `ResearchConfig` - 45 edges
3. `read_dataset()` - 45 edges
4. `write_dataset()` - 29 edges
5. `_FakeExecution` - 29 edges
6. `DailyCloseFadeConfig` - 28 edges
7. `run_volume_trade_backtest()` - 27 edges
8. `CostConfig` - 24 edges
9. `run_bybit_demo_sync()` - 24 edges
10. `download_market_data()` - 24 edges

## Surprising Connections (you probably didn't know these)
- `test_cli_fixture_pipeline_end_to_end()` --calls--> `main()`  [INFERRED]
  tests/test_aggression_carry_cli.py → scripts/run_volume_bucket_sweep.py
- `trades_to_frame()` --calls--> `test_trade_parser_handles_websocket_aliases_and_string_booleans()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py
- `run_volume_grid()` --calls--> `main()`  [INFERRED]
  aggression_carry/volume_backtest.py → scripts/run_volume_bucket_sweep.py
- `run_volume_alpha()` --calls--> `test_volume_alpha_isolated_daily_research_path()`  [INFERRED]
  aggression_carry/volume_alpha.py → tests/test_aggression_carry_volume_alpha.py
- `build_volume_features()` --calls--> `test_volume_alpha_isolated_daily_research_path()`  [INFERRED]
  aggression_carry/volume_alpha.py → tests/test_aggression_carry_volume_alpha.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.05
Nodes (87): VolumeBacktestConfig, VolumeGridConfig, generate_fixture_data(), _attribution_rows(), backtest_volume_trades(), _bar_at_close(), _bar_excursion(), _bar_exit_hits() (+79 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (67): download_archive_bytes(), download_public_trade_archive(), read_public_trade_archive(), TradeFlowConfig, _replace_dataset(), format_demo_sync_report(), _write_demo_sync_outputs(), _archive_filename() (+59 more)

### Community 2 - "Community 2"
Cohesion: 0.08
Nodes (60): _apply_basket_stop_to_rows(), apply_close_fade_basket_stop(), _attach_archive_membership(), _attach_instrument_age(), backtest_daily_close_fade(), _baseline_liquidity_filter_expr(), _baseline_liquidity_rank(), build_close_fade_diagnostic_observations() (+52 more)

### Community 3 - "Community 3"
Cohesion: 0.08
Nodes (40): _as_utc(), _compact_token(), _demo_cycle_lock(), _demo_sync_compat_context(), _demo_sync_supports_entry_pause(), DemoCycleConfig, _existing_active_state(), _failed_sleeve_result() (+32 more)

### Community 4 - "Community 4"
Cohesion: 0.09
Nodes (51): _as_utc(), build_demo_sync_orders(), _build_limit_order(), _build_probe_order(), cancel_stale_demo_orders(), _candidate_order_row(), _cap_candidate_order_rows(), _capped_order_qty() (+43 more)

### Community 5 - "Community 5"
Cohesion: 0.1
Nodes (47): _close_fade_position_weight(), _as_utc(), _basket_already_opened(), _bool_value(), build_forward_scan_features(), build_forward_universe(), _concat(), _count_reason() (+39 more)

### Community 6 - "Community 6"
Cohesion: 0.15
Nodes (35): CostConfig, DailyCloseFadeConfig, DailyCloseFadeGridConfig, DailyCloseFadeDiagnosticsConfig, run_daily_close_fade(), format_forward_run_message(), _merge_trade_frames(), run_forward_once() (+27 more)

### Community 7 - "Community 7"
Cohesion: 0.18
Nodes (31): ResearchConfig, DemoCancelAllConfig, DemoFlattenConfig, DemoProbeConfig, DemoSyncConfig, run_bybit_demo_sync(), _FakeExecution, _FakeExpensiveMarket (+23 more)

### Community 8 - "Community 8"
Cohesion: 0.15
Nodes (31): _add_forward_fade_args(), _add_forward_runtime_args(), _add_universe_backtest_args(), _apply_universe_backtest_args(), _backtest_config_from_args(), build_parser(), _close_fade_base_from_diagnostics_args(), _close_fade_base_from_grid_args() (+23 more)

### Community 9 - "Community 9"
Cohesion: 0.14
Nodes (26): ExchangeConfig, ForwardTestConfig, load_config(), _merge_daily_close_fade_config(), _merge_daily_close_fade_grid_config(), _merge_dataclass(), _merge_forward_test_config(), _merge_universe_config() (+18 more)

### Community 10 - "Community 10"
Cohesion: 0.13
Nodes (25): ArchiveKlineDownloadConfig, ArchiveManifestConfig, build_archive_trade_manifest(), _download_one_archive_kline(), _download_result(), _empty_download_results(), _empty_manifest(), fetch_directory_html() (+17 more)

### Community 11 - "Community 11"
Cohesion: 0.13
Nodes (4): BybitDataError, BybitMarketData, BybitPrivateClient, _demo_executor()

### Community 12 - "Community 12"
Cohesion: 0.15
Nodes (19): _ordinal_rank(), rank_correlation(), _add_cross_sectional_z(), _add_liquidity_rank(), attach_volume_forward_returns(), _best_base_portfolio(), build_volume_features(), compute_volume_metrics() (+11 more)

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit volume-alpha research package.  This package is the stripped-down rebuild`
  These have ≤1 connection - possible missing edges or undocumented components.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `main()` connect `Community 8` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 9`, `Community 10`, `Community 12`?**
  _High betweenness centrality (0.111) - this node is a cross-community bridge._
- **Why does `ResearchConfig` connect `Community 7` to `Community 9`, `Community 3`, `Community 6`, `Community 1`?**
  _High betweenness centrality (0.083) - this node is a cross-community bridge._
- **Why does `read_dataset()` connect `Community 6` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 5`, `Community 7`, `Community 10`, `Community 12`?**
  _High betweenness centrality (0.068) - this node is a cross-community bridge._
- **Are the 31 inferred relationships involving `main()` (e.g. with `load_config()` and `generate_fixture_data()`) actually correct?**
  _`main()` has 31 INFERRED edges - model-reasoned connections that need verification._
- **Are the 43 inferred relationships involving `ResearchConfig` (e.g. with `DemoCycleConfig` and `DemoProbeConfig`) actually correct?**
  _`ResearchConfig` has 43 INFERRED edges - model-reasoned connections that need verification._
- **Are the 43 inferred relationships involving `read_dataset()` (e.g. with `run_volume_trade_backtest()` and `run_volume_grid()`) actually correct?**
  _`read_dataset()` has 43 INFERRED edges - model-reasoned connections that need verification._
- **Are the 24 inferred relationships involving `write_dataset()` (e.g. with `generate_fixture_data()` and `_replace_dataset()`) actually correct?**
  _`write_dataset()` has 24 INFERRED edges - model-reasoned connections that need verification._