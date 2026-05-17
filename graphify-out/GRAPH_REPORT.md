# Graph Report - MODEL050426  (2026-05-17)

## Corpus Check
- 31 files · ~55,401 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 646 nodes · 1872 edges · 10 communities detected
- Extraction: 68% EXTRACTED · 32% INFERRED · 0% AMBIGUOUS · INFERRED: 591 edges (avg confidence: 0.77)
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
1. `EventWebSocketRiskEngine` - 69 edges
2. `run_event_demo_cycle()` - 49 edges
3. `read_dataset()` - 41 edges
4. `_float()` - 39 edges
5. `ResearchConfig` - 38 edges
6. `write_dataset()` - 36 edges
7. `EventWebSocketRiskConfig` - 34 edges
8. `main()` - 32 edges
9. `VolumeEventResearchConfig` - 32 edges
10. `run_event_risk_cycle()` - 30 edges

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
Cohesion: 0.03
Nodes (129): TradeLifecycleConfig, _cooldown_until(), _empty_skip_counts(), _realized_stop_exit_ts(), select_demo_entry_candidates(), _trade_id(), _bar_excursion(), _bar_exit_hits() (+121 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (83): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig (+75 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (85): _active_position_by_symbol(), _base36(), _bool(), _build_demo_features(), _build_demo_universe(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot(), build_position_pnl_snapshot() (+77 more)

### Community 3 - "Community 3"
Cohesion: 0.06
Nodes (58): build_parser(), _csv_float(), _csv_int(), _csv_str(), _event_risk_payload_material(), main(), _print_event_risk_summary(), _universe_config_from_args() (+50 more)

### Community 4 - "Community 4"
Cohesion: 0.08
Nodes (21): BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitWebSocketTradeClient, _close_ws_client(), _column_values(), _open_trades(), _safe_raw_positions(), _upsert_rows() (+13 more)

### Community 5 - "Community 5"
Cohesion: 0.08
Nodes (47): TradeFlowConfig, _archive_filename(), _archive_outputs_exist(), _dates_between(), download_market_data(), _download_rest_symbol_datasets(), _download_symbol_dataset(), _float_or_none() (+39 more)

### Community 6 - "Community 6"
Cohesion: 0.08
Nodes (13): BybitDataError, BybitMarketData, BybitPrivateClient, BybitPublicTradeStream, _is_rate_limit(), _leverage_text(), RuntimeError, _contract() (+5 more)

### Community 7 - "Community 7"
Cohesion: 0.16
Nodes (31): ResearchConfig, read_dataset(), EventWebSocketRiskConfig, test_archive_download_can_build_1m_klines_from_public_trades(), test_rest_kline_download_only_marks_successful_symbols(), test_rest_kline_download_writes_each_symbol_and_resumes(), FakePrivateClient, FakePrivateStream (+23 more)

### Community 8 - "Community 8"
Cohesion: 0.18
Nodes (16): _age_filter_label(), build_current_universe_table(), _empty_universe_table(), format_universe_report(), run_discover_universe(), _safe_name(), _add_cross_sectional_z(), _add_liquidity_rank() (+8 more)

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

- **Why does `EventWebSocketRiskEngine` connect `Community 4` to `Community 2`, `Community 3`, `Community 6`, `Community 7`?**
  _High betweenness centrality (0.071) - this node is a cross-community bridge._
- **Why does `main()` connect `Community 3` to `Community 0`, `Community 1`, `Community 2`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 8`?**
  _High betweenness centrality (0.068) - this node is a cross-community bridge._
- **Why does `VolumeEventResearchConfig` connect `Community 0` to `Community 2`, `Community 3`?**
  _High betweenness centrality (0.064) - this node is a cross-community bridge._
- **Are the 30 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateClient` and `BybitPrivateWebSocketStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 30 INFERRED edges - model-reasoned connections that need verification._
- **Are the 12 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 12 INFERRED edges - model-reasoned connections that need verification._
- **Are the 39 inferred relationships involving `read_dataset()` (e.g. with `.bootstrap()` and `.rest_reconcile()`) actually correct?**
  _`read_dataset()` has 39 INFERRED edges - model-reasoned connections that need verification._
- **Are the 10 inferred relationships involving `_float()` (e.g. with `.on_position_message()` and `.on_order_message()`) actually correct?**
  _`_float()` has 10 INFERRED edges - model-reasoned connections that need verification._