# Graph Report - MODEL050426  (2026-05-17)

## Corpus Check
- 33 files · ~63,007 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 729 nodes · 2204 edges · 10 communities detected
- Extraction: 67% EXTRACTED · 33% INFERRED · 0% AMBIGUOUS · INFERRED: 726 edges (avg confidence: 0.77)
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
1. `EventWebSocketRiskEngine` - 87 edges
2. `ResearchConfig` - 62 edges
3. `run_event_demo_cycle()` - 61 edges
4. `read_dataset()` - 55 edges
5. `EventWebSocketRiskConfig` - 49 edges
6. `EventDemoCycleConfig` - 42 edges
7. `_float()` - 41 edges
8. `FakePrivateClient` - 41 edges
9. `FakePrivateStream` - 41 edges
10. `FakeRiskClient` - 39 edges

## Surprising Connections (you probably didn't know these)
- `plan_stop_repairs()` --calls--> `test_plan_stop_repairs_detects_missing_exchange_stop()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `build_ledger_position_pnl_snapshot()` --calls--> `test_ledger_position_snapshot_marks_short_pnl_from_current_price()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `trades_to_frame()` --calls--> `test_trade_parser_handles_websocket_aliases_and_string_booleans()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py
- `generate_fixture_data()` --calls--> `test_volume_event_research_writes_reports_on_fixture()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_volume_events.py
- `generate_fixture_data()` --calls--> `test_volume_event_research_requires_full_pit_by_default()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_volume_events.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (131): CostConfig, TradeLifecycleConfig, _bar_excursion(), _bar_exit_hits(), build_equity_curve(), _daily_equity_values(), _date_boundary_ms(), _empty_baskets() (+123 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (87): _active_position_by_symbol(), _base36(), _bool(), _build_demo_features(), _build_demo_universe(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot(), build_position_pnl_snapshot() (+79 more)

### Community 2 - "Community 2"
Cohesion: 0.05
Nodes (98): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig (+90 more)

### Community 3 - "Community 3"
Cohesion: 0.04
Nodes (43): BybitDataError, BybitPrivateClient, BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitPublicTradeStream, BybitWebSocketTradeClient, _close_ws_client(), _is_rate_limit() (+35 more)

### Community 4 - "Community 4"
Cohesion: 0.08
Nodes (56): EventDemoCycleConfig, EventRiskCycleConfig, _execute_entries(), _execute_exits(), _fallback_tick_size(), _limit_chase_price(), order_quantity_for_notional(), _prices_close() (+48 more)

### Community 5 - "Community 5"
Cohesion: 0.16
Nodes (44): ResearchConfig, read_dataset(), EventWebSocketRiskConfig, EventWebSocketRiskEngine, BlockingPrivateStream, BlockingPublicStream, FakePrivateClient, FakePrivateStream (+36 more)

### Community 6 - "Community 6"
Cohesion: 0.06
Nodes (54): build_parser(), _csv_float(), _csv_int(), _csv_str(), _event_risk_payload_material(), _event_risk_report_path(), main(), _print_event_risk_summary() (+46 more)

### Community 7 - "Community 7"
Cohesion: 0.15
Nodes (25): TradeFlowConfig, aggregate_signed_flow_1h(), aggregate_signed_flow_1m(), aggregate_trade_klines_1h(), aggregate_trade_klines_1m(), densify_trade_klines_1h(), densify_trade_klines_1m(), _first_present() (+17 more)

### Community 8 - "Community 8"
Cohesion: 0.17
Nodes (6): BybitMarketData, _dedupe_recent_klines(), _download_recent_1h_klines(), _empty_klines(), _fetch_recent_1h_klines(), _read_demo_kline_cache()

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

- **Why does `EventWebSocketRiskEngine` connect `Community 5` to `Community 1`, `Community 2`, `Community 3`, `Community 4`?**
  _High betweenness centrality (0.077) - this node is a cross-community bridge._
- **Why does `run_event_demo_cycle()` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 8`?**
  _High betweenness centrality (0.071) - this node is a cross-community bridge._
- **Why does `EventDemoCycleConfig` connect `Community 4` to `Community 0`, `Community 1`, `Community 3`, `Community 5`, `Community 6`, `Community 8`?**
  _High betweenness centrality (0.069) - this node is a cross-community bridge._
- **Are the 45 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateClient` and `BybitPrivateWebSocketStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 45 INFERRED edges - model-reasoned connections that need verification._
- **Are the 60 inferred relationships involving `ResearchConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`ResearchConfig` has 60 INFERRED edges - model-reasoned connections that need verification._
- **Are the 16 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 16 INFERRED edges - model-reasoned connections that need verification._
- **Are the 53 inferred relationships involving `read_dataset()` (e.g. with `.bootstrap()` and `.rest_reconcile()`) actually correct?**
  _`read_dataset()` has 53 INFERRED edges - model-reasoned connections that need verification._