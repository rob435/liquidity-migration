# Graph Report - MODEL050426  (2026-05-18)

## Corpus Check
- 31 files · ~71,769 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 795 nodes · 2549 edges · 10 communities detected
- Extraction: 67% EXTRACTED · 33% INFERRED · 0% AMBIGUOUS · INFERRED: 848 edges (avg confidence: 0.77)
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
- [[_COMMUNITY_Community 12|Community 12]]

## God Nodes (most connected - your core abstractions)
1. `EventWebSocketRiskEngine` - 108 edges
2. `ResearchConfig` - 82 edges
3. `read_dataset()` - 73 edges
4. `run_event_demo_cycle()` - 72 edges
5. `EventWebSocketRiskConfig` - 63 edges
6. `FakePrivateClient` - 56 edges
7. `FakePrivateStream` - 55 edges
8. `FakePublicStream` - 53 edges
9. `EventDemoCycleConfig` - 51 edges
10. `FakeRiskClient` - 50 edges

## Surprising Connections (you probably didn't know these)
- `trades_to_frame()` --calls--> `test_trade_parser_handles_websocket_aliases_and_string_booleans()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py
- `densify_trade_klines_1h()` --calls--> `test_trade_klines_1h_aggregates_and_densifies_utc_day()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py
- `UniverseConfig` --calls--> `test_current_universe_table_filters_and_ranks()`  [INFERRED]
  aggression_carry/config.py → tests/test_aggression_carry_universe.py
- `ResearchConfig` --uses--> `FakeRiskClient`  [INFERRED]
  aggression_carry/config.py → tests/test_aggression_carry_event_demo.py
- `ResearchConfig` --uses--> `FakeKlineMarket`  [INFERRED]
  aggression_carry/config.py → tests/test_aggression_carry_event_demo.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (154): TradeLifecycleConfig, select_demo_entry_candidates(), FixtureSpec, generate_fixture_data(), _bar_excursion(), _bar_exit_hits(), build_equity_curve(), _daily_equity_values() (+146 more)

### Community 1 - "Community 1"
Cohesion: 0.04
Nodes (103): _active_position_by_symbol(), _base36(), _bool(), _build_demo_features(), _build_demo_universe(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot(), build_position_pnl_snapshot() (+95 more)

### Community 2 - "Community 2"
Cohesion: 0.05
Nodes (82): download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig, build_archive_trade_manifest(), _bybit_api_kline_url() (+74 more)

### Community 3 - "Community 3"
Cohesion: 0.14
Nodes (62): ResearchConfig, read_dataset(), EventWebSocketRiskConfig, EventWebSocketRiskEngine, test_archive_download_can_build_1m_klines_from_public_trades(), test_rest_kline_download_only_marks_successful_symbols(), test_rest_kline_download_writes_each_symbol_and_resumes(), test_demo_kline_cache_fetches_only_new_hour() (+54 more)

### Community 4 - "Community 4"
Cohesion: 0.04
Nodes (34): BybitDataError, BybitPrivateClient, BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitPublicTradeStream, BybitWebSocketTradeClient, _close_ws_client(), _is_rate_limit() (+26 more)

### Community 5 - "Community 5"
Cohesion: 0.08
Nodes (63): _demo_event_config(), _demo_strategy_id(), EventDemoCycleConfig, EventRiskCycleConfig, _execute_entries(), _normalize_demo_strategy_profile(), order_quantity_for_notional(), _prices_close() (+55 more)

### Community 6 - "Community 6"
Cohesion: 0.06
Nodes (62): download_archive_bytes(), _download_archive_to_path(), _public_trade_text_handle(), read_public_trade_archive(), read_public_trade_archive_klines_1h(), _read_public_trade_archive_klines_1h_vectorized(), TradeFlowConfig, _archive_filename() (+54 more)

### Community 7 - "Community 7"
Cohesion: 0.09
Nodes (32): build_parser(), _csv_float(), _csv_int(), _csv_str(), _event_demo_timing_text(), _event_risk_payload_material(), _event_risk_report_path(), main() (+24 more)

### Community 8 - "Community 8"
Cohesion: 0.12
Nodes (12): BybitMarketData, _dedupe_recent_klines(), _demo_kline_compact_cache_paths(), _demo_kline_compact_metadata(), _demo_kline_fetch_ranges(), _download_recent_1h_klines(), _empty_klines(), _fetch_recent_1h_klines() (+4 more)

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

- **Why does `EventWebSocketRiskEngine` connect `Community 3` to `Community 1`, `Community 2`, `Community 4`, `Community 5`?**
  _High betweenness centrality (0.082) - this node is a cross-community bridge._
- **Why does `read_dataset()` connect `Community 3` to `Community 0`, `Community 1`, `Community 2`, `Community 5`, `Community 8`?**
  _High betweenness centrality (0.073) - this node is a cross-community bridge._
- **Why does `run_event_demo_cycle()` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 8`?**
  _High betweenness centrality (0.073) - this node is a cross-community bridge._
- **Are the 59 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateClient` and `BybitPrivateWebSocketStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 59 INFERRED edges - model-reasoned connections that need verification._
- **Are the 80 inferred relationships involving `ResearchConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`ResearchConfig` has 80 INFERRED edges - model-reasoned connections that need verification._
- **Are the 71 inferred relationships involving `read_dataset()` (e.g. with `.bootstrap()` and `.rest_reconcile()`) actually correct?**
  _`read_dataset()` has 71 INFERRED edges - model-reasoned connections that need verification._
- **Are the 22 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 22 INFERRED edges - model-reasoned connections that need verification._