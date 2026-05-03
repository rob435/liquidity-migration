# Graph Report - MODEL050426  (2026-05-03)

## Corpus Check
- 31 files · ~67,121 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 308 nodes · 724 edges · 12 communities detected
- Extraction: 78% EXTRACTED · 22% INFERRED · 0% AMBIGUOUS · INFERRED: 159 edges (avg confidence: 0.8)
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
- [[_COMMUNITY_Community 14|Community 14]]

## God Nodes (most connected - your core abstractions)
1. `run_volume_trade_backtest()` - 25 edges
2. `download_market_data()` - 25 edges
3. `main()` - 20 edges
4. `run_volume_grid()` - 17 edges
5. `backtest_volume_trades()` - 16 edges
6. `read_dataset()` - 16 edges
7. `write_dataset()` - 15 edges
8. `run_volume_alpha()` - 14 edges
9. `run_daily_close_fade()` - 14 edges
10. `run_daily_close_fade_grid()` - 14 edges

## Surprising Connections (you probably didn't know these)
- `trades_to_frame()` --calls--> `test_trade_parser_handles_websocket_aliases_and_string_booleans()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py
- `run_volume_trade_backtest()` --calls--> `test_volume_backtest_writes_trade_ledger()`  [INFERRED]
  aggression_carry/volume_backtest.py → tests/test_aggression_carry_volume_alpha.py
- `run_volume_trade_backtest()` --calls--> `test_volume_backtest_can_filter_daily_liquidity_bucket()`  [INFERRED]
  aggression_carry/volume_backtest.py → tests/test_aggression_carry_volume_alpha.py
- `run_volume_trade_backtest()` --calls--> `test_volume_backtest_records_stop_loss_exit_reason()`  [INFERRED]
  aggression_carry/volume_backtest.py → tests/test_aggression_carry_volume_alpha.py
- `run_volume_trade_backtest()` --calls--> `test_volume_backtest_zero_stop_disables_stop_loss()`  [INFERRED]
  aggression_carry/volume_backtest.py → tests/test_aggression_carry_volume_alpha.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.07
Nodes (59): _attribution_rows(), backtest_volume_trades(), _bar_at_close(), _bar_excursion(), _bar_exit_hits(), _btc_monthly_returns(), _btc_regime(), build_equity_curve() (+51 more)

### Community 1 - "Community 1"
Cohesion: 0.09
Nodes (45): CostConfig, DailyCloseFadeConfig, _attach_instrument_age(), backtest_daily_close_fade(), build_close_fade_equity(), build_daily_close_fade_features(), _daily_realized_vol(), _date_range() (+37 more)

### Community 2 - "Community 2"
Cohesion: 0.12
Nodes (29): download_archive_bytes(), download_public_trade_archive(), read_public_trade_archive(), ResearchConfig, _archive_filename(), _archive_outputs_exist(), _dates_between(), download_market_data() (+21 more)

### Community 3 - "Community 3"
Cohesion: 0.15
Nodes (19): _ordinal_rank(), rank_correlation(), _add_cross_sectional_z(), _add_liquidity_rank(), attach_volume_forward_returns(), _best_base_portfolio(), build_volume_features(), compute_volume_metrics() (+11 more)

### Community 4 - "Community 4"
Cohesion: 0.15
Nodes (12): BybitDataError, BybitMarketData, _age_filter_label(), build_current_universe_table(), _empty_universe_table(), format_universe_report(), run_discover_universe(), _safe_name() (+4 more)

### Community 5 - "Community 5"
Cohesion: 0.27
Nodes (18): _add_universe_backtest_args(), _apply_universe_backtest_args(), _backtest_config_from_args(), build_parser(), _close_fade_base_from_grid_args(), _close_fade_config_from_args(), _close_fade_exclusions(), _close_fade_grid_config_from_args() (+10 more)

### Community 6 - "Community 6"
Cohesion: 0.21
Nodes (17): DailyCloseFadeGridConfig, ExchangeConfig, load_config(), _merge_daily_close_fade_config(), _merge_daily_close_fade_grid_config(), _merge_dataclass(), _merge_universe_config(), _merge_volume_alpha_config() (+9 more)

### Community 7 - "Community 7"
Cohesion: 0.21
Nodes (15): TradeFlowConfig, aggregate_signed_flow_1h(), aggregate_signed_flow_1m(), _first_present(), normalize_funding_history(), normalize_trade(), _parse_bool(), _parse_ts_ms() (+7 more)

### Community 8 - "Community 8"
Cohesion: 0.24
Nodes (16): VolumeBacktestConfig, VolumeGridConfig, FixtureSpec, generate_fixture_data(), _grid_chunksize(), iter_grid_configs(), _resolve_workers(), run_volume_grid() (+8 more)

### Community 9 - "Community 9"
Cohesion: 0.35
Nodes (9): _csv_bool(), _csv_float(), _csv_int(), _csv_str(), _format_summary(), main(), parse_args(), _parse_buckets() (+1 more)

### Community 10 - "Community 10"
Cohesion: 0.44
Nodes (9): command_exists(), install_ao(), install_composio(), install_graphify(), install_skills(), main(), parse_args(), print_status() (+1 more)

### Community 14 - "Community 14"
Cohesion: 1.0
Nodes (1): Bybit volume-alpha research package.  This package is the stripped-down rebuild

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit volume-alpha research package.  This package is the stripped-down rebuild`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 14`** (2 nodes): `__init__.py`, `Bybit volume-alpha research package.  This package is the stripped-down rebuild`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `main()` connect `Community 5` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 6`, `Community 8`?**
  _High betweenness centrality (0.080) - this node is a cross-community bridge._
- **Why does `download_market_data()` connect `Community 2` to `Community 1`, `Community 4`, `Community 5`, `Community 7`?**
  _High betweenness centrality (0.076) - this node is a cross-community bridge._
- **Why does `main()` connect `Community 9` to `Community 8`, `Community 6`?**
  _High betweenness centrality (0.062) - this node is a cross-community bridge._
- **Are the 10 inferred relationships involving `run_volume_trade_backtest()` (e.g. with `VolumeBacktestConfig` and `CostConfig`) actually correct?**
  _`run_volume_trade_backtest()` has 10 INFERRED edges - model-reasoned connections that need verification._
- **Are the 15 inferred relationships involving `download_market_data()` (e.g. with `main()` and `BybitMarketData`) actually correct?**
  _`download_market_data()` has 15 INFERRED edges - model-reasoned connections that need verification._
- **Are the 11 inferred relationships involving `main()` (e.g. with `load_config()` and `generate_fixture_data()`) actually correct?**
  _`main()` has 11 INFERRED edges - model-reasoned connections that need verification._
- **Are the 9 inferred relationships involving `run_volume_grid()` (e.g. with `VolumeGridConfig` and `VolumeBacktestConfig`) actually correct?**
  _`run_volume_grid()` has 9 INFERRED edges - model-reasoned connections that need verification._