# Graph Report - MODEL050426  (2026-05-19)

## Corpus Check
- 45 files · ~95,026 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 995 nodes · 3030 edges · 18 communities detected
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
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 21|Community 21]]

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
- `main()` --calls--> `test_cli_fixture_pipeline_runs_volume_events()`  [INFERRED]
  aggression_carry/cli.py → tests/test_aggression_carry_cli.py
- `plan_risk_exits()` --calls--> `test_plan_risk_exits_uses_live_position_price_for_stops()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `plan_stop_repairs()` --calls--> `test_plan_stop_repairs_detects_missing_exchange_stop()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `build_ledger_position_pnl_snapshot()` --calls--> `test_ledger_position_snapshot_marks_short_pnl_from_current_price()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (169): TradeLifecycleConfig, FixtureSpec, generate_fixture_data(), _bar_excursion(), _bar_exit_hits(), build_equity_curve(), _daily_equity_values(), _date_boundary_ms() (+161 more)

### Community 1 - "Community 1"
Cohesion: 0.04
Nodes (117): _active_position_by_symbol(), _base36(), _bool(), _build_demo_features(), _build_demo_universe(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot(), build_position_pnl_snapshot() (+109 more)

### Community 2 - "Community 2"
Cohesion: 0.04
Nodes (100): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig (+92 more)

### Community 3 - "Community 3"
Cohesion: 0.12
Nodes (64): ResearchConfig, read_dataset(), EventWebSocketRiskConfig, EventWebSocketRiskEngine, test_archive_download_can_build_1m_klines_from_public_trades(), test_rest_kline_download_only_marks_successful_symbols(), test_rest_kline_download_writes_each_symbol_and_resumes(), FakeKlineMarket (+56 more)

### Community 4 - "Community 4"
Cohesion: 0.04
Nodes (23): BybitDataError, BybitMarketData, BybitPrivateClient, BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitPublicTradeStream, BybitWebSocketTradeClient, _close_ws_client() (+15 more)

### Community 5 - "Community 5"
Cohesion: 0.05
Nodes (70): CostConfig, ExchangeConfig, load_config(), _merge_dataclass(), _merge_universe_config(), TradeFlowConfig, _tuple_str(), _archive_filename() (+62 more)

### Community 6 - "Community 6"
Cohesion: 0.07
Nodes (69): UniverseConfig, _demo_event_config(), _demo_strategy_id(), EventDemoCycleConfig, EventRiskCycleConfig, _normalize_demo_strategy_profile(), _reconcile_pending_order_fills(), _validate_demo_config() (+61 more)

### Community 7 - "Community 7"
Cohesion: 0.1
Nodes (52): _artifact_check(), _basket_returns(), _best_summary_row(), _block_bootstrap(), _boolish(), _cluster_report(), _comparison_family_frame(), _comparison_family_label() (+44 more)

### Community 8 - "Community 8"
Cohesion: 0.17
Nodes (23): _covered_pairs(), DataLayerAuditConfig, _dataset_notes(), _dataset_row(), DatasetCoverageSnapshot, _date_range(), _date_span(), _date_start_ms() (+15 more)

### Community 9 - "Community 9"
Cohesion: 0.21
Nodes (19): _coverage_rows(), _crowded_edge(), _entry_date_expr(), _feature_edge(), _feature_verdict(), FeatureSpec, _filter_split(), _finite_float() (+11 more)

### Community 11 - "Community 11"
Cohesion: 0.18
Nodes (18): build_parser(), test_cli_archive_hourly_api_kline_default_resumes_written_partitions(), test_cli_archive_hourly_kline_default_resumes_written_partitions(), test_cli_archive_kline_default_requires_dense_utc_day(), test_cli_binance_proxy_parses_defaults(), test_cli_champion_challenger_parses_output_dir(), test_cli_data_layer_audit_parses_options(), test_cli_download_data_default_open_interest_interval() (+10 more)

### Community 12 - "Community 12"
Cohesion: 0.25
Nodes (5): BinanceDataError, BinanceUSDMData, _ceil_to_period(), _floor_to_period(), _recent_history_start()

### Community 13 - "Community 13"
Cohesion: 0.29
Nodes (10): _daily_basket_returns(), format_portfolio_hedge_report(), _path_metrics(), _pct(), run_portfolio_hedge_report(), _short_bad_dates(), _split_returns(), _worst_rolling_return() (+2 more)

### Community 14 - "Community 14"
Cohesion: 0.35
Nodes (11): _dedupe_recent_klines(), _demo_kline_compact_cache_paths(), _demo_kline_compact_metadata(), _demo_kline_fetch_ranges(), _download_recent_1h_klines(), _empty_klines(), _fetch_recent_1h_klines(), _read_demo_kline_cache() (+3 more)

### Community 15 - "Community 15"
Cohesion: 0.33
Nodes (9): audit_crowding_model(), classify_liquidity_migration_crowding(), _crowding_reason_expr(), _entry_hour_expr(), format_crowding_model_report(), _pct(), summarize_crowding_classes(), _with_numeric_columns() (+1 more)

### Community 16 - "Community 16"
Cohesion: 0.36
Nodes (7): _add_cross_sectional_z(), _add_liquidity_rank(), build_volume_features(), _daily_bars(), _rolling_mean(), _rolling_sum(), test_volume_features_build_daily_liquidity_ranks()

### Community 17 - "Community 17"
Cohesion: 0.36
Nodes (7): _audit_violations(), ChallengerSpec, champion_challenger_specs(), format_champion_challenger_manifest(), run_champion_challenger_audit(), test_champion_challenger_audit_allows_only_demo_relaxed_submitter(), test_shadow_challenger_commands_never_submit_orders()

### Community 21 - "Community 21"
Cohesion: 1.0
Nodes (1): Bybit liquidity-migration research package.

## Knowledge Gaps
- **3 isolated node(s):** `ExchangeConfig`, `Bybit liquidity-migration research package.`, `FeatureSpec`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 21`** (2 nodes): `__init__.py`, `Bybit liquidity-migration research package.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `main()` connect `Community 2` to `Community 0`, `Community 1`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 8`, `Community 9`, `Community 11`, `Community 13`, `Community 17`?**
  _High betweenness centrality (0.128) - this node is a cross-community bridge._
- **Why does `read_dataset()` connect `Community 3` to `Community 0`, `Community 1`, `Community 2`, `Community 6`, `Community 7`, `Community 8`, `Community 14`, `Community 16`?**
  _High betweenness centrality (0.065) - this node is a cross-community bridge._
- **Why does `VolumeEventResearchConfig` connect `Community 0` to `Community 1`, `Community 2`, `Community 3`, `Community 5`, `Community 6`, `Community 11`?**
  _High betweenness centrality (0.062) - this node is a cross-community bridge._
- **Are the 59 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateClient` and `BybitPrivateWebSocketStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 59 INFERRED edges - model-reasoned connections that need verification._
- **Are the 80 inferred relationships involving `ResearchConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`ResearchConfig` has 80 INFERRED edges - model-reasoned connections that need verification._
- **Are the 73 inferred relationships involving `read_dataset()` (e.g. with `.bootstrap()` and `.rest_reconcile()`) actually correct?**
  _`read_dataset()` has 73 INFERRED edges - model-reasoned connections that need verification._
- **Are the 22 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 22 INFERRED edges - model-reasoned connections that need verification._