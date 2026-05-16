# Graph Report - MODEL050426  (2026-05-16)

## Corpus Check
- 30 files · ~41,251 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 514 nodes · 1289 edges · 12 communities detected
- Extraction: 72% EXTRACTED · 28% INFERRED · 0% AMBIGUOUS · INFERRED: 361 edges (avg confidence: 0.79)
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
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 13|Community 13]]

## God Nodes (most connected - your core abstractions)
1. `run_event_demo_cycle()` - 40 edges
2. `write_dataset()` - 31 edges
3. `read_dataset()` - 30 edges
4. `run_volume_trade_backtest()` - 28 edges
5. `run_volume_event_research()` - 26 edges
6. `BybitMarketData` - 24 edges
7. `main()` - 24 edges
8. `download_market_data()` - 22 edges
9. `VolumeEventResearchConfig` - 22 edges
10. `VolumeBacktestConfig` - 21 edges

## Surprising Connections (you probably didn't know these)
- `trades_to_frame()` --calls--> `test_trade_parser_handles_websocket_aliases_and_string_booleans()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py
- `densify_trade_klines_1h()` --calls--> `test_trade_klines_1h_aggregates_and_densifies_utc_day()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py
- `generate_fixture_data()` --calls--> `test_volume_event_research_writes_reports_on_fixture()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_volume_events.py
- `generate_fixture_data()` --calls--> `test_volume_event_research_requires_full_pit_by_default()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_volume_events.py
- `run_volume_alpha()` --calls--> `test_volume_alpha_isolated_daily_research_path()`  [INFERRED]
  aggression_carry/volume_alpha.py → tests/test_aggression_carry_volume_alpha.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.05
Nodes (93): CostConfig, VolumeBacktestConfig, VolumeGridConfig, generate_fixture_data(), _attribution_rows(), backtest_volume_trades(), _bar_at_close(), _bar_excursion() (+85 more)

### Community 1 - "Community 1"
Cohesion: 0.06
Nodes (84): _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig, build_archive_trade_manifest(), _bybit_api_kline_url(), _date_from_ts_ms() (+76 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (63): _exit_reason_rows(), _price_bars_by_symbol(), _add_rank_fraction(), _apply_market_context_filters(), _attach_event_archive_membership(), _attach_market_context(), _bottom_cut_from_top_cut(), _daily_return_frame() (+55 more)

### Community 3 - "Community 3"
Cohesion: 0.07
Nodes (61): _base36(), _build_demo_features(), _build_demo_universe(), _build_private_client(), _column_values(), _contract_lookup(), _cooldown_until(), _decimal_text() (+53 more)

### Community 4 - "Community 4"
Cohesion: 0.08
Nodes (7): BybitDataError, BybitMarketData, BybitPrivateClient, BybitPublicTradeStream, _is_rate_limit(), _leverage_text(), RuntimeError

### Community 5 - "Community 5"
Cohesion: 0.1
Nodes (35): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _positive_int_env(), _public_trade_text_handle(), read_public_trade_archive(), read_public_trade_archive_klines_1h(), _read_public_trade_archive_klines_1h_vectorized() (+27 more)

### Community 6 - "Community 6"
Cohesion: 0.13
Nodes (25): _archive_outputs_exist(), _dates_between(), _download_rest_symbol_datasets(), _download_symbol_dataset(), _float_or_none(), _mark_complete(), _marked_complete(), _marker_path() (+17 more)

### Community 7 - "Community 7"
Cohesion: 0.18
Nodes (19): _ordinal_rank(), rank_correlation(), _add_cross_sectional_z(), _add_liquidity_rank(), attach_volume_forward_returns(), _best_base_portfolio(), build_volume_features(), compute_volume_metrics() (+11 more)

### Community 8 - "Community 8"
Cohesion: 0.21
Nodes (14): ExchangeConfig, load_config(), _merge_dataclass(), _merge_universe_config(), _merge_volume_alpha_config(), _merge_volume_backtest_config(), _merge_volume_grid_config(), _tuple_bool() (+6 more)

### Community 10 - "Community 10"
Cohesion: 0.22
Nodes (13): build_parser(), _csv_float(), _csv_int(), _csv_str(), main(), parse_date_ms(), test_cli_archive_hourly_api_kline_default_resumes_written_partitions(), test_cli_archive_hourly_kline_default_resumes_written_partitions() (+5 more)

### Community 11 - "Community 11"
Cohesion: 0.27
Nodes (12): audit_label(), AuditGate, config_hash(), data_identity(), _finite(), gate(), _jsonable(), _parquet_identity_stats() (+4 more)

### Community 13 - "Community 13"
Cohesion: 1.0
Nodes (1): Bybit liquidity-migration research package.

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit liquidity-migration research package.`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 13`** (2 nodes): `__init__.py`, `Bybit liquidity-migration research package.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `run_event_demo_cycle()` connect `Community 3` to `Community 1`, `Community 2`, `Community 4`, `Community 6`, `Community 10`?**
  _High betweenness centrality (0.067) - this node is a cross-community bridge._
- **Why does `BybitMarketData` connect `Community 4` to `Community 1`, `Community 3`, `Community 6`?**
  _High betweenness centrality (0.061) - this node is a cross-community bridge._
- **Why does `main()` connect `Community 10` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 6`, `Community 8`?**
  _High betweenness centrality (0.054) - this node is a cross-community bridge._
- **Are the 12 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 12 INFERRED edges - model-reasoned connections that need verification._
- **Are the 26 inferred relationships involving `write_dataset()` (e.g. with `generate_fixture_data()` and `_replace_dataset()`) actually correct?**
  _`write_dataset()` has 26 INFERRED edges - model-reasoned connections that need verification._
- **Are the 28 inferred relationships involving `read_dataset()` (e.g. with `run_volume_trade_backtest()` and `run_volume_grid()`) actually correct?**
  _`read_dataset()` has 28 INFERRED edges - model-reasoned connections that need verification._
- **Are the 11 inferred relationships involving `run_volume_trade_backtest()` (e.g. with `VolumeBacktestConfig` and `CostConfig`) actually correct?**
  _`run_volume_trade_backtest()` has 11 INFERRED edges - model-reasoned connections that need verification._