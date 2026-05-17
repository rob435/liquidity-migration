# Graph Report - MODEL050426  (2026-05-17)

## Corpus Check
- 31 files · ~70,373 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 799 nodes · 2591 edges · 10 communities detected
- Extraction: 66% EXTRACTED · 34% INFERRED · 0% AMBIGUOUS · INFERRED: 873 edges (avg confidence: 0.76)
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
1. `EventWebSocketRiskEngine` - 108 edges
2. `ResearchConfig` - 89 edges
3. `read_dataset()` - 73 edges
4. `run_event_demo_cycle()` - 69 edges
5. `EventWebSocketRiskConfig` - 63 edges
6. `FakePrivateClient` - 56 edges
7. `FakePrivateStream` - 55 edges
8. `EventDemoCycleConfig` - 53 edges
9. `FakePublicStream` - 53 edges
10. `FakeRiskClient` - 51 edges

## Surprising Connections (you probably didn't know these)
- `test_ledger_position_snapshot_marks_short_pnl_from_current_price()` --calls--> `build_ledger_position_pnl_snapshot()`  [INFERRED]
  tests/test_aggression_carry_event_demo.py → aggression_carry/event_demo.py
- `test_stale_pending_entry_terminalizes_only_when_exchange_flat()` --calls--> `_terminalize_stale_pending_entry_orders()`  [INFERRED]
  tests/test_aggression_carry_event_demo.py → aggression_carry/event_demo.py
- `test_trade_parser_handles_websocket_aliases_and_string_booleans()` --calls--> `trades_to_frame()`  [INFERRED]
  tests/test_aggression_carry_ingestion.py → aggression_carry/ingestion.py
- `test_trade_klines_1h_aggregates_and_densifies_utc_day()` --calls--> `aggregate_trade_klines_1h()`  [INFERRED]
  tests/test_aggression_carry_ingestion.py → aggression_carry/ingestion.py
- `test_trade_klines_1h_aggregates_and_densifies_utc_day()` --calls--> `densify_trade_klines_1h()`  [INFERRED]
  tests/test_aggression_carry_ingestion.py → aggression_carry/ingestion.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (156): _base36(), build_event_risk_private_client(), build_position_pnl_snapshot(), _build_private_client(), _canary_limit_price(), _canary_mark_price(), _canary_order_is_open(), _combine_errors() (+148 more)

### Community 1 - "Community 1"
Cohesion: 0.03
Nodes (148): CostConfig, TradeLifecycleConfig, _build_demo_features(), _cooldown_until(), _empty_skip_counts(), _realized_stop_exit_ts(), select_demo_entry_candidates(), _trade_id() (+140 more)

### Community 2 - "Community 2"
Cohesion: 0.05
Nodes (91): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig (+83 more)

### Community 3 - "Community 3"
Cohesion: 0.14
Nodes (62): ResearchConfig, read_dataset(), EventWebSocketRiskConfig, EventWebSocketRiskEngine, test_archive_download_can_build_1m_klines_from_public_trades(), test_rest_kline_download_only_marks_successful_symbols(), test_rest_kline_download_writes_each_symbol_and_resumes(), test_demo_kline_cache_fetches_only_new_hour() (+54 more)

### Community 4 - "Community 4"
Cohesion: 0.04
Nodes (34): BybitDataError, BybitPrivateClient, BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitPublicTradeStream, BybitWebSocketTradeClient, _close_ws_client(), _is_rate_limit() (+26 more)

### Community 5 - "Community 5"
Cohesion: 0.1
Nodes (20): _active_position_by_symbol(), _bool(), build_ledger_position_pnl_snapshot(), _column_values(), _empty_trades(), _open_trades(), _price_lookup_from_positions(), _reconcile_open_trades() (+12 more)

### Community 6 - "Community 6"
Cohesion: 0.07
Nodes (41): build_parser(), _csv_float(), _csv_int(), _csv_str(), _event_risk_payload_material(), _event_risk_report_path(), main(), _print_event_risk_summary() (+33 more)

### Community 7 - "Community 7"
Cohesion: 0.08
Nodes (45): TradeFlowConfig, _archive_filename(), _archive_outputs_exist(), _dates_between(), download_market_data(), _download_rest_symbol_datasets(), _download_symbol_dataset(), _float_or_none() (+37 more)

### Community 9 - "Community 9"
Cohesion: 0.2
Nodes (1): BybitMarketData

### Community 12 - "Community 12"
Cohesion: 1.0
Nodes (1): Bybit liquidity-migration research package.

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit liquidity-migration research package.`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 9`** (18 nodes): `BybitMarketData`, `._get()`, `.get_funding_history()`, `.get_index_price_klines()`, `.get_instruments_info()`, `.get_klines()`, `.get_mark_price_klines()`, `.get_open_interest()`, `.get_orderbook()`, `.get_premium_index_klines()`, `._get_price_index_klines()`, `.get_recent_trades()`, `.get_tickers()`, `._paged_time_range()`, `.__post_init__()`, `._record_call()`, `.reset_stats()`, `.stats()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 12`** (2 nodes): `__init__.py`, `Bybit liquidity-migration research package.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `EventWebSocketRiskEngine` connect `Community 3` to `Community 0`, `Community 2`, `Community 4`, `Community 5`?**
  _High betweenness centrality (0.082) - this node is a cross-community bridge._
- **Why does `ResearchConfig` connect `Community 3` to `Community 0`, `Community 4`, `Community 6`, `Community 7`?**
  _High betweenness centrality (0.079) - this node is a cross-community bridge._
- **Why does `read_dataset()` connect `Community 3` to `Community 0`, `Community 1`, `Community 2`, `Community 5`?**
  _High betweenness centrality (0.070) - this node is a cross-community bridge._
- **Are the 59 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateClient` and `BybitPrivateWebSocketStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 59 INFERRED edges - model-reasoned connections that need verification._
- **Are the 87 inferred relationships involving `ResearchConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`ResearchConfig` has 87 INFERRED edges - model-reasoned connections that need verification._
- **Are the 71 inferred relationships involving `read_dataset()` (e.g. with `.bootstrap()` and `.rest_reconcile()`) actually correct?**
  _`read_dataset()` has 71 INFERRED edges - model-reasoned connections that need verification._
- **Are the 21 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 21 INFERRED edges - model-reasoned connections that need verification._