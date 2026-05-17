# Graph Report - MODEL050426  (2026-05-17)

## Corpus Check
- 31 files · ~69,022 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 773 nodes · 2478 edges · 9 communities detected
- Extraction: 67% EXTRACTED · 33% INFERRED · 0% AMBIGUOUS · INFERRED: 825 edges (avg confidence: 0.77)
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
- [[_COMMUNITY_Community 11|Community 11]]

## God Nodes (most connected - your core abstractions)
1. `EventWebSocketRiskEngine` - 108 edges
2. `ResearchConfig` - 81 edges
3. `read_dataset()` - 73 edges
4. `run_event_demo_cycle()` - 70 edges
5. `EventWebSocketRiskConfig` - 63 edges
6. `FakePrivateClient` - 56 edges
7. `FakePrivateStream` - 55 edges
8. `FakePublicStream` - 53 edges
9. `EventDemoCycleConfig` - 50 edges
10. `FakeRiskClient` - 50 edges

## Surprising Connections (you probably didn't know these)
- `test_plan_risk_exits_uses_live_position_price_for_stops()` --calls--> `plan_risk_exits()`  [INFERRED]
  tests/test_aggression_carry_event_demo.py → aggression_carry/event_demo.py
- `test_plan_stop_repairs_detects_missing_exchange_stop()` --calls--> `plan_stop_repairs()`  [INFERRED]
  tests/test_aggression_carry_event_demo.py → aggression_carry/event_demo.py
- `test_ledger_position_snapshot_marks_short_pnl_from_current_price()` --calls--> `build_ledger_position_pnl_snapshot()`  [INFERRED]
  tests/test_aggression_carry_event_demo.py → aggression_carry/event_demo.py
- `test_stale_pending_entry_terminalizes_only_when_exchange_flat()` --calls--> `_terminalize_stale_pending_entry_orders()`  [INFERRED]
  tests/test_aggression_carry_event_demo.py → aggression_carry/event_demo.py
- `test_telegram_status_message_includes_positions_and_pnl()` --calls--> `format_telegram_status_message()`  [INFERRED]
  tests/test_aggression_carry_event_demo.py → aggression_carry/event_demo.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (146): TradeLifecycleConfig, _cooldown_until(), _demo_event_config(), _empty_skip_counts(), _realized_stop_exit_ts(), select_demo_entry_candidates(), _trade_id(), FixtureSpec (+138 more)

### Community 1 - "Community 1"
Cohesion: 0.03
Nodes (135): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig (+127 more)

### Community 2 - "Community 2"
Cohesion: 0.05
Nodes (100): _normalize_instruments(), _active_position_by_symbol(), _base36(), _bool(), _build_demo_features(), _build_demo_universe(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot() (+92 more)

### Community 3 - "Community 3"
Cohesion: 0.07
Nodes (68): EventDemoCycleConfig, EventRiskCycleConfig, _execute_entries(), order_quantity_for_notional(), _prices_close(), _reconcile_pending_order_fills(), _round_price(), _stop_price_for_entry() (+60 more)

### Community 4 - "Community 4"
Cohesion: 0.15
Nodes (58): ResearchConfig, read_dataset(), EventWebSocketRiskConfig, EventWebSocketRiskEngine, BlockingPrivateStream, BlockingPublicStream, FakePrivateClient, FakePrivateStream (+50 more)

### Community 5 - "Community 5"
Cohesion: 0.04
Nodes (16): BybitDataError, BybitMarketData, BybitPrivateClient, BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitPublicTradeStream, BybitWebSocketTradeClient, _close_ws_client() (+8 more)

### Community 6 - "Community 6"
Cohesion: 0.09
Nodes (19): _column_values(), _open_trades(), _upsert_rows(), _write_order_rows(), _write_trade_rows(), _ack_order_link(), _call_with_timeout(), _first_price() (+11 more)

### Community 7 - "Community 7"
Cohesion: 0.07
Nodes (41): build_parser(), _csv_float(), _csv_int(), _csv_str(), _event_risk_payload_material(), _event_risk_report_path(), main(), _print_event_risk_summary() (+33 more)

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

- **Why does `EventWebSocketRiskEngine` connect `Community 4` to `Community 2`, `Community 3`, `Community 5`, `Community 6`?**
  _High betweenness centrality (0.085) - this node is a cross-community bridge._
- **Why does `read_dataset()` connect `Community 4` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 6`?**
  _High betweenness centrality (0.075) - this node is a cross-community bridge._
- **Why does `run_event_demo_cycle()` connect `Community 2` to `Community 0`, `Community 1`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`?**
  _High betweenness centrality (0.072) - this node is a cross-community bridge._
- **Are the 59 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateClient` and `BybitPrivateWebSocketStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 59 INFERRED edges - model-reasoned connections that need verification._
- **Are the 79 inferred relationships involving `ResearchConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`ResearchConfig` has 79 INFERRED edges - model-reasoned connections that need verification._
- **Are the 71 inferred relationships involving `read_dataset()` (e.g. with `.bootstrap()` and `.rest_reconcile()`) actually correct?**
  _`read_dataset()` has 71 INFERRED edges - model-reasoned connections that need verification._
- **Are the 21 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 21 INFERRED edges - model-reasoned connections that need verification._