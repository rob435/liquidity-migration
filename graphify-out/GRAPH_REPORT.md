# Graph Report - MODEL050426  (2026-05-17)

## Corpus Check
- 33 files · ~65,789 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 745 nodes · 2319 edges · 9 communities detected
- Extraction: 66% EXTRACTED · 34% INFERRED · 0% AMBIGUOUS · INFERRED: 778 edges (avg confidence: 0.77)
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
- [[_COMMUNITY_Community 12|Community 12]]

## God Nodes (most connected - your core abstractions)
1. `EventWebSocketRiskEngine` - 95 edges
2. `ResearchConfig` - 71 edges
3. `run_event_demo_cycle()` - 68 edges
4. `read_dataset()` - 64 edges
5. `EventWebSocketRiskConfig` - 54 edges
6. `EventDemoCycleConfig` - 46 edges
7. `FakePrivateClient` - 46 edges
8. `FakePrivateStream` - 46 edges
9. `write_dataset()` - 44 edges
10. `FakePublicStream` - 44 edges

## Surprising Connections (you probably didn't know these)
- `plan_risk_exits()` --calls--> `test_plan_risk_exits_uses_live_position_price_for_stops()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `plan_stop_repairs()` --calls--> `test_plan_stop_repairs_detects_missing_exchange_stop()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `build_ledger_position_pnl_snapshot()` --calls--> `test_ledger_position_snapshot_marks_short_pnl_from_current_price()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `test_stale_pending_entry_terminalizes_only_when_exchange_flat()` --calls--> `_terminalize_stale_pending_entry_orders()`  [INFERRED]
  tests/test_aggression_carry_event_demo.py → aggression_carry/event_demo.py
- `format_telegram_status_message()` --calls--> `test_telegram_status_message_includes_positions_and_pnl()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (130): TradeLifecycleConfig, _cooldown_until(), _empty_skip_counts(), _realized_stop_exit_ts(), select_demo_entry_candidates(), _trade_id(), _bar_excursion(), _bar_exit_hits() (+122 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (82): _normalize_instruments(), _active_position_by_symbol(), _bool(), _build_demo_features(), _build_demo_universe(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot(), build_position_pnl_snapshot() (+74 more)

### Community 2 - "Community 2"
Cohesion: 0.03
Nodes (39): BybitDataError, BybitMarketData, BybitPrivateClient, BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitPublicTradeStream, BybitWebSocketTradeClient, _close_ws_client() (+31 more)

### Community 3 - "Community 3"
Cohesion: 0.05
Nodes (90): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig (+82 more)

### Community 4 - "Community 4"
Cohesion: 0.06
Nodes (73): _base36(), _decimal_text(), EventDemoCycleConfig, EventRiskCycleConfig, _execute_entries(), _execute_exits(), _execute_risk_exits(), _execution_summary() (+65 more)

### Community 5 - "Community 5"
Cohesion: 0.14
Nodes (53): ResearchConfig, read_dataset(), EventWebSocketRiskConfig, EventWebSocketRiskEngine, test_archive_download_can_build_1m_klines_from_public_trades(), test_rest_kline_download_only_marks_successful_symbols(), test_rest_kline_download_writes_each_symbol_and_resumes(), test_demo_kline_cache_fetches_only_new_hour() (+45 more)

### Community 6 - "Community 6"
Cohesion: 0.06
Nodes (46): build_parser(), _csv_float(), _csv_int(), _csv_str(), _event_risk_payload_material(), _event_risk_report_path(), main(), _print_event_risk_summary() (+38 more)

### Community 7 - "Community 7"
Cohesion: 0.09
Nodes (44): TradeFlowConfig, _archive_filename(), _archive_outputs_exist(), _dates_between(), download_market_data(), _download_rest_symbol_datasets(), _download_symbol_dataset(), _float_or_none() (+36 more)

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

- **Why does `EventWebSocketRiskEngine` connect `Community 5` to `Community 1`, `Community 2`, `Community 4`?**
  _High betweenness centrality (0.080) - this node is a cross-community bridge._
- **Why does `run_event_demo_cycle()` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`?**
  _High betweenness centrality (0.075) - this node is a cross-community bridge._
- **Why does `read_dataset()` connect `Community 5` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 6`?**
  _High betweenness centrality (0.069) - this node is a cross-community bridge._
- **Are the 50 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateClient` and `BybitPrivateWebSocketStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 50 INFERRED edges - model-reasoned connections that need verification._
- **Are the 69 inferred relationships involving `ResearchConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`ResearchConfig` has 69 INFERRED edges - model-reasoned connections that need verification._
- **Are the 20 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 20 INFERRED edges - model-reasoned connections that need verification._
- **Are the 62 inferred relationships involving `read_dataset()` (e.g. with `.bootstrap()` and `.rest_reconcile()`) actually correct?**
  _`read_dataset()` has 62 INFERRED edges - model-reasoned connections that need verification._