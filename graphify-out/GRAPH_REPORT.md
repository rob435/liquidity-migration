# Graph Report - MODEL050426  (2026-05-18)

## Corpus Check
- 31 files · ~71,159 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 786 nodes · 2513 edges · 10 communities detected
- Extraction: 67% EXTRACTED · 33% INFERRED · 0% AMBIGUOUS · INFERRED: 835 edges (avg confidence: 0.77)
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
4. `run_event_demo_cycle()` - 71 edges
5. `EventWebSocketRiskConfig` - 63 edges
6. `FakePrivateClient` - 56 edges
7. `FakePrivateStream` - 55 edges
8. `FakePublicStream` - 53 edges
9. `EventDemoCycleConfig` - 50 edges
10. `FakeRiskClient` - 50 edges

## Surprising Connections (you probably didn't know these)
- `plan_risk_exits()` --calls--> `test_plan_risk_exits_uses_live_position_price_for_stops()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `plan_stop_repairs()` --calls--> `test_plan_stop_repairs_detects_missing_exchange_stop()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `build_ledger_position_pnl_snapshot()` --calls--> `test_ledger_position_snapshot_marks_short_pnl_from_current_price()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `_terminalize_stale_pending_entry_orders()` --calls--> `test_stale_pending_entry_terminalizes_only_when_exchange_flat()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `format_telegram_status_message()` --calls--> `test_telegram_status_message_includes_positions_and_pnl()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (144): CostConfig, TradeLifecycleConfig, _demo_event_config(), FixtureSpec, generate_fixture_data(), _bar_excursion(), _bar_exit_hits(), build_equity_curve() (+136 more)

### Community 1 - "Community 1"
Cohesion: 0.04
Nodes (96): _active_position_by_symbol(), _bool(), _build_demo_features(), _build_demo_universe(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot(), build_position_pnl_snapshot(), _build_private_client() (+88 more)

### Community 2 - "Community 2"
Cohesion: 0.04
Nodes (121): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig (+113 more)

### Community 3 - "Community 3"
Cohesion: 0.06
Nodes (82): _base36(), _decimal_text(), EventDemoCycleConfig, EventRiskCycleConfig, _execute_entries(), _execute_exits(), _execute_risk_exits(), _fallback_tick_size() (+74 more)

### Community 4 - "Community 4"
Cohesion: 0.15
Nodes (59): ResearchConfig, read_dataset(), EventWebSocketRiskConfig, EventWebSocketRiskEngine, test_demo_kline_cache_fetches_only_new_hour(), BlockingPrivateStream, BlockingPublicStream, FakePrivateClient (+51 more)

### Community 5 - "Community 5"
Cohesion: 0.06
Nodes (57): build_parser(), _csv_float(), _csv_int(), _csv_str(), _event_demo_timing_text(), _event_risk_payload_material(), _event_risk_report_path(), main() (+49 more)

### Community 6 - "Community 6"
Cohesion: 0.05
Nodes (22): BybitDataError, BybitPrivateClient, BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitPublicTradeStream, BybitWebSocketTradeClient, _close_ws_client(), _is_rate_limit() (+14 more)

### Community 7 - "Community 7"
Cohesion: 0.14
Nodes (8): BybitMarketData, _dedupe_recent_klines(), _demo_kline_fetch_ranges(), _download_recent_1h_klines(), _empty_klines(), _fetch_recent_1h_klines(), _read_demo_kline_cache(), test_demo_kline_fetch_ranges_uses_latest_bar_per_symbol()

### Community 9 - "Community 9"
Cohesion: 0.36
Nodes (7): _add_cross_sectional_z(), _add_liquidity_rank(), build_volume_features(), _daily_bars(), _rolling_mean(), _rolling_sum(), test_volume_features_build_daily_liquidity_ranks()

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

- **Why does `EventWebSocketRiskEngine` connect `Community 4` to `Community 1`, `Community 2`, `Community 3`, `Community 6`?**
  _High betweenness centrality (0.083) - this node is a cross-community bridge._
- **Why does `read_dataset()` connect `Community 4` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 7`, `Community 9`?**
  _High betweenness centrality (0.074) - this node is a cross-community bridge._
- **Why does `run_event_demo_cycle()` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`?**
  _High betweenness centrality (0.073) - this node is a cross-community bridge._
- **Are the 59 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateClient` and `BybitPrivateWebSocketStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 59 INFERRED edges - model-reasoned connections that need verification._
- **Are the 79 inferred relationships involving `ResearchConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`ResearchConfig` has 79 INFERRED edges - model-reasoned connections that need verification._
- **Are the 71 inferred relationships involving `read_dataset()` (e.g. with `.bootstrap()` and `.rest_reconcile()`) actually correct?**
  _`read_dataset()` has 71 INFERRED edges - model-reasoned connections that need verification._
- **Are the 22 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 22 INFERRED edges - model-reasoned connections that need verification._