# Graph Report - MODEL050426  (2026-05-17)

## Corpus Check
- 31 files · ~54,265 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 632 nodes · 1767 edges · 10 communities detected
- Extraction: 69% EXTRACTED · 31% INFERRED · 0% AMBIGUOUS · INFERRED: 545 edges (avg confidence: 0.77)
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
- [[_COMMUNITY_Community 11|Community 11]]

## God Nodes (most connected - your core abstractions)
1. `EventWebSocketRiskEngine` - 55 edges
2. `run_event_demo_cycle()` - 49 edges
3. `read_dataset()` - 37 edges
4. `_float()` - 35 edges
5. `write_dataset()` - 34 edges
6. `main()` - 32 edges
7. `VolumeEventResearchConfig` - 32 edges
8. `ResearchConfig` - 31 edges
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
Cohesion: 0.06
Nodes (88): _active_position_by_symbol(), _base36(), _bool(), _build_demo_features(), _build_demo_universe(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot(), build_position_pnl_snapshot() (+80 more)

### Community 1 - "Community 1"
Cohesion: 0.04
Nodes (102): TradeLifecycleConfig, _cooldown_until(), _empty_skip_counts(), _realized_stop_exit_ts(), select_demo_entry_candidates(), _trade_id(), build_equity_curve(), _exit_reason_rows() (+94 more)

### Community 2 - "Community 2"
Cohesion: 0.05
Nodes (97): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig (+89 more)

### Community 3 - "Community 3"
Cohesion: 0.07
Nodes (33): BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitWebSocketTradeClient, _close_ws_client(), ResearchConfig, _build_private_stream(), _build_ws_trade_client(), EventWebSocketRiskConfig (+25 more)

### Community 4 - "Community 4"
Cohesion: 0.07
Nodes (45): build_parser(), UniverseConfig, EventDemoCycleConfig, EventRiskCycleConfig, _reconcile_pending_order_fills(), target_initial_margin_pct_equity(), target_order_notional_pct_equity(), _validate_demo_config() (+37 more)

### Community 5 - "Community 5"
Cohesion: 0.08
Nodes (13): BybitDataError, BybitMarketData, BybitPrivateClient, BybitPublicTradeStream, _is_rate_limit(), _leverage_text(), RuntimeError, _contract() (+5 more)

### Community 6 - "Community 6"
Cohesion: 0.08
Nodes (40): _csv_float(), _csv_int(), _csv_str(), _event_risk_payload_material(), main(), _print_event_risk_summary(), _universe_config_from_args(), CostConfig (+32 more)

### Community 7 - "Community 7"
Cohesion: 0.09
Nodes (34): _bar_excursion(), _bar_exit_hits(), _daily_equity_values(), _date_boundary_ms(), _empty_baskets(), _filter_signal_window(), _filter_universe(), _funding_lookup() (+26 more)

### Community 8 - "Community 8"
Cohesion: 0.16
Nodes (23): TradeFlowConfig, aggregate_signed_flow_1h(), aggregate_signed_flow_1m(), aggregate_trade_klines_1m(), densify_trade_klines_1m(), _first_present(), FixtureSpec, generate_fixture_data() (+15 more)

### Community 11 - "Community 11"
Cohesion: 1.0
Nodes (1): Bybit liquidity-migration research package.

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit liquidity-migration research package.`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 11`** (2 nodes): `__init__.py`, `Bybit liquidity-migration research package.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `main()` connect `Community 6` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 8`?**
  _High betweenness centrality (0.071) - this node is a cross-community bridge._
- **Why does `VolumeEventResearchConfig` connect `Community 1` to `Community 0`, `Community 4`, `Community 6`?**
  _High betweenness centrality (0.065) - this node is a cross-community bridge._
- **Why does `run_event_demo_cycle()` connect `Community 0` to `Community 1`, `Community 2`, `Community 4`, `Community 5`, `Community 6`?**
  _High betweenness centrality (0.065) - this node is a cross-community bridge._
- **Are the 22 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateClient` and `BybitPrivateWebSocketStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 22 INFERRED edges - model-reasoned connections that need verification._
- **Are the 12 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 12 INFERRED edges - model-reasoned connections that need verification._
- **Are the 35 inferred relationships involving `read_dataset()` (e.g. with `.bootstrap()` and `.rest_reconcile()`) actually correct?**
  _`read_dataset()` has 35 INFERRED edges - model-reasoned connections that need verification._
- **Are the 6 inferred relationships involving `_float()` (e.g. with `.on_position_message()` and `.on_execution_message()`) actually correct?**
  _`_float()` has 6 INFERRED edges - model-reasoned connections that need verification._