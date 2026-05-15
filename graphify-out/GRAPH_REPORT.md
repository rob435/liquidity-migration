# Graph Report - MODEL050426  (2026-05-15)

## Corpus Check
- 40 files · ~83,901 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1018 nodes · 3122 edges · 13 communities detected
- Extraction: 72% EXTRACTED · 28% INFERRED · 0% AMBIGUOUS · INFERRED: 878 edges (avg confidence: 0.77)
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
1. `read_dataset()` - 98 edges
2. `ResearchConfig` - 89 edges
3. `write_dataset()` - 58 edges
4. `main()` - 54 edges
5. `DailyCloseFadeConfig` - 48 edges
6. `run_bybit_demo_sync()` - 44 edges
7. `_FakeExecution` - 44 edges
8. `DemoSyncConfig` - 41 edges
9. `CostConfig` - 39 edges
10. `BybitMarketData` - 37 edges

## Surprising Connections (you probably didn't know these)
- `DailyCloseFadeGridConfig` --calls--> `test_active_daily_close_fade_default_is_stage4_selected_twap_schedule()`  [INFERRED]
  aggression_carry/config.py → tests/test_aggression_carry_config.py
- `trades_to_frame()` --calls--> `test_trade_parser_handles_websocket_aliases_and_string_booleans()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py
- `run_volume_alpha()` --calls--> `test_volume_alpha_isolated_daily_research_path()`  [INFERRED]
  aggression_carry/volume_alpha.py → tests/test_aggression_carry_volume_alpha.py
- `build_volume_features()` --calls--> `test_volume_alpha_isolated_daily_research_path()`  [INFERRED]
  aggression_carry/volume_alpha.py → tests/test_aggression_carry_volume_alpha.py
- `DailyCloseFadeConfig` --uses--> `_FakeBybit`  [INFERRED]
  aggression_carry/config.py → tests/test_aggression_carry_forward_test.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (142): VolumeBacktestConfig, VolumeGridConfig, generate_fixture_data(), _attribution_rows(), backtest_volume_trades(), _bar_at_close(), _bar_excursion(), _bar_exit_hits() (+134 more)

### Community 1 - "Community 1"
Cohesion: 0.04
Nodes (137): CostConfig, DailyCloseFadeConfig, DailyCloseFadeGridConfig, _all_context_rate(), _apply_basket_stop_to_rows(), apply_close_fade_basket_stop(), _attach_archive_membership(), attach_close_fade_optional_context() (+129 more)

### Community 2 - "Community 2"
Cohesion: 0.04
Nodes (106): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveKlineDownloadConfig, ArchiveManifestConfig, build_archive_trade_manifest(), _delete_local_archive() (+98 more)

### Community 3 - "Community 3"
Cohesion: 0.05
Nodes (93): ForwardTestConfig, attach_close_fade_coin_market_context(), _bar_open(), close_fade_entry_child_schedule_ts_ms(), _short_limit_execution_price(), _short_stop_execution_price(), _vwap_reversion_exit_price(), _as_utc() (+85 more)

### Community 4 - "Community 4"
Cohesion: 0.06
Nodes (81): _as_utc(), _bounded_exit_limit_price(), build_demo_sync_orders(), _build_limit_order(), _build_probe_order(), _cancel_open_entry_orders_for_symbols(), cancel_stale_demo_orders(), _candidate_order_row() (+73 more)

### Community 5 - "Community 5"
Cohesion: 0.13
Nodes (50): ResearchConfig, DemoCancelAllConfig, DemoFlattenConfig, DemoProbeConfig, DemoSyncConfig, run_bybit_demo_sync(), write_dataset(), _CountingExecution (+42 more)

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
Cohesion: 0.11
Nodes (41): _add_forward_fade_args(), _add_forward_runtime_args(), _add_universe_backtest_args(), _apply_universe_backtest_args(), _arg(), _backtest_config_from_args(), build_parser(), _close_fade_base_from_diagnostics_args() (+33 more)

### Community 10 - "Community 10"
Cohesion: 0.09
Nodes (37): ExchangeConfig, load_config(), _merge_daily_close_fade_config(), _merge_daily_close_fade_grid_config(), _merge_dataclass(), _merge_forward_test_config(), _merge_universe_config(), _merge_volume_alpha_config() (+29 more)

### Community 11 - "Community 11"
Cohesion: 0.1
Nodes (6): BybitDataError, BybitMarketData, BybitPrivateClient, _is_rate_limit(), _leverage_text(), _demo_executor()

### Community 12 - "Community 12"
Cohesion: 0.24
Nodes (18): audit_label(), AuditGate, close_fade_audit(), close_fade_lifecycle(), config_hash(), _context_datasets_present(), data_identity(), _finite() (+10 more)

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit demo-account trading system package.`
  These have ≤1 connection - possible missing edges or undocumented components.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `read_dataset()` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 8`, `Community 10`?**
  _High betweenness centrality (0.124) - this node is a cross-community bridge._
- **Why does `main()` connect `Community 9` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 7`, `Community 8`, `Community 10`?**
  _High betweenness centrality (0.094) - this node is a cross-community bridge._
- **Why does `ResearchConfig` connect `Community 5` to `Community 2`, `Community 3`, `Community 4`, `Community 6`, `Community 8`, `Community 10`?**
  _High betweenness centrality (0.088) - this node is a cross-community bridge._
- **Are the 96 inferred relationships involving `read_dataset()` (e.g. with `run_volume_trade_backtest()` and `run_volume_grid()`) actually correct?**
  _`read_dataset()` has 96 INFERRED edges - model-reasoned connections that need verification._
- **Are the 87 inferred relationships involving `ResearchConfig` (e.g. with `DemoCycleConfig` and `DemoProbeConfig`) actually correct?**
  _`ResearchConfig` has 87 INFERRED edges - model-reasoned connections that need verification._
- **Are the 53 inferred relationships involving `write_dataset()` (e.g. with `generate_fixture_data()` and `_replace_dataset()`) actually correct?**
  _`write_dataset()` has 53 INFERRED edges - model-reasoned connections that need verification._
- **Are the 34 inferred relationships involving `main()` (e.g. with `load_config()` and `generate_fixture_data()`) actually correct?**
  _`main()` has 34 INFERRED edges - model-reasoned connections that need verification._