# Graph Report - MODEL050426  (2026-05-02)

## Corpus Check
- 66 files · ~67,873 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 869 nodes · 2515 edges · 19 communities detected
- Extraction: 60% EXTRACTED · 40% INFERRED · 0% AMBIGUOUS · INFERRED: 995 edges (avg confidence: 0.66)
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
- [[_COMMUNITY_Community 19|Community 19]]

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
- `ReplayProgressTracker` --calls--> `test_replay_progress_tracker_reports_percent_and_eta()`  [INFERRED]
  backtest.py → tests/test_backtest.py
- `validate_universe()` --calls--> `run_smoke()`  [INFERRED]
  universe_validator.py → smoke.py
- `OrderSubmission` --uses--> `TelegramNotifier`  [INFERRED]
  execution.py → alerting.py
- `OrderSubmission` --uses--> `Settings`  [INFERRED]
  execution.py → config.py
- `OrderSubmission` --uses--> `SignalDatabase`  [INFERRED]
  execution.py → database.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (120): download_archive_bytes(), download_public_trade_archive(), read_public_trade_archive(), build_parser(), main(), CostConfig, ExchangeConfig, FeatureConfig (+112 more)

### Community 1 - "Community 1"
Cohesion: 0.04
Nodes (121): _align_down(), _available_memory_bytes(), _average_variant_runtime(), BacktestVariantSpec, _build_backtest_settings(), _build_comprehensive_settings(), _build_stress_variant_specs(), _build_sweep_window_end_times() (+113 more)

### Community 2 - "Community 2"
Cohesion: 0.07
Nodes (86): BacktestResult, BacktestTrade, BacktestVariantRunResult, BacktestVariantSummary, ComprehensiveBacktestResult, ComprehensiveBacktestSummary, DailyBacktestSummary, EquitySnapshot (+78 more)

### Community 3 - "Community 3"
Cohesion: 0.03
Nodes (35): _run_export_reconciliation(), SignalDatabase, ActualTradeEvent, _clean_html_text(), export_reconciliation(), format_reconciliation(), load_actual_trade_events_from_db(), load_backtest_trade_events() (+27 more)

### Community 4 - "Community 4"
Cohesion: 0.05
Nodes (51): AlertPayload, TelegramNotifier, btc_regime_score(), clip_value(), correlation_cluster_labels(), cross_sectional_zscores(), curvature_signal(), dominance_rotation_signal() (+43 more)

### Community 5 - "Community 5"
Cohesion: 0.11
Nodes (7): ExecutionPayload, round_decimal(), WalletBalance, ExecutionAction, ExecutionEngine, OrderSubmission, SimulatedExecutionClient

### Community 6 - "Community 6"
Cohesion: 0.09
Nodes (43): _count_reason(), _export_csvs(), _first_present(), _float_or_none(), _format_float(), format_report(), _int_or_none(), _is_short_side() (+35 more)

### Community 7 - "Community 7"
Cohesion: 0.1
Nodes (9): BybitMarketDataClient, BybitTradeClient, is_rate_limited_payload(), main(), parse_args(), run_replay(), RuntimeError, _exercise_cached_ohlc_range() (+1 more)

### Community 8 - "Community 8"
Cohesion: 0.12
Nodes (23): apply_bootstrap(), build_client_session(), configure_logging(), enqueue_cycle(), format_runtime_summary(), log_runtime_event(), queue_consumer_loop(), refresh_macro_state_loop() (+15 more)

### Community 9 - "Community 9"
Cohesion: 0.15
Nodes (4): HistoricalBacktestSimulator, _utc_day(), ticker_cluster(), test_ticker_daily_loss_limit_blocks_new_entries_in_simulator()

### Community 10 - "Community 10"
Cohesion: 0.12
Nodes (16): _get_bool(), _get_float(), _get_int(), _get_symbols(), _load_dotenv(), load_settings(), fetch_listed_symbols(), main() (+8 more)

### Community 11 - "Community 11"
Cohesion: 0.21
Nodes (9): BenchmarkResult, build_benchmark_state(), format_benchmark(), main(), NullNotifier, parse_args(), run_benchmark(), test_build_benchmark_state_populates_requested_universe() (+1 more)

### Community 12 - "Community 12"
Cohesion: 0.26
Nodes (12): command_exists(), install_ao(), install_composio(), install_graphify(), install_skills(), main(), parse_args(), print_status() (+4 more)

### Community 13 - "Community 13"
Cohesion: 0.27
Nodes (2): BybitDataError, BybitMarketData

### Community 14 - "Community 14"
Cohesion: 0.42
Nodes (8): build_parser(), inspect_cache(), main(), pack_cache(), sqlite_row_count(), unpack_cache(), _create_cache(), test_pack_and_unpack_cache_round_trip()

### Community 15 - "Community 15"
Cohesion: 0.36
Nodes (7): raise_for_invalid_runtime_settings(), RuntimeConfigError, validate_runtime_settings(), ValidationMessage, test_raise_for_invalid_runtime_settings_rejects_live_submit_without_credentials(), test_validate_runtime_settings_accepts_disabled_telegram_override(), test_validate_runtime_settings_warns_when_telegram_enabled_without_credentials()

### Community 16 - "Community 16"
Cohesion: 1.0
Nodes (2): main(), parse_args()

### Community 17 - "Community 17"
Cohesion: 1.0
Nodes (2): _exercise_large_window_replay_fetch(), test_fetch_replay_plan_supports_large_windows_with_range_fetch()

### Community 19 - "Community 19"
Cohesion: 1.0
Nodes (1): Bybit aggression-carry alpha research package.  This package is intentionally se

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit aggression-carry alpha research package.  This package is intentionally se`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 13`** (13 nodes): `BybitDataError`, `BybitMarketData`, `._get()`, `.get_funding_history()`, `.get_instruments_info()`, `.get_klines()`, `.get_open_interest()`, `.get_orderbook()`, `.get_recent_trades()`, `.get_tickers()`, `._paged_time_range()`, `.__post_init__()`, `bybit.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 16`** (3 nodes): `main()`, `parse_args()`, `monitor.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 17`** (3 nodes): `_exercise_large_window_replay_fetch()`, `test_replay.py`, `test_fetch_replay_plan_supports_large_windows_with_range_fetch()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 19`** (2 nodes): `__init__.py`, `Bybit aggression-carry alpha research package.  This package is intentionally se`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `SignalDatabase` connect `Community 3` to `Community 1`, `Community 2`, `Community 4`, `Community 5`, `Community 7`, `Community 8`, `Community 9`, `Community 11`?**
  _High betweenness centrality (0.212) - this node is a cross-community bridge._
- **Why does `Settings` connect `Community 2` to `Community 1`, `Community 3`, `Community 4`, `Community 5`, `Community 7`, `Community 8`, `Community 9`, `Community 10`, `Community 11`, `Community 15`, `Community 17`?**
  _High betweenness centrality (0.155) - this node is a cross-community bridge._
- **Why does `ExecutionEngine` connect `Community 5` to `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 7`, `Community 8`, `Community 9`?**
  _High betweenness centrality (0.106) - this node is a cross-community bridge._
- **Are the 84 inferred relationships involving `SignalDatabase` (e.g. with `OrderSubmission` and `ExecutionAction`) actually correct?**
  _`SignalDatabase` has 84 INFERRED edges - model-reasoned connections that need verification._
- **Are the 118 inferred relationships involving `Settings` (e.g. with `OrderSubmission` and `ExecutionAction`) actually correct?**
  _`Settings` has 118 INFERRED edges - model-reasoned connections that need verification._
- **Are the 57 inferred relationships involving `ExecutionEngine` (e.g. with `ExecutionPayload` and `TelegramNotifier`) actually correct?**
  _`ExecutionEngine` has 57 INFERRED edges - model-reasoned connections that need verification._
- **Are the 76 inferred relationships involving `MarketState` (e.g. with `OrderSubmission` and `ExecutionAction`) actually correct?**
  _`MarketState` has 76 INFERRED edges - model-reasoned connections that need verification._