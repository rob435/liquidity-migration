# Graph Report - MODEL050426  (2026-05-19)

## Corpus Check
- 45 files · ~96,061 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 998 nodes · 3033 edges · 15 communities detected
- Extraction: 70% EXTRACTED · 30% INFERRED · 0% AMBIGUOUS · INFERRED: 909 edges (avg confidence: 0.77)
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
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 18|Community 18]]

## God Nodes (most connected - your core abstractions)
1. `EventWebSocketRiskEngine` - 108 edges
2. `ResearchConfig` - 82 edges
3. `read_dataset()` - 75 edges
4. `run_event_demo_cycle()` - 72 edges
5. `EventWebSocketRiskConfig` - 63 edges
6. `FakePrivateClient` - 56 edges
7. `FakePrivateStream` - 55 edges
8. `FakePublicStream` - 53 edges
9. `EventDemoCycleConfig` - 51 edges
10. `_float()` - 50 edges

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
Nodes (170): TradeLifecycleConfig, FixtureSpec, generate_fixture_data(), _bar_excursion(), _bar_exit_hits(), build_equity_curve(), _daily_equity_values(), _date_boundary_ms() (+162 more)

### Community 1 - "Community 1"
Cohesion: 0.04
Nodes (109): _normalize_instruments(), _active_position_by_symbol(), _bool(), _build_demo_features(), _build_demo_universe(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot(), build_position_pnl_snapshot() (+101 more)

### Community 2 - "Community 2"
Cohesion: 0.05
Nodes (91): UniverseConfig, _base36(), _decimal_text(), _demo_event_config(), _demo_strategy_id(), EventDemoCycleConfig, EventRiskCycleConfig, _execute_entries() (+83 more)

### Community 3 - "Community 3"
Cohesion: 0.05
Nodes (82): download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig, build_archive_trade_manifest(), _bybit_api_kline_url() (+74 more)

### Community 4 - "Community 4"
Cohesion: 0.04
Nodes (22): BybitDataError, BybitMarketData, BybitPrivateClient, BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitPublicTradeStream, BybitWebSocketTradeClient, _close_ws_client() (+14 more)

### Community 5 - "Community 5"
Cohesion: 0.14
Nodes (62): ResearchConfig, read_dataset(), EventWebSocketRiskConfig, EventWebSocketRiskEngine, test_archive_download_can_build_1m_klines_from_public_trades(), test_rest_kline_download_only_marks_successful_symbols(), test_rest_kline_download_writes_each_symbol_and_resumes(), test_demo_kline_cache_fetches_only_new_hour() (+54 more)

### Community 6 - "Community 6"
Cohesion: 0.05
Nodes (66): download_archive_bytes(), _download_archive_to_path(), _public_trade_text_handle(), read_public_trade_archive(), read_public_trade_archive_klines_1h(), _read_public_trade_archive_klines_1h_vectorized(), CostConfig, ExchangeConfig (+58 more)

### Community 7 - "Community 7"
Cohesion: 0.1
Nodes (52): _artifact_check(), _basket_returns(), _best_summary_row(), _block_bootstrap(), _boolish(), _cluster_report(), _comparison_family_frame(), _comparison_family_label() (+44 more)

### Community 8 - "Community 8"
Cohesion: 0.07
Nodes (46): _audit_violations(), ChallengerSpec, champion_challenger_specs(), format_champion_challenger_manifest(), run_champion_challenger_audit(), build_parser(), _csv_float(), _csv_int() (+38 more)

### Community 9 - "Community 9"
Cohesion: 0.17
Nodes (23): _covered_pairs(), DataLayerAuditConfig, _dataset_notes(), _dataset_row(), DatasetCoverageSnapshot, _date_range(), _date_span(), _date_start_ms() (+15 more)

### Community 10 - "Community 10"
Cohesion: 0.16
Nodes (17): _age_filter_label(), build_current_universe_table(), _empty_universe_table(), format_universe_report(), run_discover_universe(), _safe_name(), _add_cross_sectional_z(), _add_liquidity_rank() (+9 more)

### Community 11 - "Community 11"
Cohesion: 0.21
Nodes (19): _coverage_rows(), _crowded_edge(), _entry_date_expr(), _feature_edge(), _feature_verdict(), FeatureSpec, _filter_split(), _finite_float() (+11 more)

### Community 13 - "Community 13"
Cohesion: 0.25
Nodes (5): BinanceDataError, BinanceUSDMData, _ceil_to_period(), _floor_to_period(), _recent_history_start()

### Community 15 - "Community 15"
Cohesion: 0.33
Nodes (9): audit_crowding_model(), classify_liquidity_migration_crowding(), _crowding_reason_expr(), _entry_hour_expr(), format_crowding_model_report(), _pct(), summarize_crowding_classes(), _with_numeric_columns() (+1 more)

### Community 18 - "Community 18"
Cohesion: 1.0
Nodes (1): Bybit liquidity-migration research package.

## Knowledge Gaps
- **3 isolated node(s):** `ExchangeConfig`, `Bybit liquidity-migration research package.`, `FeatureSpec`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 18`** (2 nodes): `__init__.py`, `Bybit liquidity-migration research package.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `main()` connect `Community 8` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 9`, `Community 10`, `Community 11`?**
  _High betweenness centrality (0.128) - this node is a cross-community bridge._
- **Why does `read_dataset()` connect `Community 5` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 7`, `Community 9`, `Community 10`?**
  _High betweenness centrality (0.065) - this node is a cross-community bridge._
- **Why does `VolumeEventResearchConfig` connect `Community 0` to `Community 8`, `Community 1`, `Community 2`, `Community 6`?**
  _High betweenness centrality (0.062) - this node is a cross-community bridge._
- **Are the 59 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateClient` and `BybitPrivateWebSocketStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 59 INFERRED edges - model-reasoned connections that need verification._
- **Are the 80 inferred relationships involving `ResearchConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`ResearchConfig` has 80 INFERRED edges - model-reasoned connections that need verification._
- **Are the 73 inferred relationships involving `read_dataset()` (e.g. with `.bootstrap()` and `.rest_reconcile()`) actually correct?**
  _`read_dataset()` has 73 INFERRED edges - model-reasoned connections that need verification._
- **Are the 22 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 22 INFERRED edges - model-reasoned connections that need verification._