# Graph Report - MODEL050426  (2026-05-17)

## Corpus Check
- 33 files · ~59,754 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 690 nodes · 2039 edges · 10 communities detected
- Extraction: 68% EXTRACTED · 32% INFERRED · 0% AMBIGUOUS · INFERRED: 661 edges (avg confidence: 0.77)
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
- [[_COMMUNITY_Community 13|Community 13]]

## God Nodes (most connected - your core abstractions)
1. `EventWebSocketRiskEngine` - 77 edges
2. `run_event_demo_cycle()` - 50 edges
3. `ResearchConfig` - 49 edges
4. `read_dataset()` - 47 edges
5. `EventWebSocketRiskConfig` - 41 edges
6. `_float()` - 40 edges
7. `write_dataset()` - 37 edges
8. `EventDemoCycleConfig` - 37 edges
9. `VolumeEventResearchConfig` - 34 edges
10. `FakeRiskClient` - 33 edges

## Surprising Connections (you probably didn't know these)
- `plan_risk_exits()` --calls--> `test_plan_risk_exits_uses_live_position_price_for_stops()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `plan_stop_repairs()` --calls--> `test_plan_stop_repairs_detects_missing_exchange_stop()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `build_ledger_position_pnl_snapshot()` --calls--> `test_ledger_position_snapshot_marks_short_pnl_from_current_price()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `trades_to_frame()` --calls--> `test_trade_parser_handles_websocket_aliases_and_string_booleans()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py
- `aggregate_trade_klines_1h()` --calls--> `test_trade_klines_1h_aggregates_and_densifies_utc_day()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (128): TradeLifecycleConfig, _cooldown_until(), _empty_skip_counts(), _realized_stop_exit_ts(), select_demo_entry_candidates(), _trade_id(), _bar_excursion(), _bar_exit_hits() (+120 more)

### Community 1 - "Community 1"
Cohesion: 0.06
Nodes (83): _active_position_by_symbol(), _base36(), _bool(), _build_demo_features(), _build_demo_universe(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot(), build_position_pnl_snapshot() (+75 more)

### Community 2 - "Community 2"
Cohesion: 0.05
Nodes (88): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig (+80 more)

### Community 3 - "Community 3"
Cohesion: 0.05
Nodes (70): _csv_float(), _csv_int(), _csv_str(), _event_risk_payload_material(), main(), _print_event_risk_summary(), _universe_config_from_args(), CostConfig (+62 more)

### Community 4 - "Community 4"
Cohesion: 0.06
Nodes (65): build_parser(), UniverseConfig, EventDemoCycleConfig, EventRiskCycleConfig, _execute_entries(), _fallback_tick_size(), _limit_chase_price(), order_quantity_for_notional() (+57 more)

### Community 5 - "Community 5"
Cohesion: 0.05
Nodes (26): BybitDataError, BybitPrivateClient, BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitPublicTradeStream, BybitWebSocketTradeClient, _close_ws_client(), _is_rate_limit() (+18 more)

### Community 6 - "Community 6"
Cohesion: 0.14
Nodes (41): ResearchConfig, read_dataset(), EventWebSocketRiskConfig, EventWebSocketRiskEngine, run_event_ws_risk(), test_archive_download_can_build_1m_klines_from_public_trades(), test_rest_kline_download_only_marks_successful_symbols(), test_rest_kline_download_writes_each_symbol_and_resumes() (+33 more)

### Community 7 - "Community 7"
Cohesion: 0.16
Nodes (6): BybitMarketData, _dedupe_recent_klines(), _download_recent_1h_klines(), _empty_klines(), _fetch_recent_1h_klines(), _read_demo_kline_cache()

### Community 9 - "Community 9"
Cohesion: 0.36
Nodes (7): _add_cross_sectional_z(), _add_liquidity_rank(), build_volume_features(), _daily_bars(), _rolling_mean(), _rolling_sum(), test_volume_features_build_daily_liquidity_ranks()

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

- **Why does `EventWebSocketRiskEngine` connect `Community 6` to `Community 1`, `Community 4`, `Community 5`?**
  _High betweenness centrality (0.073) - this node is a cross-community bridge._
- **Why does `EventDemoCycleConfig` connect `Community 4` to `Community 0`, `Community 1`, `Community 3`, `Community 5`, `Community 6`, `Community 7`?**
  _High betweenness centrality (0.069) - this node is a cross-community bridge._
- **Why does `main()` connect `Community 3` to `Community 0`, `Community 1`, `Community 2`, `Community 4`, `Community 5`, `Community 6`?**
  _High betweenness centrality (0.066) - this node is a cross-community bridge._
- **Are the 37 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateClient` and `BybitPrivateWebSocketStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 37 INFERRED edges - model-reasoned connections that need verification._
- **Are the 12 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 12 INFERRED edges - model-reasoned connections that need verification._
- **Are the 47 inferred relationships involving `ResearchConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`ResearchConfig` has 47 INFERRED edges - model-reasoned connections that need verification._
- **Are the 45 inferred relationships involving `read_dataset()` (e.g. with `.bootstrap()` and `.rest_reconcile()`) actually correct?**
  _`read_dataset()` has 45 INFERRED edges - model-reasoned connections that need verification._