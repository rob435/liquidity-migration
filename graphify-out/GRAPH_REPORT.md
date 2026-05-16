# Graph Report - MODEL050426  (2026-05-16)

## Corpus Check
- 30 files · ~41,129 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 512 nodes · 1283 edges · 11 communities detected
- Extraction: 72% EXTRACTED · 28% INFERRED · 0% AMBIGUOUS · INFERRED: 358 edges (avg confidence: 0.79)
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
- [[_COMMUNITY_Community 12|Community 12]]

## God Nodes (most connected - your core abstractions)
1. `run_event_demo_cycle()` - 39 edges
2. `write_dataset()` - 31 edges
3. `read_dataset()` - 30 edges
4. `run_volume_trade_backtest()` - 28 edges
5. `run_volume_event_research()` - 26 edges
6. `BybitMarketData` - 24 edges
7. `main()` - 24 edges
8. `download_market_data()` - 22 edges
9. `VolumeBacktestConfig` - 21 edges
10. `VolumeEventResearchConfig` - 21 edges

## Surprising Connections (you probably didn't know these)
- `test_event_demo_cli_defaults_to_frequent_demo_forward_cycle()` --calls--> `build_parser()`  [INFERRED]
  tests/test_aggression_carry_event_demo.py → aggression_carry/cli.py
- `trades_to_frame()` --calls--> `test_trade_parser_handles_websocket_aliases_and_string_booleans()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py
- `densify_trade_klines_1h()` --calls--> `test_trade_klines_1h_aggregates_and_densifies_utc_day()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py
- `generate_fixture_data()` --calls--> `test_volume_event_research_writes_reports_on_fixture()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_volume_events.py
- `generate_fixture_data()` --calls--> `test_volume_event_research_requires_full_pit_by_default()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_volume_events.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.05
Nodes (96): VolumeBacktestConfig, VolumeGridConfig, generate_fixture_data(), _attribution_rows(), backtest_volume_trades(), _bar_at_close(), _bar_excursion(), _bar_exit_hits() (+88 more)

### Community 1 - "Community 1"
Cohesion: 0.07
Nodes (73): _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig, build_archive_trade_manifest(), _bybit_api_kline_url(), _date_from_ts_ms() (+65 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (67): CostConfig, _add_rank_fraction(), _apply_market_context_filters(), _attach_event_archive_membership(), _attach_market_context(), _bottom_cut_from_top_cut(), _daily_return_frame(), _date_ms() (+59 more)

### Community 3 - "Community 3"
Cohesion: 0.06
Nodes (60): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _positive_int_env(), _public_trade_text_handle(), read_public_trade_archive(), read_public_trade_archive_klines_1h(), _read_public_trade_archive_klines_1h_vectorized() (+52 more)

### Community 4 - "Community 4"
Cohesion: 0.08
Nodes (55): _base36(), _build_demo_features(), _build_demo_universe(), _build_private_client(), _column_values(), _contract_lookup(), _cooldown_until(), _decimal_text() (+47 more)

### Community 5 - "Community 5"
Cohesion: 0.09
Nodes (6): BybitDataError, BybitMarketData, BybitPrivateClient, BybitPublicTradeStream, _is_rate_limit(), _leverage_text()

### Community 6 - "Community 6"
Cohesion: 0.13
Nodes (22): build_parser(), _csv_float(), _csv_int(), _csv_str(), main(), parse_date_ms(), _age_filter_label(), build_current_universe_table() (+14 more)

### Community 7 - "Community 7"
Cohesion: 0.18
Nodes (19): _ordinal_rank(), rank_correlation(), _add_cross_sectional_z(), _add_liquidity_rank(), attach_volume_forward_returns(), _best_base_portfolio(), build_volume_features(), compute_volume_metrics() (+11 more)

### Community 8 - "Community 8"
Cohesion: 0.21
Nodes (14): ExchangeConfig, load_config(), _merge_dataclass(), _merge_universe_config(), _merge_volume_alpha_config(), _merge_volume_backtest_config(), _merge_volume_grid_config(), _tuple_bool() (+6 more)

### Community 10 - "Community 10"
Cohesion: 0.27
Nodes (12): audit_label(), AuditGate, config_hash(), data_identity(), _finite(), gate(), _jsonable(), _parquet_identity_stats() (+4 more)

### Community 12 - "Community 12"
Cohesion: 1.0
Nodes (1): Bybit liquidity-migration research package.

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit liquidity-migration research package.`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 12`** (2 nodes): `__init__.py`, `Bybit liquidity-migration research package.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `run_event_demo_cycle()` connect `Community 4` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 5`, `Community 6`?**
  _High betweenness centrality (0.066) - this node is a cross-community bridge._
- **Why does `BybitMarketData` connect `Community 5` to `Community 0`, `Community 3`, `Community 4`, `Community 6`?**
  _High betweenness centrality (0.061) - this node is a cross-community bridge._
- **Why does `main()` connect `Community 6` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 8`?**
  _High betweenness centrality (0.053) - this node is a cross-community bridge._
- **Are the 12 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 12 INFERRED edges - model-reasoned connections that need verification._
- **Are the 26 inferred relationships involving `write_dataset()` (e.g. with `generate_fixture_data()` and `_replace_dataset()`) actually correct?**
  _`write_dataset()` has 26 INFERRED edges - model-reasoned connections that need verification._
- **Are the 28 inferred relationships involving `read_dataset()` (e.g. with `run_volume_trade_backtest()` and `run_volume_grid()`) actually correct?**
  _`read_dataset()` has 28 INFERRED edges - model-reasoned connections that need verification._
- **Are the 11 inferred relationships involving `run_volume_trade_backtest()` (e.g. with `VolumeBacktestConfig` and `CostConfig`) actually correct?**
  _`run_volume_trade_backtest()` has 11 INFERRED edges - model-reasoned connections that need verification._