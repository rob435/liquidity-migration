# Graph Report - MODEL050426  (2026-05-18)

## Corpus Check
- 39 files · ~85,953 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 918 nodes · 2862 edges · 13 communities detected
- Extraction: 69% EXTRACTED · 31% INFERRED · 0% AMBIGUOUS · INFERRED: 884 edges (avg confidence: 0.77)
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
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 15|Community 15]]

## God Nodes (most connected - your core abstractions)
1. `EventWebSocketRiskEngine` - 108 edges
2. `ResearchConfig` - 82 edges
3. `read_dataset()` - 74 edges
4. `run_event_demo_cycle()` - 72 edges
5. `EventWebSocketRiskConfig` - 63 edges
6. `FakePrivateClient` - 56 edges
7. `FakePrivateStream` - 55 edges
8. `FakePublicStream` - 53 edges
9. `EventDemoCycleConfig` - 51 edges
10. `FakeRiskClient` - 50 edges

## Surprising Connections (you probably didn't know these)
- `build_parser()` --calls--> `test_cli_strategy_tribunal_parses_research_controls()`  [INFERRED]
  aggression_carry/cli.py → tests/test_aggression_carry_strategy_tribunal.py
- `plan_risk_exits()` --calls--> `test_plan_risk_exits_uses_live_position_price_for_stops()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `plan_stop_repairs()` --calls--> `test_plan_stop_repairs_detects_missing_exchange_stop()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `build_ledger_position_pnl_snapshot()` --calls--> `test_ledger_position_snapshot_marks_short_pnl_from_current_price()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `_terminalize_stale_pending_entry_orders()` --calls--> `test_stale_pending_entry_terminalizes_only_when_exchange_flat()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (166): TradeLifecycleConfig, FixtureSpec, generate_fixture_data(), _bar_excursion(), _bar_exit_hits(), build_equity_curve(), _daily_equity_values(), _date_boundary_ms() (+158 more)

### Community 1 - "Community 1"
Cohesion: 0.03
Nodes (132): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig (+124 more)

### Community 2 - "Community 2"
Cohesion: 0.05
Nodes (98): _normalize_instruments(), _active_position_by_symbol(), _bool(), _build_demo_features(), _build_demo_universe(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot(), build_position_pnl_snapshot() (+90 more)

### Community 3 - "Community 3"
Cohesion: 0.06
Nodes (84): _base36(), _decimal_text(), EventDemoCycleConfig, EventRiskCycleConfig, _execute_entries(), _execute_exits(), _execute_risk_exits(), _execution_summary() (+76 more)

### Community 4 - "Community 4"
Cohesion: 0.07
Nodes (29): BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitWebSocketTradeClient, _close_ws_client(), _is_rate_limit(), _column_values(), _open_trades(), _upsert_rows() (+21 more)

### Community 5 - "Community 5"
Cohesion: 0.12
Nodes (62): ResearchConfig, read_dataset(), EventWebSocketRiskConfig, test_archive_download_can_build_1m_klines_from_public_trades(), test_rest_kline_download_only_marks_successful_symbols(), test_rest_kline_download_writes_each_symbol_and_resumes(), test_demo_kline_cache_avoids_refetching_complete_window(), test_demo_kline_cache_fetches_only_new_hour() (+54 more)

### Community 6 - "Community 6"
Cohesion: 0.06
Nodes (54): build_parser(), _csv_float(), _csv_int(), _csv_str(), _event_demo_timing_text(), _event_risk_payload_material(), _event_risk_report_path(), main() (+46 more)

### Community 7 - "Community 7"
Cohesion: 0.1
Nodes (52): _artifact_check(), _basket_returns(), _best_summary_row(), _block_bootstrap(), _boolish(), _cluster_report(), _comparison_family_frame(), _comparison_family_label() (+44 more)

### Community 8 - "Community 8"
Cohesion: 0.06
Nodes (7): BybitDataError, BybitMarketData, BybitPrivateClient, BybitPublicTradeStream, _leverage_text(), _patch_pybit_daemon_ping_timer(), RuntimeError

### Community 9 - "Community 9"
Cohesion: 0.21
Nodes (19): _coverage_rows(), _crowded_edge(), _entry_date_expr(), _feature_edge(), _feature_verdict(), FeatureSpec, _filter_split(), _finite_float() (+11 more)

### Community 11 - "Community 11"
Cohesion: 0.33
Nodes (9): audit_crowding_model(), classify_liquidity_migration_crowding(), _crowding_reason_expr(), _entry_hour_expr(), format_crowding_model_report(), _pct(), summarize_crowding_classes(), _with_numeric_columns() (+1 more)

### Community 12 - "Community 12"
Cohesion: 0.36
Nodes (7): _add_cross_sectional_z(), _add_liquidity_rank(), build_volume_features(), _daily_bars(), _rolling_mean(), _rolling_sum(), test_volume_features_build_daily_liquidity_ranks()

### Community 15 - "Community 15"
Cohesion: 1.0
Nodes (1): Bybit liquidity-migration research package.

## Knowledge Gaps
- **3 isolated node(s):** `ExchangeConfig`, `Bybit liquidity-migration research package.`, `FeatureSpec`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 15`** (2 nodes): `__init__.py`, `Bybit liquidity-migration research package.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `main()` connect `Community 6` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 7`, `Community 8`, `Community 9`?**
  _High betweenness centrality (0.112) - this node is a cross-community bridge._
- **Why does `read_dataset()` connect `Community 5` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 7`, `Community 12`?**
  _High betweenness centrality (0.068) - this node is a cross-community bridge._
- **Why does `EventWebSocketRiskEngine` connect `Community 4` to `Community 8`, `Community 2`, `Community 3`, `Community 5`?**
  _High betweenness centrality (0.068) - this node is a cross-community bridge._
- **Are the 59 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateClient` and `BybitPrivateWebSocketStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 59 INFERRED edges - model-reasoned connections that need verification._
- **Are the 80 inferred relationships involving `ResearchConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`ResearchConfig` has 80 INFERRED edges - model-reasoned connections that need verification._
- **Are the 72 inferred relationships involving `read_dataset()` (e.g. with `.bootstrap()` and `.rest_reconcile()`) actually correct?**
  _`read_dataset()` has 72 INFERRED edges - model-reasoned connections that need verification._
- **Are the 22 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 22 INFERRED edges - model-reasoned connections that need verification._