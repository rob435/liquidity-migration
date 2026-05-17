# Graph Report - MODEL050426  (2026-05-17)

## Corpus Check
- 40 files · ~83,218 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 890 nodes · 2746 edges · 13 communities detected
- Extraction: 69% EXTRACTED · 31% INFERRED · 0% AMBIGUOUS · INFERRED: 852 edges (avg confidence: 0.77)
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
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 16|Community 16]]

## God Nodes (most connected - your core abstractions)
1. `EventWebSocketRiskEngine` - 108 edges
2. `ResearchConfig` - 81 edges
3. `read_dataset()` - 77 edges
4. `run_event_demo_cycle()` - 69 edges
5. `EventWebSocketRiskConfig` - 63 edges
6. `FakePrivateClient` - 56 edges
7. `FakePrivateStream` - 55 edges
8. `FakePublicStream` - 53 edges
9. `FakeRiskClient` - 50 edges
10. `EventDemoCycleConfig` - 49 edges

## Surprising Connections (you probably didn't know these)
- `test_plan_risk_exits_uses_live_position_price_for_stops()` --calls--> `plan_risk_exits()`  [INFERRED]
  tests/test_aggression_carry_event_demo.py → aggression_carry/event_demo.py
- `test_plan_stop_repairs_detects_missing_exchange_stop()` --calls--> `plan_stop_repairs()`  [INFERRED]
  tests/test_aggression_carry_event_demo.py → aggression_carry/event_demo.py
- `build_ledger_position_pnl_snapshot()` --calls--> `test_ledger_position_snapshot_marks_short_pnl_from_current_price()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `format_telegram_status_message()` --calls--> `test_telegram_status_message_includes_positions_and_pnl()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `trades_to_frame()` --calls--> `test_trade_parser_handles_websocket_aliases_and_string_booleans()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.04
Nodes (109): _normalize_instruments(), _active_position_by_symbol(), _base36(), _bool(), _build_demo_features(), _build_demo_universe(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot() (+101 more)

### Community 1 - "Community 1"
Cohesion: 0.03
Nodes (138): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig (+130 more)

### Community 2 - "Community 2"
Cohesion: 0.03
Nodes (129): TradeLifecycleConfig, _bar_excursion(), _bar_exit_hits(), build_equity_curve(), _daily_equity_values(), _date_boundary_ms(), _empty_baskets(), _exit_reason_rows() (+121 more)

### Community 3 - "Community 3"
Cohesion: 0.15
Nodes (59): ResearchConfig, read_dataset(), EventWebSocketRiskConfig, EventWebSocketRiskEngine, test_demo_kline_cache_fetches_only_new_hour(), BlockingPrivateStream, BlockingPublicStream, FakePrivateClient (+51 more)

### Community 4 - "Community 4"
Cohesion: 0.07
Nodes (69): EventDemoCycleConfig, EventRiskCycleConfig, _execute_entries(), _fallback_tick_size(), _limit_chase_price(), order_quantity_for_notional(), _prices_close(), _reconcile_pending_order_fills() (+61 more)

### Community 5 - "Community 5"
Cohesion: 0.05
Nodes (30): BybitDataError, BybitPrivateClient, BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitPublicTradeStream, BybitWebSocketTradeClient, _close_ws_client(), _is_rate_limit() (+22 more)

### Community 6 - "Community 6"
Cohesion: 0.07
Nodes (49): CostConfig, _add_cross_sectional_z(), _add_liquidity_rank(), build_volume_features(), _daily_bars(), _rolling_mean(), _rolling_sum(), _annotated_events() (+41 more)

### Community 7 - "Community 7"
Cohesion: 0.08
Nodes (39): build_parser(), _csv_float(), _csv_int(), _csv_str(), _event_risk_payload_material(), _event_risk_report_path(), main(), _print_event_risk_summary() (+31 more)

### Community 8 - "Community 8"
Cohesion: 0.2
Nodes (23): _asset_basket_return(), _best_true_hedge(), _blend_grid(), _combined_squeeze_hedge_grid(), _daily_equity(), _date_ms(), _defensive_throttle_grid(), _event() (+15 more)

### Community 9 - "Community 9"
Cohesion: 0.16
Nodes (6): BybitMarketData, _dedupe_recent_klines(), _download_recent_1h_klines(), _empty_klines(), _fetch_recent_1h_klines(), _read_demo_kline_cache()

### Community 10 - "Community 10"
Cohesion: 0.22
Nodes (21): _add_trade_keys(), _annotated_events(), _candidate_predicates(), _cluster_summary(), _consecutive_stop_runs(), _drop_annotation_cols(), _filter_events(), _format_backtest_table() (+13 more)

### Community 12 - "Community 12"
Cohesion: 0.25
Nodes (18): _asset_return(), _asset_sets(), _daily_equity_values(), _duration_hedge_grid(), _event(), _ge(), _joined_baskets(), _load_price_maps() (+10 more)

### Community 16 - "Community 16"
Cohesion: 1.0
Nodes (1): Bybit liquidity-migration research package.

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit liquidity-migration research package.`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 16`** (2 nodes): `__init__.py`, `Bybit liquidity-migration research package.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `main()` connect `Community 7` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`?**
  _High betweenness centrality (0.097) - this node is a cross-community bridge._
- **Why does `read_dataset()` connect `Community 3` to `Community 0`, `Community 1`, `Community 2`, `Community 4`, `Community 6`, `Community 9`?**
  _High betweenness centrality (0.092) - this node is a cross-community bridge._
- **Why does `CostConfig` connect `Community 6` to `Community 2`, `Community 10`, `Community 4`, `Community 7`?**
  _High betweenness centrality (0.077) - this node is a cross-community bridge._
- **Are the 59 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateClient` and `BybitPrivateWebSocketStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 59 INFERRED edges - model-reasoned connections that need verification._
- **Are the 79 inferred relationships involving `ResearchConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`ResearchConfig` has 79 INFERRED edges - model-reasoned connections that need verification._
- **Are the 75 inferred relationships involving `read_dataset()` (e.g. with `.bootstrap()` and `.rest_reconcile()`) actually correct?**
  _`read_dataset()` has 75 INFERRED edges - model-reasoned connections that need verification._
- **Are the 21 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 21 INFERRED edges - model-reasoned connections that need verification._