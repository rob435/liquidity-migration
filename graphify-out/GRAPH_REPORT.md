# Graph Report - MODEL050426  (2026-05-17)

## Corpus Check
- 33 files · ~62,528 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 723 nodes · 2167 edges · 10 communities detected
- Extraction: 67% EXTRACTED · 33% INFERRED · 0% AMBIGUOUS · INFERRED: 710 edges (avg confidence: 0.77)
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
1. `EventWebSocketRiskEngine` - 82 edges
2. `run_event_demo_cycle()` - 61 edges
3. `ResearchConfig` - 59 edges
4. `read_dataset()` - 52 edges
5. `EventWebSocketRiskConfig` - 46 edges
6. `EventDemoCycleConfig` - 42 edges
7. `_float()` - 41 edges
8. `FakeRiskClient` - 39 edges
9. `write_dataset()` - 38 edges
10. `FakePrivateStream` - 38 edges

## Surprising Connections (you probably didn't know these)
- `_scenario_side()` --calls--> `test_selloff_exhaustion_side_hypotheses_are_directional()`  [INFERRED]
  aggression_carry/volume_events.py → tests/test_aggression_carry_volume_events.py
- `plan_risk_exits()` --calls--> `test_plan_risk_exits_uses_live_position_price_for_stops()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `plan_stop_repairs()` --calls--> `test_plan_stop_repairs_detects_missing_exchange_stop()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `build_ledger_position_pnl_snapshot()` --calls--> `test_ledger_position_snapshot_marks_short_pnl_from_current_price()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `trades_to_frame()` --calls--> `test_trade_parser_handles_websocket_aliases_and_string_booleans()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.04
Nodes (123): TradeLifecycleConfig, _bar_excursion(), _bar_exit_hits(), build_equity_curve(), _daily_equity_values(), _date_boundary_ms(), _empty_baskets(), _exit_reason_rows() (+115 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (97): _normalize_instruments(), _active_position_by_symbol(), _base36(), _bool(), _build_demo_features(), _build_demo_universe(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot() (+89 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (77): _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig, build_archive_trade_manifest(), _bybit_api_kline_url(), _date_from_ts_ms() (+69 more)

### Community 3 - "Community 3"
Cohesion: 0.08
Nodes (27): BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitWebSocketTradeClient, _close_ws_client(), _is_rate_limit(), _column_values(), _open_trades(), _upsert_rows() (+19 more)

### Community 4 - "Community 4"
Cohesion: 0.08
Nodes (56): EventDemoCycleConfig, EventRiskCycleConfig, _execute_entries(), _execute_exits(), order_quantity_for_notional(), _prices_close(), _reconcile_pending_order_fills(), _round_price() (+48 more)

### Community 5 - "Community 5"
Cohesion: 0.06
Nodes (57): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _positive_int_env(), _public_trade_text_handle(), read_public_trade_archive(), read_public_trade_archive_klines_1h(), _read_public_trade_archive_klines_1h_vectorized() (+49 more)

### Community 6 - "Community 6"
Cohesion: 0.14
Nodes (43): ResearchConfig, read_dataset(), EventWebSocketRiskConfig, test_archive_download_can_build_1m_klines_from_public_trades(), test_rest_kline_download_only_marks_successful_symbols(), test_rest_kline_download_writes_each_symbol_and_resumes(), BlockingPrivateStream, BlockingPublicStream (+35 more)

### Community 7 - "Community 7"
Cohesion: 0.06
Nodes (15): BybitDataError, BybitMarketData, BybitPrivateClient, BybitPublicTradeStream, _leverage_text(), _patch_pybit_daemon_ping_timer(), RuntimeError, _contract() (+7 more)

### Community 8 - "Community 8"
Cohesion: 0.06
Nodes (46): build_parser(), _csv_float(), _csv_int(), _csv_str(), _event_risk_payload_material(), _event_risk_report_path(), main(), _print_event_risk_summary() (+38 more)

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

- **Why does `EventWebSocketRiskEngine` connect `Community 3` to `Community 1`, `Community 4`, `Community 6`, `Community 7`?**
  _High betweenness centrality (0.073) - this node is a cross-community bridge._
- **Why does `run_event_demo_cycle()` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 8`?**
  _High betweenness centrality (0.071) - this node is a cross-community bridge._
- **Why does `EventDemoCycleConfig` connect `Community 4` to `Community 0`, `Community 1`, `Community 3`, `Community 6`, `Community 7`, `Community 8`?**
  _High betweenness centrality (0.070) - this node is a cross-community bridge._
- **Are the 42 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateClient` and `BybitPrivateWebSocketStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 42 INFERRED edges - model-reasoned connections that need verification._
- **Are the 16 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 16 INFERRED edges - model-reasoned connections that need verification._
- **Are the 57 inferred relationships involving `ResearchConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`ResearchConfig` has 57 INFERRED edges - model-reasoned connections that need verification._
- **Are the 50 inferred relationships involving `read_dataset()` (e.g. with `.bootstrap()` and `.rest_reconcile()`) actually correct?**
  _`read_dataset()` has 50 INFERRED edges - model-reasoned connections that need verification._