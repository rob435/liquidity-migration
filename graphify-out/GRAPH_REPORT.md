# Graph Report - MODEL050426  (2026-05-02)

## Corpus Check
- 66 files · ~67,509 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 866 nodes · 2508 edges · 18 communities detected
- Extraction: 60% EXTRACTED · 40% INFERRED · 0% AMBIGUOUS · INFERRED: 992 edges (avg confidence: 0.66)
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
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]

## God Nodes (most connected - your core abstractions)
1. `SignalDatabase` - 148 edges
2. `Settings` - 120 edges
3. `ExecutionEngine` - 100 edges
4. `MarketState` - 87 edges
5. `SignalEngine` - 71 edges
6. `RankedSignal` - 61 edges
7. `BybitMarketDataClient` - 54 edges
8. `HistoricalBacktestSimulator` - 45 edges
9. `HistoricalCandle` - 38 edges
10. `MissingCandlesError` - 37 edges

## Surprising Connections (you probably didn't know these)
- `Settings` --uses--> `GlobalState`  [INFERRED]
  config.py → state.py
- `ReplayProgressTracker` --calls--> `test_replay_progress_tracker_reports_percent_and_eta()`  [INFERRED]
  backtest.py → tests/test_backtest.py
- `validate_universe()` --calls--> `run_smoke()`  [INFERRED]
  universe_validator.py → smoke.py
- `OrderSubmission` --uses--> `Settings`  [INFERRED]
  execution.py → config.py
- `OrderSubmission` --uses--> `SignalDatabase`  [INFERRED]
  execution.py → database.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.05
Nodes (113): AlertPayload, BacktestResult, BacktestTrade, BacktestVariantRunResult, BacktestVariantSpec, BacktestVariantSummary, ComprehensiveBacktestResult, ComprehensiveBacktestSummary (+105 more)

### Community 1 - "Community 1"
Cohesion: 0.03
Nodes (118): download_archive_bytes(), download_public_trade_archive(), read_public_trade_archive(), build_parser(), main(), CostConfig, ExchangeConfig, FeatureConfig (+110 more)

### Community 2 - "Community 2"
Cohesion: 0.04
Nodes (116): _align_down(), _available_memory_bytes(), _average_variant_runtime(), _build_backtest_settings(), _build_comprehensive_settings(), _build_stress_variant_specs(), _build_sweep_window_end_times(), _build_variant_specs() (+108 more)

### Community 3 - "Community 3"
Cohesion: 0.03
Nodes (35): _run_export_reconciliation(), SignalDatabase, ActualTradeEvent, _clean_html_text(), export_reconciliation(), format_reconciliation(), load_actual_trade_events_from_db(), load_backtest_trade_events() (+27 more)

### Community 4 - "Community 4"
Cohesion: 0.09
Nodes (8): ExecutionPayload, TelegramNotifier, round_decimal(), WalletBalance, ExecutionAction, ExecutionEngine, OrderSubmission, SimulatedExecutionClient

### Community 5 - "Community 5"
Cohesion: 0.09
Nodes (44): format_comprehensive_backtest(), _count_reason(), _export_csvs(), _first_present(), _float_or_none(), _format_float(), format_report(), _int_or_none() (+36 more)

### Community 6 - "Community 6"
Cohesion: 0.09
Nodes (30): apply_bootstrap(), build_client_session(), configure_logging(), enqueue_cycle(), format_runtime_summary(), log_runtime_event(), queue_consumer_loop(), refresh_macro_state_loop() (+22 more)

### Community 7 - "Community 7"
Cohesion: 0.1
Nodes (23): btc_regime_score(), clip_value(), correlation_cluster_labels(), cross_sectional_zscores(), curvature_signal(), dominance_rotation_signal(), dominance_state(), ema() (+15 more)

### Community 8 - "Community 8"
Cohesion: 0.11
Nodes (9): BybitDataError, BybitMarketData, BybitTradeClient, ClosedPnlRecord, InstrumentSpec, is_rate_limited_payload(), OrderAck, VenuePosition (+1 more)

### Community 9 - "Community 9"
Cohesion: 0.09
Nodes (22): _get_bool(), _get_float(), _get_int(), _get_symbols(), _load_dotenv(), load_settings(), fetch_replay_plan(), main() (+14 more)

### Community 10 - "Community 10"
Cohesion: 0.15
Nodes (5): HistoricalBacktestSimulator, _timestamp_iso(), _utc_day(), ticker_cluster(), test_ticker_daily_loss_limit_blocks_new_entries_in_simulator()

### Community 11 - "Community 11"
Cohesion: 0.26
Nodes (12): command_exists(), install_ao(), install_composio(), install_graphify(), install_skills(), main(), parse_args(), print_status() (+4 more)

### Community 12 - "Community 12"
Cohesion: 0.22
Nodes (4): CandleGapError, GlobalState, ProvisionalCandle, StateManager

### Community 13 - "Community 13"
Cohesion: 0.42
Nodes (8): build_parser(), inspect_cache(), main(), pack_cache(), sqlite_row_count(), unpack_cache(), _create_cache(), test_pack_and_unpack_cache_round_trip()

### Community 14 - "Community 14"
Cohesion: 0.28
Nodes (4): format_benchmark(), main(), parse_args(), test_format_benchmark_contains_key_metrics()

### Community 15 - "Community 15"
Cohesion: 1.0
Nodes (2): main(), parse_args()

### Community 16 - "Community 16"
Cohesion: 1.0
Nodes (2): _exercise_entry_ready_alert_text(), test_entry_ready_alert_text_uses_live_tradeable_label()

### Community 17 - "Community 17"
Cohesion: 1.0
Nodes (1): Bybit aggression-carry alpha research package.  This package is intentionally se

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit aggression-carry alpha research package.  This package is intentionally se`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 15`** (3 nodes): `main()`, `parse_args()`, `monitor.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 16`** (3 nodes): `_exercise_entry_ready_alert_text()`, `test_alerting.py`, `test_entry_ready_alert_text_uses_live_tradeable_label()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 17`** (2 nodes): `__init__.py`, `Bybit aggression-carry alpha research package.  This package is intentionally se`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `SignalDatabase` connect `Community 3` to `Community 0`, `Community 2`, `Community 4`, `Community 6`, `Community 9`, `Community 10`?**
  _High betweenness centrality (0.213) - this node is a cross-community bridge._
- **Why does `Settings` connect `Community 0` to `Community 2`, `Community 3`, `Community 4`, `Community 6`, `Community 8`, `Community 9`, `Community 10`, `Community 12`?**
  _High betweenness centrality (0.155) - this node is a cross-community bridge._
- **Why does `ExecutionEngine` connect `Community 4` to `Community 0`, `Community 2`, `Community 3`, `Community 6`, `Community 8`, `Community 10`?**
  _High betweenness centrality (0.106) - this node is a cross-community bridge._
- **Are the 84 inferred relationships involving `SignalDatabase` (e.g. with `OrderSubmission` and `ExecutionAction`) actually correct?**
  _`SignalDatabase` has 84 INFERRED edges - model-reasoned connections that need verification._
- **Are the 118 inferred relationships involving `Settings` (e.g. with `OrderSubmission` and `ExecutionAction`) actually correct?**
  _`Settings` has 118 INFERRED edges - model-reasoned connections that need verification._
- **Are the 57 inferred relationships involving `ExecutionEngine` (e.g. with `ExecutionPayload` and `TelegramNotifier`) actually correct?**
  _`ExecutionEngine` has 57 INFERRED edges - model-reasoned connections that need verification._
- **Are the 76 inferred relationships involving `MarketState` (e.g. with `OrderSubmission` and `ExecutionAction`) actually correct?**
  _`MarketState` has 76 INFERRED edges - model-reasoned connections that need verification._