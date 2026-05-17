# Graph Report - MODEL050426  (2026-05-17)

## Corpus Check
- 31 files · ~53,221 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 620 nodes · 1683 edges · 11 communities detected
- Extraction: 70% EXTRACTED · 30% INFERRED · 0% AMBIGUOUS · INFERRED: 513 edges (avg confidence: 0.77)
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
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 12|Community 12]]

## God Nodes (most connected - your core abstractions)
1. `run_event_demo_cycle()` - 49 edges
2. `EventWebSocketRiskEngine` - 44 edges
3. `write_dataset()` - 34 edges
4. `read_dataset()` - 33 edges
5. `main()` - 32 edges
6. `VolumeEventResearchConfig` - 32 edges
7. `_float()` - 32 edges
8. `run_event_risk_cycle()` - 30 edges
9. `run_volume_event_research()` - 28 edges
10. `BybitMarketData` - 27 edges

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
Nodes (123): TradeLifecycleConfig, _bar_excursion(), _bar_exit_hits(), build_equity_curve(), _daily_equity_values(), _date_boundary_ms(), _empty_baskets(), _exit_reason_rows() (+115 more)

### Community 1 - "Community 1"
Cohesion: 0.07
Nodes (76): _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig, build_archive_trade_manifest(), _bybit_api_kline_url(), _date_from_ts_ms() (+68 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (79): _base36(), _bool(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot(), build_position_pnl_snapshot(), _build_private_client(), _contract_lookup(), _cooldown_until() (+71 more)

### Community 3 - "Community 3"
Cohesion: 0.07
Nodes (28): BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitWebSocketTradeClient, _close_ws_client(), _active_position_by_symbol(), _column_values(), _empty_trades(), _open_trades() (+20 more)

### Community 4 - "Community 4"
Cohesion: 0.06
Nodes (58): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _positive_int_env(), _public_trade_text_handle(), read_public_trade_archive(), read_public_trade_archive_klines_1h(), _read_public_trade_archive_klines_1h_vectorized() (+50 more)

### Community 5 - "Community 5"
Cohesion: 0.08
Nodes (43): build_parser(), UniverseConfig, EventDemoCycleConfig, EventRiskCycleConfig, _reconcile_pending_order_fills(), _selected_scenario(), _validate_demo_config(), EventScenario (+35 more)

### Community 6 - "Community 6"
Cohesion: 0.07
Nodes (14): BybitDataError, BybitMarketData, BybitPrivateClient, BybitPublicTradeStream, _is_rate_limit(), _leverage_text(), _validate_risk_config(), RuntimeError (+6 more)

### Community 7 - "Community 7"
Cohesion: 0.11
Nodes (26): _csv_float(), _csv_int(), _csv_str(), _event_risk_payload_material(), main(), _print_event_risk_summary(), _universe_config_from_args(), CostConfig (+18 more)

### Community 8 - "Community 8"
Cohesion: 0.18
Nodes (14): ResearchConfig, EventWebSocketRiskConfig, FakePrivateClient, FakePrivateStream, FakePublicStream, FakeTradeClient, test_ws_risk_bootstrap_loads_pending_exit_order_after_restart(), test_ws_risk_rest_fallback_order_closes_from_execution_stream() (+6 more)

### Community 10 - "Community 10"
Cohesion: 0.31
Nodes (8): _build_demo_features(), _add_cross_sectional_z(), _add_liquidity_rank(), build_volume_features(), _daily_bars(), _rolling_mean(), _rolling_sum(), test_volume_features_build_daily_liquidity_ranks()

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

- **Why does `main()` connect `Community 7` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 8`?**
  _High betweenness centrality (0.073) - this node is a cross-community bridge._
- **Why does `VolumeEventResearchConfig` connect `Community 0` to `Community 2`, `Community 5`, `Community 7`?**
  _High betweenness centrality (0.067) - this node is a cross-community bridge._
- **Why does `run_event_demo_cycle()` connect `Community 2` to `Community 0`, `Community 1`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 10`?**
  _High betweenness centrality (0.065) - this node is a cross-community bridge._
- **Are the 12 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 12 INFERRED edges - model-reasoned connections that need verification._
- **Are the 17 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateClient` and `BybitPrivateWebSocketStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 17 INFERRED edges - model-reasoned connections that need verification._
- **Are the 29 inferred relationships involving `write_dataset()` (e.g. with `generate_fixture_data()` and `.write_report()`) actually correct?**
  _`write_dataset()` has 29 INFERRED edges - model-reasoned connections that need verification._
- **Are the 31 inferred relationships involving `read_dataset()` (e.g. with `.bootstrap()` and `.rest_reconcile()`) actually correct?**
  _`read_dataset()` has 31 INFERRED edges - model-reasoned connections that need verification._