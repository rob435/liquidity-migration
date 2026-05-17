# Graph Report - MODEL050426  (2026-05-17)

## Corpus Check
- 31 files · ~68,329 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 769 nodes · 2468 edges · 10 communities detected
- Extraction: 67% EXTRACTED · 33% INFERRED · 0% AMBIGUOUS · INFERRED: 820 edges (avg confidence: 0.77)
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
2. `ResearchConfig` - 81 edges
3. `read_dataset()` - 73 edges
4. `run_event_demo_cycle()` - 69 edges
5. `EventWebSocketRiskConfig` - 63 edges
6. `FakePrivateClient` - 56 edges
7. `FakePrivateStream` - 55 edges
8. `FakePublicStream` - 53 edges
9. `FakeRiskClient` - 50 edges
10. `EventDemoCycleConfig` - 49 edges

## Surprising Connections (you probably didn't know these)
- `test_plan_stop_repairs_detects_missing_exchange_stop()` --calls--> `plan_stop_repairs()`  [INFERRED]
  tests/test_aggression_carry_event_demo.py → aggression_carry/event_demo.py
- `test_ledger_position_snapshot_marks_short_pnl_from_current_price()` --calls--> `build_ledger_position_pnl_snapshot()`  [INFERRED]
  tests/test_aggression_carry_event_demo.py → aggression_carry/event_demo.py
- `test_trade_parser_handles_websocket_aliases_and_string_booleans()` --calls--> `trades_to_frame()`  [INFERRED]
  tests/test_aggression_carry_ingestion.py → aggression_carry/ingestion.py
- `test_trade_klines_1h_aggregates_and_densifies_utc_day()` --calls--> `densify_trade_klines_1h()`  [INFERRED]
  tests/test_aggression_carry_ingestion.py → aggression_carry/ingestion.py
- `FakeRiskClient` --uses--> `ResearchConfig`  [INFERRED]
  tests/test_aggression_carry_event_demo.py → aggression_carry/config.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (134): CostConfig, TradeLifecycleConfig, _demo_event_config(), FixtureSpec, generate_fixture_data(), _bar_excursion(), _bar_exit_hits(), build_equity_curve() (+126 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (76): _active_position_by_symbol(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot(), build_position_pnl_snapshot(), _build_private_client(), _column_values(), _combine_errors(), _contract_lookup() (+68 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (92): _base36(), _bool(), _decimal_text(), EventDemoCycleConfig, EventRiskCycleConfig, _execute_entries(), _execute_exits(), _execute_risk_exits() (+84 more)

### Community 3 - "Community 3"
Cohesion: 0.06
Nodes (77): _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig, build_archive_trade_manifest(), _bybit_api_kline_url(), _date_from_ts_ms() (+69 more)

### Community 4 - "Community 4"
Cohesion: 0.14
Nodes (61): ResearchConfig, read_dataset(), EventWebSocketRiskConfig, EventWebSocketRiskEngine, test_archive_download_can_build_1m_klines_from_public_trades(), test_rest_kline_download_only_marks_successful_symbols(), test_rest_kline_download_writes_each_symbol_and_resumes(), BlockingPrivateStream (+53 more)

### Community 5 - "Community 5"
Cohesion: 0.04
Nodes (35): BybitDataError, BybitPrivateClient, BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitPublicTradeStream, BybitWebSocketTradeClient, _close_ws_client(), _is_rate_limit() (+27 more)

### Community 6 - "Community 6"
Cohesion: 0.07
Nodes (56): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _positive_int_env(), _public_trade_text_handle(), read_public_trade_archive(), read_public_trade_archive_klines_1h(), _read_public_trade_archive_klines_1h_vectorized() (+48 more)

### Community 7 - "Community 7"
Cohesion: 0.06
Nodes (47): build_parser(), _csv_float(), _csv_int(), _csv_str(), _event_risk_payload_material(), _event_risk_report_path(), main(), _print_event_risk_summary() (+39 more)

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

- **Why does `EventWebSocketRiskEngine` connect `Community 4` to `Community 1`, `Community 2`, `Community 5`?**
  _High betweenness centrality (0.086) - this node is a cross-community bridge._
- **Why does `read_dataset()` connect `Community 4` to `Community 0`, `Community 1`, `Community 2`, `Community 3`?**
  _High betweenness centrality (0.075) - this node is a cross-community bridge._
- **Why does `run_event_demo_cycle()` connect `Community 2` to `Community 0`, `Community 1`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 9`?**
  _High betweenness centrality (0.071) - this node is a cross-community bridge._
- **Are the 59 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateClient` and `BybitPrivateWebSocketStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 59 INFERRED edges - model-reasoned connections that need verification._
- **Are the 79 inferred relationships involving `ResearchConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`ResearchConfig` has 79 INFERRED edges - model-reasoned connections that need verification._
- **Are the 71 inferred relationships involving `read_dataset()` (e.g. with `.bootstrap()` and `.rest_reconcile()`) actually correct?**
  _`read_dataset()` has 71 INFERRED edges - model-reasoned connections that need verification._
- **Are the 21 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 21 INFERRED edges - model-reasoned connections that need verification._