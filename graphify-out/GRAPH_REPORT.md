# Graph Report - MODEL050426  (2026-05-16)

## Corpus Check
- 28 files · ~37,018 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 455 nodes · 1099 edges · 15 communities detected
- Extraction: 72% EXTRACTED · 28% INFERRED · 0% AMBIGUOUS · INFERRED: 305 edges (avg confidence: 0.79)
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
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 16|Community 16]]

## God Nodes (most connected - your core abstractions)
1. `read_dataset()` - 29 edges
2. `run_volume_trade_backtest()` - 28 edges
3. `write_dataset()` - 28 edges
4. `run_volume_event_research()` - 26 edges
5. `main()` - 22 edges
6. `download_market_data()` - 22 edges
7. `VolumeBacktestConfig` - 21 edges
8. `BybitMarketData` - 21 edges
9. `_run_event_scenario()` - 20 edges
10. `run_volume_grid()` - 19 edges

## Surprising Connections (you probably didn't know these)
- `test_trade_parser_handles_websocket_aliases_and_string_booleans()` --calls--> `trades_to_frame()`  [INFERRED]
  tests/test_aggression_carry_ingestion.py → aggression_carry/ingestion.py
- `test_volume_event_research_writes_reports_on_fixture()` --calls--> `generate_fixture_data()`  [INFERRED]
  tests/test_aggression_carry_volume_events.py → aggression_carry/ingestion.py
- `test_volume_event_research_requires_full_pit_by_default()` --calls--> `generate_fixture_data()`  [INFERRED]
  tests/test_aggression_carry_volume_events.py → aggression_carry/ingestion.py
- `test_volume_backtest_stop_takes_precedence_when_stop_and_tp_hit_same_bar()` --calls--> `_simulate_trade()`  [INFERRED]
  tests/test_aggression_carry_volume_alpha.py → aggression_carry/volume_backtest.py
- `test_volume_backtest_take_profit_is_symmetric_for_long_and_short()` --calls--> `_simulate_trade()`  [INFERRED]
  tests/test_aggression_carry_volume_alpha.py → aggression_carry/volume_backtest.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.06
Nodes (65): _empty_trades(), _exit_reason_rows(), _add_rank_fraction(), _apply_market_context_filters(), _attach_event_archive_membership(), _attach_market_context(), _bottom_cut_from_top_cut(), _daily_return_frame() (+57 more)

### Community 1 - "Community 1"
Cohesion: 0.06
Nodes (65): backtest_volume_trades(), _bar_at_close(), _bar_excursion(), _bar_exit_hits(), _btc_monthly_returns(), _btc_regime(), build_equity_curve(), build_equity_vs_btc() (+57 more)

### Community 2 - "Community 2"
Cohesion: 0.1
Nodes (44): ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig, format_archive_manifest_report(), _rows_by_date(), run_archive_klines_download(), run_archive_manifest() (+36 more)

### Community 3 - "Community 3"
Cohesion: 0.08
Nodes (7): BybitDataError, BybitMarketData, BybitPrivateClient, BybitPublicTradeStream, _is_rate_limit(), _leverage_text(), RuntimeError

### Community 4 - "Community 4"
Cohesion: 0.11
Nodes (38): _archive_kline_skip_rows(), build_archive_trade_manifest(), _bybit_api_kline_url(), _date_from_ts_ms(), _delete_local_archive(), _download_api_hourly_group(), _download_archive_hourly_group(), _download_one_archive_hourly_kline() (+30 more)

### Community 5 - "Community 5"
Cohesion: 0.16
Nodes (28): VolumeBacktestConfig, VolumeGridConfig, generate_fixture_data(), _attribution_rows(), _date_boundary_ms(), _date_range(), _filter_signal_window(), _grid_backend() (+20 more)

### Community 6 - "Community 6"
Cohesion: 0.13
Nodes (25): _archive_outputs_exist(), _dates_between(), _download_rest_symbol_datasets(), _download_symbol_dataset(), _float_or_none(), _mark_complete(), _marked_complete(), _marker_path() (+17 more)

### Community 7 - "Community 7"
Cohesion: 0.15
Nodes (24): TradeFlowConfig, aggregate_signed_flow_1h(), aggregate_signed_flow_1m(), aggregate_trade_klines_1h(), aggregate_trade_klines_1m(), densify_trade_klines_1h(), densify_trade_klines_1m(), _first_present() (+16 more)

### Community 8 - "Community 8"
Cohesion: 0.17
Nodes (20): CostConfig, _ordinal_rank(), rank_correlation(), _add_cross_sectional_z(), _add_liquidity_rank(), attach_volume_forward_returns(), _best_base_portfolio(), build_volume_features() (+12 more)

### Community 9 - "Community 9"
Cohesion: 0.21
Nodes (14): ExchangeConfig, load_config(), _merge_dataclass(), _merge_universe_config(), _merge_volume_alpha_config(), _merge_volume_backtest_config(), _merge_volume_grid_config(), _tuple_bool() (+6 more)

### Community 11 - "Community 11"
Cohesion: 0.24
Nodes (12): build_parser(), _csv_float(), _csv_int(), _csv_str(), main(), parse_date_ms(), test_cli_archive_hourly_api_kline_default_resumes_written_partitions(), test_cli_archive_hourly_kline_default_resumes_written_partitions() (+4 more)

### Community 12 - "Community 12"
Cohesion: 0.27
Nodes (12): audit_label(), AuditGate, config_hash(), data_identity(), _finite(), gate(), _jsonable(), _parquet_identity_stats() (+4 more)

### Community 13 - "Community 13"
Cohesion: 0.22
Nodes (13): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _download_and_read_hourly_archive(), _positive_int_env(), _public_trade_text_handle(), read_public_trade_archive(), read_public_trade_archive_klines_1h() (+5 more)

### Community 14 - "Community 14"
Cohesion: 1.0
Nodes (2): send_telegram_message(), TelegramConfig

### Community 16 - "Community 16"
Cohesion: 1.0
Nodes (1): Bybit liquidity-migration research package.

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit liquidity-migration research package.`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 14`** (3 nodes): `telegram.py`, `send_telegram_message()`, `TelegramConfig`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 16`** (2 nodes): `__init__.py`, `Bybit liquidity-migration research package.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `main()` connect `Community 11` to `Community 0`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 9`?**
  _High betweenness centrality (0.064) - this node is a cross-community bridge._
- **Why does `BybitMarketData` connect `Community 3` to `Community 2`, `Community 6`?**
  _High betweenness centrality (0.061) - this node is a cross-community bridge._
- **Why does `read_dataset()` connect `Community 2` to `Community 8`, `Community 0`, `Community 4`, `Community 5`?**
  _High betweenness centrality (0.054) - this node is a cross-community bridge._
- **Are the 27 inferred relationships involving `read_dataset()` (e.g. with `run_volume_trade_backtest()` and `run_volume_grid()`) actually correct?**
  _`read_dataset()` has 27 INFERRED edges - model-reasoned connections that need verification._
- **Are the 11 inferred relationships involving `run_volume_trade_backtest()` (e.g. with `VolumeBacktestConfig` and `CostConfig`) actually correct?**
  _`run_volume_trade_backtest()` has 11 INFERRED edges - model-reasoned connections that need verification._
- **Are the 23 inferred relationships involving `write_dataset()` (e.g. with `generate_fixture_data()` and `_replace_dataset()`) actually correct?**
  _`write_dataset()` has 23 INFERRED edges - model-reasoned connections that need verification._
- **Are the 9 inferred relationships involving `run_volume_event_research()` (e.g. with `main()` and `CostConfig`) actually correct?**
  _`run_volume_event_research()` has 9 INFERRED edges - model-reasoned connections that need verification._