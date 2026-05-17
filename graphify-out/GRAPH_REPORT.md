# Graph Report - MODEL050426  (2026-05-17)

## Corpus Check
- 31 files · ~54,475 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 634 nodes · 1782 edges · 11 communities detected
- Extraction: 69% EXTRACTED · 31% INFERRED · 0% AMBIGUOUS · INFERRED: 552 edges (avg confidence: 0.77)
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
- [[_COMMUNITY_Community 12|Community 12]]

## God Nodes (most connected - your core abstractions)
1. `EventWebSocketRiskEngine` - 57 edges
2. `run_event_demo_cycle()` - 49 edges
3. `read_dataset()` - 38 edges
4. `_float()` - 36 edges
5. `write_dataset()` - 35 edges
6. `ResearchConfig` - 32 edges
7. `main()` - 32 edges
8. `VolumeEventResearchConfig` - 32 edges
9. `run_event_risk_cycle()` - 30 edges
10. `run_volume_event_research()` - 28 edges

## Surprising Connections (you probably didn't know these)
- `plan_risk_exits()` --calls--> `test_plan_risk_exits_uses_live_position_price_for_stops()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `plan_stop_repairs()` --calls--> `test_plan_stop_repairs_detects_missing_exchange_stop()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `build_ledger_position_pnl_snapshot()` --calls--> `test_ledger_position_snapshot_marks_short_pnl_from_current_price()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `_limit_chase_price()` --calls--> `test_limit_chase_price_crosses_spread_with_tick_rounding()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `trades_to_frame()` --calls--> `test_trade_parser_handles_websocket_aliases_and_string_booleans()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.04
Nodes (99): TradeLifecycleConfig, build_equity_curve(), _date_boundary_ms(), _exit_reason_rows(), _filter_signal_window(), _price_bars_by_symbol(), _add_rank_fraction(), _apply_market_context_filters() (+91 more)

### Community 1 - "Community 1"
Cohesion: 0.06
Nodes (92): _normalize_instruments(), _active_position_by_symbol(), _base36(), _bool(), _build_demo_features(), _build_demo_universe(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot() (+84 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (72): _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig, build_archive_trade_manifest(), _bybit_api_kline_url(), _date_from_ts_ms() (+64 more)

### Community 3 - "Community 3"
Cohesion: 0.08
Nodes (21): BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitWebSocketTradeClient, _close_ws_client(), _column_values(), _safe_raw_positions(), _upsert_rows(), _write_order_rows() (+13 more)

### Community 4 - "Community 4"
Cohesion: 0.06
Nodes (57): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _positive_int_env(), _public_trade_text_handle(), read_public_trade_archive(), read_public_trade_archive_klines_1h(), _read_public_trade_archive_klines_1h_vectorized() (+49 more)

### Community 5 - "Community 5"
Cohesion: 0.08
Nodes (43): build_parser(), UniverseConfig, EventDemoCycleConfig, EventRiskCycleConfig, _reconcile_pending_order_fills(), _validate_demo_config(), EventScenario, _iter_scenarios() (+35 more)

### Community 6 - "Community 6"
Cohesion: 0.08
Nodes (13): BybitDataError, BybitMarketData, BybitPrivateClient, BybitPublicTradeStream, _is_rate_limit(), _leverage_text(), RuntimeError, _contract() (+5 more)

### Community 7 - "Community 7"
Cohesion: 0.17
Nodes (24): ResearchConfig, read_dataset(), EventWebSocketRiskConfig, test_archive_download_can_build_1m_klines_from_public_trades(), test_rest_kline_download_only_marks_successful_symbols(), test_rest_kline_download_writes_each_symbol_and_resumes(), FakePrivateClient, FakePrivateStream (+16 more)

### Community 8 - "Community 8"
Cohesion: 0.1
Nodes (30): _bar_excursion(), _bar_exit_hits(), _daily_equity_values(), _empty_baskets(), _filter_universe(), _funding_lookup(), _funding_mode_summary(), _max_underwater_days() (+22 more)

### Community 9 - "Community 9"
Cohesion: 0.11
Nodes (25): _csv_float(), _csv_int(), _csv_str(), _event_risk_payload_material(), main(), _print_event_risk_summary(), _universe_config_from_args(), CostConfig (+17 more)

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

- **Why does `main()` connect `Community 9` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`?**
  _High betweenness centrality (0.070) - this node is a cross-community bridge._
- **Why does `VolumeEventResearchConfig` connect `Community 0` to `Community 9`, `Community 5`, `Community 1`?**
  _High betweenness centrality (0.065) - this node is a cross-community bridge._
- **Why does `run_event_demo_cycle()` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 9`?**
  _High betweenness centrality (0.065) - this node is a cross-community bridge._
- **Are the 23 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateClient` and `BybitPrivateWebSocketStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 23 INFERRED edges - model-reasoned connections that need verification._
- **Are the 12 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 12 INFERRED edges - model-reasoned connections that need verification._
- **Are the 36 inferred relationships involving `read_dataset()` (e.g. with `.bootstrap()` and `.rest_reconcile()`) actually correct?**
  _`read_dataset()` has 36 INFERRED edges - model-reasoned connections that need verification._
- **Are the 7 inferred relationships involving `_float()` (e.g. with `.on_position_message()` and `.on_order_message()`) actually correct?**
  _`_float()` has 7 INFERRED edges - model-reasoned connections that need verification._