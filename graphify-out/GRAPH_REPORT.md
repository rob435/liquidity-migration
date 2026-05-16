# Graph Report - MODEL050426  (2026-05-16)

## Corpus Check
- 28 files · ~37,645 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 458 nodes · 1114 edges · 10 communities detected
- Extraction: 72% EXTRACTED · 28% INFERRED · 0% AMBIGUOUS · INFERRED: 314 edges (avg confidence: 0.79)
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
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 11|Community 11]]

## God Nodes (most connected - your core abstractions)
1. `run_event_demo_cycle()` - 44 edges
2. `write_dataset()` - 29 edges
3. `run_volume_event_research()` - 28 edges
4. `main()` - 25 edges
5. `VolumeEventResearchConfig` - 25 edges
6. `BybitMarketData` - 24 edges
7. `read_dataset()` - 24 edges
8. `download_market_data()` - 22 edges
9. `_run_event_scenario()` - 21 edges
10. `BybitPrivateClient` - 17 edges

## Surprising Connections (you probably didn't know these)
- `trades_to_frame()` --calls--> `test_trade_parser_handles_websocket_aliases_and_string_booleans()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py
- `generate_fixture_data()` --calls--> `test_volume_event_research_writes_reports_on_fixture()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_volume_events.py
- `generate_fixture_data()` --calls--> `test_volume_event_research_requires_full_pit_by_default()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_volume_events.py
- `generate_fixture_data()` --calls--> `test_volume_features_build_daily_liquidity_ranks()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_volume_features.py
- `load_config()` --calls--> `test_active_system_config_loads_from_yaml()`  [INFERRED]
  aggression_carry/config.py → tests/test_aggression_carry_config.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.05
Nodes (92): TradeLifecycleConfig, build_equity_curve(), _exit_reason_rows(), _add_rank_fraction(), _apply_market_context_filters(), _attach_event_archive_membership(), _attach_market_context(), _bottom_cut_from_top_cut() (+84 more)

### Community 1 - "Community 1"
Cohesion: 0.07
Nodes (71): _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig, build_archive_trade_manifest(), _bybit_api_kline_url(), _date_from_ts_ms() (+63 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (74): _base36(), _build_demo_features(), _build_demo_universe(), build_ledger_position_pnl_snapshot(), build_position_pnl_snapshot(), _build_private_client(), _column_values(), _contract_lookup() (+66 more)

### Community 3 - "Community 3"
Cohesion: 0.08
Nodes (7): BybitDataError, BybitMarketData, BybitPrivateClient, BybitPublicTradeStream, _is_rate_limit(), _leverage_text(), RuntimeError

### Community 4 - "Community 4"
Cohesion: 0.09
Nodes (32): build_parser(), _csv_float(), _csv_int(), _csv_str(), main(), _universe_config_from_args(), CostConfig, ExchangeConfig (+24 more)

### Community 5 - "Community 5"
Cohesion: 0.09
Nodes (31): _bar_excursion(), _bar_exit_hits(), _date_boundary_ms(), _empty_baskets(), _filter_signal_window(), _filter_universe(), _funding_lookup(), _funding_mode_summary() (+23 more)

### Community 6 - "Community 6"
Cohesion: 0.15
Nodes (25): TradeFlowConfig, aggregate_signed_flow_1h(), aggregate_signed_flow_1m(), aggregate_trade_klines_1h(), aggregate_trade_klines_1m(), densify_trade_klines_1h(), densify_trade_klines_1m(), _first_present() (+17 more)

### Community 7 - "Community 7"
Cohesion: 0.14
Nodes (26): ResearchConfig, _archive_filename(), _archive_outputs_exist(), _dates_between(), download_market_data(), _download_rest_symbol_datasets(), _download_symbol_dataset(), _float_or_none() (+18 more)

### Community 9 - "Community 9"
Cohesion: 0.22
Nodes (13): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _download_and_read_hourly_archive(), _positive_int_env(), _public_trade_text_handle(), read_public_trade_archive(), read_public_trade_archive_klines_1h() (+5 more)

### Community 11 - "Community 11"
Cohesion: 1.0
Nodes (1): Bybit liquidity-migration research package.

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit liquidity-migration research package.`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 11`** (2 nodes): `__init__.py`, `Bybit liquidity-migration research package.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `run_event_demo_cycle()` connect `Community 2` to `Community 0`, `Community 1`, `Community 3`, `Community 4`, `Community 7`?**
  _High betweenness centrality (0.093) - this node is a cross-community bridge._
- **Why does `main()` connect `Community 4` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 6`, `Community 7`?**
  _High betweenness centrality (0.072) - this node is a cross-community bridge._
- **Why does `BybitMarketData` connect `Community 3` to `Community 2`, `Community 4`, `Community 7`?**
  _High betweenness centrality (0.069) - this node is a cross-community bridge._
- **Are the 12 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 12 INFERRED edges - model-reasoned connections that need verification._
- **Are the 24 inferred relationships involving `write_dataset()` (e.g. with `generate_fixture_data()` and `run_archive_manifest()`) actually correct?**
  _`write_dataset()` has 24 INFERRED edges - model-reasoned connections that need verification._
- **Are the 9 inferred relationships involving `run_volume_event_research()` (e.g. with `main()` and `CostConfig`) actually correct?**
  _`run_volume_event_research()` has 9 INFERRED edges - model-reasoned connections that need verification._
- **Are the 19 inferred relationships involving `main()` (e.g. with `load_config()` and `generate_fixture_data()`) actually correct?**
  _`main()` has 19 INFERRED edges - model-reasoned connections that need verification._