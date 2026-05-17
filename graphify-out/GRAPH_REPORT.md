# Graph Report - MODEL050426  (2026-05-17)

## Corpus Check
- 33 files · ~64,890 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 739 nodes · 2281 edges · 11 communities detected
- Extraction: 67% EXTRACTED · 33% INFERRED · 0% AMBIGUOUS · INFERRED: 762 edges (avg confidence: 0.77)
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
- [[_COMMUNITY_Community 14|Community 14]]

## God Nodes (most connected - your core abstractions)
1. `EventWebSocketRiskEngine` - 92 edges
2. `ResearchConfig` - 68 edges
3. `run_event_demo_cycle()` - 66 edges
4. `read_dataset()` - 61 edges
5. `EventWebSocketRiskConfig` - 52 edges
6. `EventDemoCycleConfig` - 45 edges
7. `FakePrivateClient` - 44 edges
8. `FakePrivateStream` - 44 edges
9. `write_dataset()` - 43 edges
10. `_float()` - 42 edges

## Surprising Connections (you probably didn't know these)
- `plan_risk_exits()` --calls--> `test_plan_risk_exits_uses_live_position_price_for_stops()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `plan_stop_repairs()` --calls--> `test_plan_stop_repairs_detects_missing_exchange_stop()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `build_ledger_position_pnl_snapshot()` --calls--> `test_ledger_position_snapshot_marks_short_pnl_from_current_price()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `_limit_chase_price()` --calls--> `test_limit_chase_price_crosses_spread_with_tick_rounding()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `format_telegram_status_message()` --calls--> `test_telegram_status_message_includes_positions_and_pnl()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (129): TradeLifecycleConfig, _cooldown_until(), _empty_skip_counts(), _realized_stop_exit_ts(), select_demo_entry_candidates(), _trade_id(), _bar_excursion(), _bar_exit_hits() (+121 more)

### Community 1 - "Community 1"
Cohesion: 0.06
Nodes (98): _normalize_instruments(), _active_position_by_symbol(), _base36(), _bool(), _build_demo_features(), _build_demo_universe(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot() (+90 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (81): download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig, build_archive_trade_manifest(), _bybit_api_kline_url() (+73 more)

### Community 3 - "Community 3"
Cohesion: 0.08
Nodes (27): BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitWebSocketTradeClient, _close_ws_client(), _column_values(), _open_trades(), _upsert_rows(), _write_order_rows() (+19 more)

### Community 4 - "Community 4"
Cohesion: 0.13
Nodes (49): ResearchConfig, read_dataset(), EventWebSocketRiskConfig, test_archive_download_can_build_1m_klines_from_public_trades(), test_rest_kline_download_only_marks_successful_symbols(), test_rest_kline_download_writes_each_symbol_and_resumes(), BlockingPrivateStream, BlockingPublicStream (+41 more)

### Community 5 - "Community 5"
Cohesion: 0.06
Nodes (17): BybitDataError, BybitMarketData, BybitPrivateClient, BybitPublicTradeStream, _is_rate_limit(), _leverage_text(), _patch_pybit_daemon_ping_timer(), _safe_open_orders() (+9 more)

### Community 6 - "Community 6"
Cohesion: 0.08
Nodes (52): EventDemoCycleConfig, EventRiskCycleConfig, _validate_demo_config(), EventScenario, FailingKlineMarket, FakeKlineMarket, FakeRiskClient, MinimalEventMarket (+44 more)

### Community 7 - "Community 7"
Cohesion: 0.07
Nodes (53): download_archive_bytes(), _download_archive_to_path(), _public_trade_text_handle(), read_public_trade_archive(), read_public_trade_archive_klines_1h(), _read_public_trade_archive_klines_1h_vectorized(), TradeFlowConfig, _archive_filename() (+45 more)

### Community 8 - "Community 8"
Cohesion: 0.08
Nodes (39): build_parser(), _csv_float(), _csv_int(), _csv_str(), _event_risk_payload_material(), _event_risk_report_path(), main(), _print_event_risk_summary() (+31 more)

### Community 10 - "Community 10"
Cohesion: 0.36
Nodes (7): _add_cross_sectional_z(), _add_liquidity_rank(), build_volume_features(), _daily_bars(), _rolling_mean(), _rolling_sum(), test_volume_features_build_daily_liquidity_ranks()

### Community 14 - "Community 14"
Cohesion: 1.0
Nodes (1): Bybit liquidity-migration research package.

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit liquidity-migration research package.`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 14`** (2 nodes): `__init__.py`, `Bybit liquidity-migration research package.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `EventWebSocketRiskEngine` connect `Community 3` to `Community 1`, `Community 4`, `Community 5`, `Community 6`?**
  _High betweenness centrality (0.079) - this node is a cross-community bridge._
- **Why does `run_event_demo_cycle()` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 8`?**
  _High betweenness centrality (0.074) - this node is a cross-community bridge._
- **Why does `EventDemoCycleConfig` connect `Community 6` to `Community 0`, `Community 1`, `Community 3`, `Community 4`, `Community 5`, `Community 8`?**
  _High betweenness centrality (0.069) - this node is a cross-community bridge._
- **Are the 48 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateClient` and `BybitPrivateWebSocketStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 48 INFERRED edges - model-reasoned connections that need verification._
- **Are the 66 inferred relationships involving `ResearchConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`ResearchConfig` has 66 INFERRED edges - model-reasoned connections that need verification._
- **Are the 19 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 19 INFERRED edges - model-reasoned connections that need verification._
- **Are the 59 inferred relationships involving `read_dataset()` (e.g. with `.bootstrap()` and `.rest_reconcile()`) actually correct?**
  _`read_dataset()` has 59 INFERRED edges - model-reasoned connections that need verification._