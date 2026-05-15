# Graph Report - MODEL050426  (2026-05-15)

## Corpus Check
- 45 files · ~87,058 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1018 nodes · 3077 edges · 17 communities detected
- Extraction: 72% EXTRACTED · 28% INFERRED · 0% AMBIGUOUS · INFERRED: 851 edges (avg confidence: 0.77)
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
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 18|Community 18]]

## God Nodes (most connected - your core abstractions)
1. `read_dataset()` - 99 edges
2. `ResearchConfig` - 89 edges
3. `write_dataset()` - 59 edges
4. `main()` - 49 edges
5. `DailyCloseFadeConfig` - 48 edges
6. `run_bybit_demo_sync()` - 44 edges
7. `_FakeExecution` - 44 edges
8. `DemoSyncConfig` - 41 edges
9. `BybitMarketData` - 37 edges
10. `_FakeMarket` - 37 edges

## Surprising Connections (you probably didn't know these)
- `test_active_daily_close_fade_default_is_stage4_selected_twap_schedule()` --calls--> `DailyCloseFadeGridConfig`  [INFERRED]
  tests/test_aggression_carry_config.py → aggression_carry/config.py
- `test_trade_parser_handles_websocket_aliases_and_string_booleans()` --calls--> `trades_to_frame()`  [INFERRED]
  tests/test_aggression_carry_ingestion.py → aggression_carry/ingestion.py
- `main()` --calls--> `DemoCycleConfig`  [INFERRED]
  scripts/run_hourly_demo_full_system_test.py → aggression_carry/demo_cycle.py
- `main()` --calls--> `run_bybit_demo_cycle()`  [INFERRED]
  scripts/run_hourly_demo_full_system_test.py → aggression_carry/demo_cycle.py
- `test_volume_alpha_isolated_daily_research_path()` --calls--> `run_volume_alpha()`  [INFERRED]
  tests/test_aggression_carry_volume_alpha.py → aggression_carry/volume_alpha.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (123): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveKlineDownloadConfig, ArchiveManifestConfig, build_archive_trade_manifest(), _delete_local_archive() (+115 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (133): CostConfig, DailyCloseFadeConfig, DailyCloseFadeGridConfig, _all_context_rate(), _apply_basket_stop_to_rows(), apply_close_fade_basket_stop(), _attach_archive_membership(), attach_close_fade_optional_context() (+125 more)

### Community 2 - "Community 2"
Cohesion: 0.05
Nodes (93): ForwardTestConfig, attach_close_fade_coin_market_context(), _bar_open(), close_fade_entry_child_schedule_ts_ms(), _short_limit_execution_price(), _short_stop_execution_price(), _vwap_reversion_exit_price(), _as_utc() (+85 more)

### Community 3 - "Community 3"
Cohesion: 0.05
Nodes (93): VolumeBacktestConfig, VolumeGridConfig, generate_fixture_data(), _attribution_rows(), backtest_volume_trades(), _bar_at_close(), _bar_excursion(), _bar_exit_hits() (+85 more)

### Community 4 - "Community 4"
Cohesion: 0.06
Nodes (79): _as_utc(), _bounded_exit_limit_price(), build_demo_sync_orders(), _build_limit_order(), _build_probe_order(), _cancel_open_entry_orders_for_symbols(), cancel_stale_demo_orders(), _candidate_order_row() (+71 more)

### Community 5 - "Community 5"
Cohesion: 0.13
Nodes (49): ResearchConfig, DemoCancelAllConfig, DemoFlattenConfig, DemoProbeConfig, DemoSyncConfig, run_bybit_demo_sync(), _CountingExecution, _FakeExecution (+41 more)

### Community 6 - "Community 6"
Cohesion: 0.09
Nodes (53): BybitPublicTradeStream, _active_trade_states(), _arm_profit_protection(), _as_utc(), _clear_state(), DemoFastProtectionConfig, _dt_from_ms(), _entry_trade_ids_with_exposure() (+45 more)

### Community 7 - "Community 7"
Cohesion: 0.09
Nodes (63): _aggregate_orders(), _aggregate_status(), _as_utc(), _avg_fill_price(), build_forward_demo_audit_rows(), build_forward_demo_audit_slice_rows(), build_forward_demo_daily_summary(), build_forward_demo_slice_daily_summary() (+55 more)

### Community 8 - "Community 8"
Cohesion: 0.1
Nodes (43): _as_utc(), _demo_cycle_lock(), DemoCycleConfig, _existing_active_state(), _failed_sleeve_result(), format_demo_cycle_report(), _inactive_sleeve_result(), _is_active_window() (+35 more)

### Community 9 - "Community 9"
Cohesion: 0.12
Nodes (39): _add_forward_fade_args(), _add_forward_runtime_args(), _add_universe_backtest_args(), _apply_universe_backtest_args(), _arg(), _backtest_config_from_args(), build_parser(), _close_fade_base_from_diagnostics_args() (+31 more)

### Community 10 - "Community 10"
Cohesion: 0.09
Nodes (6): BybitDataError, BybitMarketData, BybitPrivateClient, _is_rate_limit(), _leverage_text(), _demo_executor()

### Community 11 - "Community 11"
Cohesion: 0.11
Nodes (30): ExchangeConfig, load_config(), _merge_daily_close_fade_config(), _merge_daily_close_fade_grid_config(), _merge_dataclass(), _merge_forward_test_config(), _merge_universe_config(), _merge_volume_alpha_config() (+22 more)

### Community 12 - "Community 12"
Cohesion: 0.18
Nodes (19): _ordinal_rank(), rank_correlation(), _add_cross_sectional_z(), _add_liquidity_rank(), attach_volume_forward_returns(), _best_base_portfolio(), build_volume_features(), compute_volume_metrics() (+11 more)

### Community 13 - "Community 13"
Cohesion: 0.24
Nodes (18): audit_label(), AuditGate, close_fade_audit(), close_fade_lifecycle(), config_hash(), _context_datasets_present(), data_identity(), _finite() (+10 more)

### Community 15 - "Community 15"
Cohesion: 0.26
Nodes (9): ArchiveLiquidityUniverseConfig, _csv_symbols(), _format_report(), main(), parse_args(), _rank_manifest_rows_by_content_length(), select_archive_liquidity_universe(), _start_date_manifest() (+1 more)

### Community 16 - "Community 16"
Cohesion: 0.32
Nodes (12): build_volume_promotion_table(), _empty_promotion_table(), format_volume_promotion_report(), main(), _num(), _number(), parse_args(), _pct() (+4 more)

### Community 18 - "Community 18"
Cohesion: 1.0
Nodes (2): _candidate(), test_volume_promotion_requires_split_survival_and_drawdown()

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit volume-alpha research package.  This package is the stripped-down rebuild`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 18`** (3 nodes): `_candidate()`, `test_volume_promotion_script.py`, `test_volume_promotion_requires_split_survival_and_drawdown()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `read_dataset()` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 8`, `Community 12`, `Community 15`?**
  _High betweenness centrality (0.124) - this node is a cross-community bridge._
- **Why does `ResearchConfig` connect `Community 5` to `Community 0`, `Community 2`, `Community 4`, `Community 6`, `Community 8`, `Community 11`?**
  _High betweenness centrality (0.079) - this node is a cross-community bridge._
- **Why does `main()` connect `Community 9` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 7`, `Community 8`, `Community 11`, `Community 12`?**
  _High betweenness centrality (0.079) - this node is a cross-community bridge._
- **Are the 97 inferred relationships involving `read_dataset()` (e.g. with `run_volume_trade_backtest()` and `run_volume_grid()`) actually correct?**
  _`read_dataset()` has 97 INFERRED edges - model-reasoned connections that need verification._
- **Are the 87 inferred relationships involving `ResearchConfig` (e.g. with `DemoCycleConfig` and `DemoProbeConfig`) actually correct?**
  _`ResearchConfig` has 87 INFERRED edges - model-reasoned connections that need verification._
- **Are the 54 inferred relationships involving `write_dataset()` (e.g. with `generate_fixture_data()` and `_replace_dataset()`) actually correct?**
  _`write_dataset()` has 54 INFERRED edges - model-reasoned connections that need verification._
- **Are the 31 inferred relationships involving `main()` (e.g. with `load_config()` and `generate_fixture_data()`) actually correct?**
  _`main()` has 31 INFERRED edges - model-reasoned connections that need verification._