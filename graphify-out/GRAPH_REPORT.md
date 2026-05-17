# Graph Report - MODEL050426  (2026-05-17)

## Corpus Check
- 33 files · ~59,128 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 679 nodes · 1996 edges · 10 communities detected
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
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 13|Community 13]]

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
- `densify_trade_klines_1h()` --calls--> `test_trade_klines_1h_aggregates_and_densifies_utc_day()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py
- `generate_fixture_data()` --calls--> `test_volume_event_research_writes_reports_on_fixture()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_volume_events.py
- `generate_fixture_data()` --calls--> `test_volume_event_research_requires_full_pit_by_default()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_volume_events.py
- `generate_fixture_data()` --calls--> `test_volume_features_build_daily_liquidity_ranks()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_volume_features.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (133): TradeLifecycleConfig, _cooldown_until(), _empty_skip_counts(), _realized_stop_exit_ts(), select_demo_entry_candidates(), _trade_id(), _bar_excursion(), _bar_exit_hits() (+125 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (119): UniverseConfig, _normalize_instruments(), _active_position_by_symbol(), _base36(), _bool(), _build_demo_features(), _build_demo_universe(), build_event_risk_private_client() (+111 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (75): _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig, build_archive_trade_manifest(), _bybit_api_kline_url(), _date_from_ts_ms() (+67 more)

### Community 3 - "Community 3"
Cohesion: 0.06
Nodes (57): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _positive_int_env(), _public_trade_text_handle(), read_public_trade_archive(), read_public_trade_archive_klines_1h(), _read_public_trade_archive_klines_1h_vectorized() (+49 more)

### Community 4 - "Community 4"
Cohesion: 0.06
Nodes (13): BybitDataError, BybitMarketData, BybitPrivateClient, BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitPublicTradeStream, BybitWebSocketTradeClient, _close_ws_client() (+5 more)

### Community 5 - "Community 5"
Cohesion: 0.12
Nodes (19): _column_values(), _empty_trades(), _open_trades(), _risk_reconcile_missing_positions(), _safe_raw_positions(), _upsert_rows(), _write_order_rows(), _write_trade_rows() (+11 more)

### Community 6 - "Community 6"
Cohesion: 0.15
Nodes (38): ResearchConfig, read_dataset(), EventWebSocketRiskConfig, test_archive_download_can_build_1m_klines_from_public_trades(), test_rest_kline_download_only_marks_successful_symbols(), test_rest_kline_download_writes_each_symbol_and_resumes(), BlockingPrivateStream, BlockingPublicStream (+30 more)

### Community 7 - "Community 7"
Cohesion: 0.07
Nodes (35): build_parser(), _csv_float(), _csv_int(), _csv_str(), _event_risk_payload_material(), main(), _print_event_risk_summary(), _universe_config_from_args() (+27 more)

### Community 8 - "Community 8"
Cohesion: 0.16
Nodes (17): _age_filter_label(), build_current_universe_table(), _empty_universe_table(), format_universe_report(), run_discover_universe(), _safe_name(), _add_cross_sectional_z(), _add_liquidity_rank() (+9 more)

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

- **Why does `EventWebSocketRiskEngine` connect `Community 5` to `Community 1`, `Community 4`, `Community 6`?**
  _High betweenness centrality (0.075) - this node is a cross-community bridge._
- **Why does `EventDemoCycleConfig` connect `Community 1` to `Community 0`, `Community 4`, `Community 5`, `Community 6`, `Community 7`?**
  _High betweenness centrality (0.068) - this node is a cross-community bridge._
- **Why does `main()` connect `Community 7` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 5`, `Community 6`, `Community 8`?**
  _High betweenness centrality (0.067) - this node is a cross-community bridge._
- **Are the 37 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateClient` and `BybitPrivateWebSocketStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 37 INFERRED edges - model-reasoned connections that need verification._
- **Are the 12 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 12 INFERRED edges - model-reasoned connections that need verification._
- **Are the 43 inferred relationships involving `ResearchConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`ResearchConfig` has 43 INFERRED edges - model-reasoned connections that need verification._
- **Are the 42 inferred relationships involving `read_dataset()` (e.g. with `.bootstrap()` and `.rest_reconcile()`) actually correct?**
  _`read_dataset()` has 42 INFERRED edges - model-reasoned connections that need verification._