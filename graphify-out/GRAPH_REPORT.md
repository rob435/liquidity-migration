# Graph Report - MODEL050426  (2026-05-17)

## Corpus Check
- 32 files · ~58,386 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 676 nodes · 1994 edges · 10 communities detected
- Extraction: 68% EXTRACTED · 32% INFERRED · 0% AMBIGUOUS · INFERRED: 642 edges (avg confidence: 0.77)
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
- [[_COMMUNITY_Community 12|Community 12]]

## God Nodes (most connected - your core abstractions)
1. `EventWebSocketRiskEngine` - 77 edges
2. `run_event_demo_cycle()` - 50 edges
3. `ResearchConfig` - 45 edges
4. `read_dataset()` - 44 edges
5. `EventWebSocketRiskConfig` - 41 edges
6. `_float()` - 40 edges
7. `write_dataset()` - 36 edges
8. `EventDemoCycleConfig` - 35 edges
9. `FakeRiskClient` - 33 edges
10. `FakePrivateStream` - 33 edges

## Surprising Connections (you probably didn't know these)
- `trades_to_frame()` --calls--> `test_trade_parser_handles_websocket_aliases_and_string_booleans()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py
- `generate_fixture_data()` --calls--> `test_volume_event_research_writes_reports_on_fixture()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_volume_events.py
- `generate_fixture_data()` --calls--> `test_volume_event_research_requires_full_pit_by_default()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_volume_events.py
- `generate_fixture_data()` --calls--> `test_volume_features_build_daily_liquidity_ranks()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_volume_features.py
- `UniverseConfig` --calls--> `test_current_universe_table_filters_and_ranks()`  [INFERRED]
  aggression_carry/config.py → tests/test_aggression_carry_universe.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (133): TradeLifecycleConfig, _cooldown_until(), _empty_skip_counts(), _realized_stop_exit_ts(), select_demo_entry_candidates(), _trade_id(), _bar_excursion(), _bar_exit_hits() (+125 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (121): UniverseConfig, _normalize_instruments(), _active_position_by_symbol(), _base36(), _bool(), _build_demo_features(), _build_demo_universe(), build_event_risk_private_client() (+113 more)

### Community 2 - "Community 2"
Cohesion: 0.05
Nodes (86): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig (+78 more)

### Community 3 - "Community 3"
Cohesion: 0.05
Nodes (22): BybitDataError, BybitMarketData, BybitPrivateClient, BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitPublicTradeStream, BybitWebSocketTradeClient, _close_ws_client() (+14 more)

### Community 4 - "Community 4"
Cohesion: 0.12
Nodes (17): _column_values(), _open_trades(), _safe_raw_positions(), _upsert_rows(), _write_order_rows(), _write_trade_rows(), _call_with_timeout(), EventWebSocketRiskEngine (+9 more)

### Community 5 - "Community 5"
Cohesion: 0.15
Nodes (38): ResearchConfig, read_dataset(), EventWebSocketRiskConfig, test_archive_download_can_build_1m_klines_from_public_trades(), test_rest_kline_download_only_marks_successful_symbols(), test_rest_kline_download_writes_each_symbol_and_resumes(), BlockingPrivateStream, BlockingPublicStream (+30 more)

### Community 6 - "Community 6"
Cohesion: 0.08
Nodes (46): TradeFlowConfig, _archive_filename(), _archive_outputs_exist(), _dates_between(), download_market_data(), _download_rest_symbol_datasets(), _download_symbol_dataset(), _float_or_none() (+38 more)

### Community 7 - "Community 7"
Cohesion: 0.08
Nodes (36): build_parser(), _csv_float(), _csv_int(), _csv_str(), _event_risk_payload_material(), main(), _print_event_risk_summary(), _universe_config_from_args() (+28 more)

### Community 9 - "Community 9"
Cohesion: 0.36
Nodes (7): _add_cross_sectional_z(), _add_liquidity_rank(), build_volume_features(), _daily_bars(), _rolling_mean(), _rolling_sum(), test_volume_features_build_daily_liquidity_ranks()

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

- **Why does `EventWebSocketRiskEngine` connect `Community 4` to `Community 1`, `Community 3`, `Community 5`?**
  _High betweenness centrality (0.075) - this node is a cross-community bridge._
- **Why does `EventDemoCycleConfig` connect `Community 1` to `Community 0`, `Community 3`, `Community 4`, `Community 5`, `Community 7`?**
  _High betweenness centrality (0.069) - this node is a cross-community bridge._
- **Why does `main()` connect `Community 7` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`?**
  _High betweenness centrality (0.068) - this node is a cross-community bridge._
- **Are the 37 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateClient` and `BybitPrivateWebSocketStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 37 INFERRED edges - model-reasoned connections that need verification._
- **Are the 12 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 12 INFERRED edges - model-reasoned connections that need verification._
- **Are the 43 inferred relationships involving `ResearchConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`ResearchConfig` has 43 INFERRED edges - model-reasoned connections that need verification._
- **Are the 42 inferred relationships involving `read_dataset()` (e.g. with `.bootstrap()` and `.rest_reconcile()`) actually correct?**
  _`read_dataset()` has 42 INFERRED edges - model-reasoned connections that need verification._