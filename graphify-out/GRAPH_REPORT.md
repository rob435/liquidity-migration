# Graph Report - MODEL050426  (2026-05-17)

## Corpus Check
- 35 files · ~53,601 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 585 nodes · 1415 edges · 16 communities detected
- Extraction: 74% EXTRACTED · 26% INFERRED · 0% AMBIGUOUS · INFERRED: 366 edges (avg confidence: 0.79)
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
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 17|Community 17]]

## God Nodes (most connected - your core abstractions)
1. `run_event_demo_cycle()` - 44 edges
2. `VolumeEventResearchConfig` - 38 edges
3. `write_dataset()` - 29 edges
4. `read_dataset()` - 28 edges
5. `run_volume_event_research()` - 28 edges
6. `main()` - 25 edges
7. `BybitMarketData` - 24 edges
8. `download_market_data()` - 22 edges
9. `_run_event_scenario()` - 22 edges
10. `_event_filter()` - 21 edges

## Surprising Connections (you probably didn't know these)
- `build_parser()` --calls--> `test_event_demo_cli_defaults_to_frequent_demo_forward_cycle()`  [INFERRED]
  aggression_carry/cli.py → tests/test_aggression_carry_event_demo.py
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
Cohesion: 0.04
Nodes (100): TradeLifecycleConfig, build_equity_curve(), _exit_reason_rows(), _funding_lookup(), _price_bars_by_symbol(), _add_rank_fraction(), _add_reclaim_scores(), _apply_liquidity_migration_crowding_filter() (+92 more)

### Community 1 - "Community 1"
Cohesion: 0.06
Nodes (83): _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig, build_archive_trade_manifest(), _bybit_api_kline_url(), _date_from_ts_ms() (+75 more)

### Community 2 - "Community 2"
Cohesion: 0.05
Nodes (85): UniverseConfig, _base36(), _build_demo_features(), _build_demo_universe(), build_ledger_position_pnl_snapshot(), build_position_pnl_snapshot(), _build_private_client(), _column_values() (+77 more)

### Community 3 - "Community 3"
Cohesion: 0.08
Nodes (7): BybitDataError, BybitMarketData, BybitPrivateClient, BybitPublicTradeStream, _is_rate_limit(), _leverage_text(), RuntimeError

### Community 4 - "Community 4"
Cohesion: 0.1
Nodes (36): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _positive_int_env(), _public_trade_text_handle(), read_public_trade_archive(), read_public_trade_archive_klines_1h(), _read_public_trade_archive_klines_1h_vectorized() (+28 more)

### Community 5 - "Community 5"
Cohesion: 0.13
Nodes (25): _archive_outputs_exist(), _dates_between(), _download_rest_symbol_datasets(), _download_symbol_dataset(), _float_or_none(), _mark_complete(), _marked_complete(), _marker_path() (+17 more)

### Community 6 - "Community 6"
Cohesion: 0.14
Nodes (23): _bar_excursion(), _bar_exit_hits(), _daily_equity_values(), _date_boundary_ms(), _empty_baskets(), _filter_signal_window(), _filter_universe(), _funding_mode_summary() (+15 more)

### Community 7 - "Community 7"
Cohesion: 0.16
Nodes (21): _add_cross_sectional_z(), _add_liquidity_rank(), build_volume_features(), _daily_bars(), _rolling_mean(), _rolling_sum(), _annotated_events(), _bad_march_stop_count() (+13 more)

### Community 8 - "Community 8"
Cohesion: 0.2
Nodes (23): _asset_basket_return(), _best_true_hedge(), _blend_grid(), _combined_squeeze_hedge_grid(), _daily_equity(), _date_ms(), _defensive_throttle_grid(), _event() (+15 more)

### Community 9 - "Community 9"
Cohesion: 0.22
Nodes (21): _add_trade_keys(), _annotated_events(), _candidate_predicates(), _cluster_summary(), _consecutive_stop_runs(), _drop_annotation_cols(), _filter_events(), _format_backtest_table() (+13 more)

### Community 10 - "Community 10"
Cohesion: 0.15
Nodes (17): CostConfig, ExchangeConfig, load_config(), _merge_dataclass(), _merge_universe_config(), _tuple_str(), _crowding_veto(), _frontier_adaptive_config() (+9 more)

### Community 11 - "Community 11"
Cohesion: 0.25
Nodes (18): _asset_return(), _asset_sets(), _daily_equity_values(), _duration_hedge_grid(), _event(), _ge(), _joined_baskets(), _load_price_maps() (+10 more)

### Community 13 - "Community 13"
Cohesion: 0.23
Nodes (13): build_parser(), _csv_float(), _csv_int(), _csv_str(), main(), _universe_config_from_args(), parse_date_ms(), test_cli_archive_hourly_api_kline_default_resumes_written_partitions() (+5 more)

### Community 14 - "Community 14"
Cohesion: 0.36
Nodes (10): _format_table(), _frontier_base_config(), _load_context(), _load_frontier_modules(), main(), _pct(), _rank_rows(), _run_variant() (+2 more)

### Community 15 - "Community 15"
Cohesion: 0.4
Nodes (9): _equity_curve(), _format_table(), _frontier_union_config(), _load_context(), main(), _pct(), _trade_tape(), _variant_configs() (+1 more)

### Community 17 - "Community 17"
Cohesion: 1.0
Nodes (1): Bybit liquidity-migration research package.

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit liquidity-migration research package.`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 17`** (2 nodes): `__init__.py`, `Bybit liquidity-migration research package.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `main()` connect `Community 13` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 10`?**
  _High betweenness centrality (0.129) - this node is a cross-community bridge._
- **Why does `CostConfig` connect `Community 10` to `Community 0`, `Community 2`, `Community 7`, `Community 9`, `Community 14`, `Community 15`?**
  _High betweenness centrality (0.124) - this node is a cross-community bridge._
- **Why does `VolumeEventResearchConfig` connect `Community 0` to `Community 10`, `Community 2`, `Community 13`?**
  _High betweenness centrality (0.101) - this node is a cross-community bridge._
- **Are the 12 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 12 INFERRED edges - model-reasoned connections that need verification._
- **Are the 36 inferred relationships involving `VolumeEventResearchConfig` (e.g. with `CostConfig` and `TradeLifecycleConfig`) actually correct?**
  _`VolumeEventResearchConfig` has 36 INFERRED edges - model-reasoned connections that need verification._
- **Are the 24 inferred relationships involving `write_dataset()` (e.g. with `generate_fixture_data()` and `run_archive_manifest()`) actually correct?**
  _`write_dataset()` has 24 INFERRED edges - model-reasoned connections that need verification._
- **Are the 26 inferred relationships involving `read_dataset()` (e.g. with `run_archive_klines_download()` and `run_archive_hourly_klines_download()`) actually correct?**
  _`read_dataset()` has 26 INFERRED edges - model-reasoned connections that need verification._